"""
Cost basis ヘルパー (移動平均法)。

H1 改修: 投信/株の売却仕訳を「proceeds 全額で asset を減らす」誤った 2-way から、
「簿価 (移動平均) で asset を減らし、差額を Capital Gain/Loss に切り出す」
正しい 3-way 構造に変える際の cost basis 算出。

詳細: docs/Note_Technical_Decisions.md 2026-05-16 (H1+H2 改修方針)
"""

import sqlite3


def calc_moving_avg_cost_per_unit(
    conn: sqlite3.Connection,
    security_account_guid: str,
    sell_date_exclusive: str,
) -> tuple[float, float]:
    """
    指定 security account の sell_date 直前までの移動平均単価を返す。

    Returns: (cost_per_unit, total_units_held)
    買付 + 再投資の累計 (value, qty) から、売却日前までの平均を算出。
    売却前の保有数量がゼロなら (0.0, 0.0) を返す。

    sell_date_exclusive と同日の他 split は集計に含めない (同日複数取引で
    後の split が先の split の cost basis に影響するのを避ける ── 実務上、
    日内取引は CSV 順 = 約定時刻順とは限らないため)。
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT s.value_num, s.value_denom, s.quantity_num, s.quantity_denom
        FROM splits s
        JOIN transactions t ON t.guid = s.tx_guid
        WHERE s.account_guid = ?
          AND t.post_date < ?
          AND s.quantity_num > 0
        ORDER BY t.post_date, t.guid
    """, (security_account_guid, sell_date_exclusive))

    total_value = 0.0
    total_units = 0.0
    for value_num, value_denom, qty_num, qty_denom in cursor.fetchall():
        if not value_denom or not qty_denom:
            continue  # 不完全データはスキップ (J-REIT 重複 split 等)
        total_value += value_num / value_denom
        total_units += qty_num / qty_denom

    if total_units <= 0:
        return (0.0, 0.0)
    return (total_value / total_units, total_units)


def get_capital_gain_account_guid(conn: sqlite3.Connection) -> str:
    """Income/Capital Gain の guid を取得 or 作成。通貨無関係 (split 通貨で区別)。"""
    return _get_or_create_pl_account(conn, ['Income', 'Capital Gain'], 'INCOME')


def get_capital_loss_account_guid(conn: sqlite3.Connection) -> str:
    """Expenses/Capital Loss の guid を取得 or 作成。通貨無関係 (split 通貨で区別)。"""
    return _get_or_create_pl_account(conn, ['Expenses', 'Capital Loss'], 'EXPENSE')


def _get_or_create_pl_account(
    conn: sqlite3.Connection,
    name_path: list[str],
    leaf_type: str,
) -> str:
    """ルート (Income/Expenses) は既存前提、leaf を必要なら作る最小 helper。"""
    import uuid
    cursor = conn.cursor()
    parent_guid = None
    for i, name in enumerate(name_path):
        cursor.execute(
            "SELECT guid FROM accounts WHERE name = ? AND parent_guid IS ?",
            (name, parent_guid),
        )
        result = cursor.fetchone()
        if result:
            parent_guid = result[0]
            continue
        guid = uuid.uuid4().hex
        current_type = leaf_type if i == len(name_path) - 1 else (
            'INCOME' if name_path[0] == 'Income' else 'EXPENSE'
        )
        cursor.execute("""
            INSERT INTO accounts (guid, name, account_type, parent_guid)
            VALUES (?, ?, ?, ?)
        """, (guid, name, current_type, parent_guid))
        parent_guid = guid
    return parent_guid


def build_sell_splits(
    cursor: sqlite3.Cursor,
    tx_guid: str,
    cash_account_guid: str,
    security_account_guid: str,
    capital_gain_account_guid: str,
    capital_loss_account_guid: str,
    proceeds: float,
    units: float,
    cost_per_unit: float,
    value_denom: int = 1,
    asset_qty_denom: int = 1,
) -> None:
    """
    売却 tx の 3-way split を生成・INSERT する共通ヘルパー。

    - Cash:    +proceeds                       (value/qty 同値、value_denom)
    - Asset:   value=-cost_basis_used (value_denom), quantity=-units (asset_qty_denom)
    - Income/Capital Gain or Expenses/Capital Loss: 差額 (value/qty 同値、value_denom)

    proceeds と units は正の値で渡す (売却数量・受取金額)。
    """
    import uuid
    cost_basis_used = units * cost_per_unit
    realized = proceeds - cost_basis_used  # 正なら益、負なら損

    # Cash split (value/qty は同じ通貨額)
    proceeds_scaled = int(round(proceeds * value_denom))
    cursor.execute("""
        INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (uuid.uuid4().hex, tx_guid, cash_account_guid,
          proceeds_scaled, value_denom,
          proceeds_scaled, value_denom))

    # Asset split (value=cost basis、qty=実保有単位数)
    cost_scaled = int(round(cost_basis_used * value_denom))
    units_scaled = int(round(units * asset_qty_denom))
    cursor.execute("""
        INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (uuid.uuid4().hex, tx_guid, security_account_guid,
          -cost_scaled, value_denom,
          -units_scaled, asset_qty_denom))

    # P/L split (value/qty は同じ通貨額)
    realized_scaled = int(round(realized * value_denom))
    if realized_scaled >= 0:
        # Capital Gain: Income 増加は credit (value < 0)
        cursor.execute("""
            INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (uuid.uuid4().hex, tx_guid, capital_gain_account_guid,
              -realized_scaled, value_denom,
              -realized_scaled, value_denom))
    else:
        # Capital Loss: Expense 増加は debit (value > 0)
        cursor.execute("""
            INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (uuid.uuid4().hex, tx_guid, capital_loss_account_guid,
              -realized_scaled, value_denom,
              -realized_scaled, value_denom))

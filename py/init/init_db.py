import sqlite3
from pathlib import Path
import json
import uuid
import pandas as pd

def get_project_root() -> Path:
    """プロジェクトのルートディレクトリを取得します。"""
    return Path(__file__).resolve().parent.parent.parent

def init_database():
    """
    データベースディレクトリと設定ファイルを準備し、
    すべてのデータベースを初期化します。
    """
    PROJECT_ROOT = get_project_root()
    DB_DIR = PROJECT_ROOT / "db"
    CONFIG_DIR = PROJECT_ROOT / "config"

    DB_DIR.mkdir(exist_ok=True)
    CONFIG_DIR.mkdir(exist_ok=True)

    settings_path = CONFIG_DIR / "settings.json"

    if not settings_path.exists():
        default_settings = {
            "db_path": str(DB_DIR / "finance.db")
        }
        with open(settings_path, 'w', encoding='utf-8') as f:
            json.dump(default_settings, f, indent=4, ensure_ascii=False)
        print(f"'{settings_path}' にデフォルト設定ファイルを作成しました。")

    with open(settings_path, 'r', encoding='utf-8') as f:
        settings = json.load(f)

    db_path = settings.get("db_path", str(DB_DIR / "finance.db"))

    init_finance_db(db_path)

    print("統合データベースの初期化が完了しました。")

# ==========================
# finance.db 初期化 (統合データベース)
# ==========================
def init_finance_db(db_path):
    conn = sqlite3.connect(db_path)
    try:
        create_finance_tables(conn)
        conn.commit()
        print(f"'{db_path}' のテーブル初期化が完了しました。")
    finally:
        conn.close()

    # 勘定科目の初期化（dictionary.db依存のためテーブル作成と分離）
    init_accounts_from_dictionary(db_path)


def create_finance_tables(conn: sqlite3.Connection) -> None:
    """
    finance.db 用の全テーブル・インデックス・seedデータを `conn` に作成する。
    `conn` は本番DBでも :memory: でも構わない。commit は呼び出し側の責務。
    """
    cursor = conn.cursor()

    # accounts テーブル
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS accounts (
        guid TEXT PRIMARY KEY NOT NULL UNIQUE,
        name TEXT NOT NULL,
        account_type TEXT NOT NULL, -- 例: ASSET, LIABILITY, INCOME, EXPENSE, EQUITY
        ofx_type TEXT, -- BANK, CREDITCARD, INVESTMENT, CASH など (OFX対応)
        parent_guid TEXT,
        code TEXT,
        description TEXT,
        hidden INTEGER DEFAULT 0,
        placeholder INTEGER DEFAULT 0,
        FOREIGN KEY (parent_guid) REFERENCES accounts(guid)
    )
    """)

    # transactions テーブル
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        guid TEXT PRIMARY KEY NOT NULL UNIQUE,
        post_date TEXT NOT NULL,
        enter_date TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        description TEXT,
        num TEXT, -- 小切手番号など
        ofx_fitid TEXT UNIQUE, -- OFXエクスポート用の一意なID
        currency_guid TEXT, -- 通貨のGUID (将来的な拡張用)
        manual_category_guid TEXT, -- ユーザーが手動で設定したカテゴリ (accounts.guid)
        ai_category_guid TEXT, -- AIが予測したカテゴリ (accounts.guid)
        ai_confirmed_at TEXT, -- AI予測の確定日時 (NULL=未確定バッファ、NOT NULL=splits反映済み)
        FOREIGN KEY (currency_guid) REFERENCES currencies(guid),
        FOREIGN KEY (manual_category_guid) REFERENCES accounts(guid),
        FOREIGN KEY (ai_category_guid) REFERENCES accounts(guid)
    )
    """)

    # investment_transactions テーブル (OFX INVESTMENTセクション対応)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS investment_transactions (
        guid TEXT PRIMARY KEY NOT NULL UNIQUE,
        tx_guid TEXT NOT NULL, -- 関連する transactions.guid
        security_guid TEXT NOT NULL, -- 投資商品のGUID (accounts.guid を利用)
        type TEXT NOT NULL, -- BUY, SELL, DIV, FEE, REINVEST, SPLIT など
        units REAL NOT NULL,
        unit_price REAL NOT NULL,
        commission REAL DEFAULT 0.0,
        total_amount REAL NOT NULL,
        currency_guid TEXT,
        trade_date TEXT NOT NULL,
        settle_date TEXT,
        memo TEXT,
        ofx_invest_id TEXT UNIQUE,
        FOREIGN KEY (tx_guid) REFERENCES transactions(guid),
        FOREIGN KEY (security_guid) REFERENCES accounts(guid),
        FOREIGN KEY (currency_guid) REFERENCES currencies(guid)
    )
    """)

    # splits テーブル
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS splits (
        guid TEXT PRIMARY KEY NOT NULL UNIQUE,
        tx_guid TEXT NOT NULL,
        account_guid TEXT NOT NULL,
        memo TEXT,
        value_num INTEGER NOT NULL, -- 金額の分子 (100円なら100)
        value_denom INTEGER NOT NULL DEFAULT 1, -- 金額の分母
        quantity_num INTEGER, -- 数量の分子 (投資用)
        quantity_denom INTEGER, -- 数量の分母 (投資用)
        reconcile_state TEXT DEFAULT 'n', -- 照合状態 (n:未照合, c:クリア済, y:照合済)
        reconcile_date TEXT,
        FOREIGN KEY (tx_guid) REFERENCES transactions(guid),
        FOREIGN KEY (account_guid) REFERENCES accounts(guid)
    )
    """)

    # currencies テーブル (通貨マスタ)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS currencies (
        guid TEXT PRIMARY KEY NOT NULL UNIQUE,
        mnemonic TEXT NOT NULL UNIQUE, -- 通貨コード (例: JPY, USD)
        fraction INTEGER NOT NULL -- 小数点以下の桁数 (例: JPY=0, USD=2)
    )
    """)

    # exchange_rates テーブル
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS exchange_rates (
        date TEXT NOT NULL,
        from_currency_guid TEXT NOT NULL,
        to_currency_guid TEXT NOT NULL,
        rate REAL NOT NULL,
        PRIMARY KEY (date, from_currency_guid, to_currency_guid),
        FOREIGN KEY (from_currency_guid) REFERENCES currencies(guid),
        FOREIGN KEY (to_currency_guid) REFERENCES currencies(guid)
    )
    """)

    # budget_params: ユーザー調整可能な予算パラメータ
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS budget_params (
        key TEXT PRIMARY KEY,
        value REAL NOT NULL,
        description TEXT
    )
    """)
    # デフォルト値の投入（既存行は上書きしない）
    cursor.execute("""
    INSERT OR IGNORE INTO budget_params (key, value, description) VALUES
        ('savings_ratio', 0.20, '貯蓄・投資の目標比率')
    """)

    # budget_snapshots: 予算分析の実行結果を履歴として保存
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS budget_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_date TEXT NOT NULL,
        salary_avg REAL,
        fixed_total REAL,
        savings_ratio REAL,
        variable_pool REAL,
        category_budgets TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # asset_snapshots: 投資資産の時価評価スナップショット
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS asset_snapshots (
        guid TEXT PRIMARY KEY NOT NULL UNIQUE,
        date TEXT NOT NULL,
        account_guid TEXT NOT NULL,
        market_value_num INTEGER,
        market_value_denom INTEGER DEFAULT 1,
        book_value_num INTEGER,
        book_value_denom INTEGER DEFAULT 1,
        units_num INTEGER,
        units_denom INTEGER DEFAULT 1,
        currency_guid TEXT,
        FOREIGN KEY (account_guid) REFERENCES accounts(guid),
        FOREIGN KEY (currency_guid) REFERENCES currencies(guid)
    )
    """)

    # 評価・目標管理系の旧テーブル群は本リポジトリのスコープ外へ移設済み（2026-06）。
    # finance.db は測定（実測値の記録）専用とし、評価・予測系のストアは別リポジトリが所有する。

    # social_institutions: 社会接続インフラの台帳（institution 単位）
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS social_institutions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        role TEXT,
        dependency_owner TEXT,
        parent_institution_id INTEGER,
        tenure_start_date TEXT,
        cadence TEXT,
        notes TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (parent_institution_id) REFERENCES social_institutions(id)
    )
    """)

    # social_metrics_weekly: institution 別の週次集計（行動観測由来）
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS social_metrics_weekly (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        institution_id INTEGER NOT NULL,
        week_start_date TEXT NOT NULL,
        msgs_outbound INTEGER,
        msgs_inbound INTEGER,
        meeting_count INTEGER,
        connection_count INTEGER,
        last_interaction_date TEXT,
        notes TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (institution_id) REFERENCES social_institutions(id),
        UNIQUE(institution_id, week_start_date)
    )
    """)

    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_social_metrics_inst_week
        ON social_metrics_weekly(institution_id, week_start_date DESC)
    """)

    # activity_sources: 日次運動の source 台帳（labor-bound / labor-independent 区分）
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS activity_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        activity_type TEXT,
        dependency TEXT NOT NULL,
        linked_institution_id INTEGER,
        notes TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (linked_institution_id) REFERENCES social_institutions(id)
    )
    """)

    # activity_metrics_weekly: source 別の週次 MET-hours / 分数 / 回数
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS activity_metrics_weekly (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        week_start_date TEXT NOT NULL,
        met_hours REAL,
        minutes_total INTEGER,
        sessions_count INTEGER,
        notes TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (source_id) REFERENCES activity_sources(id),
        UNIQUE(source_id, week_start_date)
    )
    """)

    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_activity_metrics_src_week
        ON activity_metrics_weekly(source_id, week_start_date DESC)
    """)

    # 提案履歴系の旧テーブルは本リポジトリのスコープ外へ移設済み（2026-06）。
    # finance.db は測定専用（予測・提案系のストアは別リポジトリが所有する）。


def init_accounts_from_dictionary(finance_db_path):
    """
    dictionary.db からカテゴリを読み込み、finance.db の accounts テーブルに登録する。
    """
    PROJECT_ROOT = get_project_root()
    dictionary_db_path = PROJECT_ROOT / "db" / "dictionary.db"
    
    finance_conn = sqlite3.connect(finance_db_path)
    finance_cursor = finance_conn.cursor()

    try:
        # ルート勘定のGUIDを保持する辞書
        root_guids = {}

        # 基本的なルート勘定を登録
        root_accounts = {
            "Assets": "ASSET", "Liabilities": "LIABILITY", 
            "Income": "INCOME", "Expenses": "EXPENSE", "Equity": "EQUITY"
        }
        for name, type in root_accounts.items():
            finance_cursor.execute("SELECT guid FROM accounts WHERE name = ? AND parent_guid IS NULL", (name,))
            res = finance_cursor.fetchone()
            if res is None:
                guid = str(uuid.uuid4().hex)
                finance_cursor.execute(
                    "INSERT INTO accounts (guid, name, account_type, placeholder) VALUES (?, ?, ?, 1)",
                    (guid, name, type)
                )
                root_guids[name] = guid
            else:
                root_guids[name] = res[0]

        # Assetsの下に Cash, Bank, Investments を作成
        asset_subs = ["Cash", "Bank", "Investments"]
        for sub in asset_subs:
            finance_cursor.execute("SELECT guid FROM accounts WHERE name = ? AND parent_guid = ?", (sub, root_guids["Assets"]))
            if finance_cursor.fetchone() is None:
                guid = str(uuid.uuid4().hex)
                finance_cursor.execute(
                    "INSERT INTO accounts (guid, name, account_type, parent_guid, placeholder) VALUES (?, ?, ?, ?, 1)",
                    (guid, sub, "ASSET", root_guids["Assets"])
                )
        
        # Retained Earnings を Equity の下に作成
        finance_cursor.execute(
            "SELECT guid FROM accounts WHERE name = 'Retained Earnings' AND account_type = 'EQUITY'"
        )
        if finance_cursor.fetchone() is None:
            guid = str(uuid.uuid4().hex)
            finance_cursor.execute(
                "INSERT INTO accounts (guid, name, account_type, parent_guid) VALUES (?, ?, ?, ?)",
                (guid, 'Retained Earnings', 'EQUITY', root_guids.get("Equity"))
            )

        # Uncategorized勘定をExpensesの下に作成
        finance_cursor.execute("SELECT guid FROM accounts WHERE name = 'Uncategorized'")
        if finance_cursor.fetchone() is None:
            guid = str(uuid.uuid4().hex)
            finance_cursor.execute(
                "INSERT INTO accounts (guid, name, account_type, parent_guid) VALUES (?, ?, ?, ?)",
                (guid, 'Uncategorized', 'EXPENSE', root_guids.get("Expenses"))
            )

        # 辞書DBがあれば読み込む
        if dictionary_db_path.exists():
            dict_conn = sqlite3.connect(dictionary_db_path)
            try:
                df = pd.read_sql_query("SELECT main_category, sub_category FROM category_dict_manual", dict_conn)
                
                for _, row in df.iterrows():
                    main_cat, sub_cat = row['main_category'], row['sub_category']
                    
                    parent_root_name = "Income" if "収入" in main_cat else "Expenses"
                    parent_root_guid = root_guids.get(parent_root_name)

                    # 主カテゴリ
                    finance_cursor.execute("SELECT guid FROM accounts WHERE name = ? AND parent_guid = ?", (main_cat, parent_root_guid))
                    main_cat_guid_result = finance_cursor.fetchone()
                    if main_cat_guid_result is None:
                        main_cat_guid = str(uuid.uuid4().hex)
                        finance_cursor.execute(
                            "INSERT INTO accounts (guid, name, account_type, parent_guid, placeholder) VALUES (?, ?, ?, ?, 1)",
                            (main_cat_guid, main_cat, 'EXPENSE' if parent_root_name == 'Expenses' else 'INCOME', parent_root_guid)
                        )
                    else:
                        main_cat_guid = main_cat_guid_result[0]

                    # サブカテゴリ
                    finance_cursor.execute("SELECT guid FROM accounts WHERE name = ? AND parent_guid = ?", (sub_cat, main_cat_guid))
                    if finance_cursor.fetchone() is None:
                        sub_cat_guid = str(uuid.uuid4().hex)
                        finance_cursor.execute(
                            "INSERT INTO accounts (guid, name, account_type, parent_guid) VALUES (?, ?, ?, ?)",
                            (sub_cat_guid, sub_cat, 'EXPENSE' if parent_root_name == 'Expenses' else 'INCOME', main_cat_guid)
                        )
            finally:
                dict_conn.close()

        finance_conn.commit()

    except Exception as e:
        print(f"勘定科目の初期化中にエラーが発生しました: {e}")
        finance_conn.rollback()
    finally:
        finance_conn.close()

if __name__ == "__main__":
    init_database()
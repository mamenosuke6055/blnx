import pandas as pd
import sqlite3
import json
from pathlib import Path
import glob
import hashlib
import uuid
from datetime import datetime
import re

from py.importers._cost_basis import (
    calc_moving_avg_cost_per_unit,
    get_capital_gain_account_guid,
    get_capital_loss_account_guid,
    build_sell_splits,
)

def get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent

def get_config():
    root = get_project_root()
    config_path = root / "config/settings.json"
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def get_db_path():
    config = get_config()
    return get_project_root() / config.get("db_path", "db/finance.db")

def get_or_create_account_guid(conn: sqlite3.Connection, name_path: list[str], account_type: str, ofx_type: str = None, code: str = None) -> str:
    cursor = conn.cursor()
    parent_guid = None
    
    for i, name in enumerate(name_path):
        cursor.execute("""
            SELECT guid FROM accounts WHERE name = ? AND parent_guid IS ?
        """, (name, parent_guid) if parent_guid else (name, None))
        
        result = cursor.fetchone()
        
        if result:
            guid = result[0]
            # Update code if missing and provided for the leaf node
            if i == len(name_path) - 1 and code:
                cursor.execute("UPDATE accounts SET code = ? WHERE guid = ? AND code IS NULL", (code, guid))
        else:
            guid = uuid.uuid4().hex
            current_type = account_type if i == len(name_path) - 1 else 'ASSET'
            if i == 0 and name == 'Expenses': current_type = 'EXPENSE'
            if i == 0 and name == 'Income': current_type = 'INCOME'
            
            current_ofx_type = ofx_type if i == len(name_path) - 1 else None
            
            current_code = code if i == len(name_path) - 1 else None

            cursor.execute("""
                INSERT INTO accounts (guid, name, account_type, ofx_type, parent_guid, code)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (guid, name, current_type, current_ofx_type, parent_guid, current_code))
            print(f"Created account: {' > '.join(name_path[:i+1])}")
        
        parent_guid = guid
        
    return parent_guid

def get_currency_guid(conn: sqlite3.Connection, mnemonic: str) -> str:
    cursor = conn.cursor()
    cursor.execute("SELECT guid FROM currencies WHERE mnemonic = ?", (mnemonic,))
    result = cursor.fetchone()
    if result:
        return result[0]
    
    guid = uuid.uuid4().hex
    fraction = 100 
    cursor.execute("INSERT INTO currencies (guid, mnemonic, fraction) VALUES (?, ?, ?)", (guid, mnemonic, fraction))
    return guid

def parse_rakuten_date(date_str):
    if not isinstance(date_str, str): return None
    date_str = date_str.strip()
    try:
        return datetime.strptime(date_str, '%Y/%m/%d').strftime('%Y-%m-%d')
    except ValueError:
        return None

def parse_amount(val):
    if pd.isna(val) or val == '-' or val == '':
        return 0.0
    if isinstance(val, str):
        # Remove commas and extract numbers (sometimes contains text like "10,000(500)")
        # For simple numbers:
        cleaned = val.replace(',', '')
        # Handle "10,000(500)" -> extract 10000. Assuming (500) is point usage etc.
        match = re.match(r'^(-?\d+(\.\d+)?)', cleaned)
        if match:
            return float(match.group(1))
    return float(val)

def create_snapshot_table_if_not_exists(conn: sqlite3.Connection):
    cursor = conn.cursor()
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
        );
    """)
    conn.commit()

def import_rakuten_asset_balance(csv_path: Path):
    # Find the "Detailed" section
    detail_header_row = -1
    date_str = None
    
    # Extract date from filename if possible (e.g., assetbalance(all)_20251112_...)
    # format: YYYYMMDD
    filename_match = re.search(r'(\d{8})', csv_path.name)
    if filename_match:
        try:
            date_str = datetime.strptime(filename_match.group(1), '%Y%m%d').strftime('%Y-%m-%d')
        except ValueError:
            pass
            
    try:
        with open(csv_path, 'r', encoding='cp932') as f:
            for i, line in enumerate(f):
                # Search for detailed section header
                # Typically contains "口座" and "評価額" (Market Value) or "銘柄名"
                if '口座' in line and ('評価額' in line or '銘柄名' in line):
                    detail_header_row = i
                    break
    except Exception as e:
        print(f"Error inspecting CSV: {e}")
        return

    if detail_header_row == -1:
        print(f"Detail header not found in {csv_path.name}")
        return

    try:
        df = pd.read_csv(csv_path, encoding='cp932', skiprows=detail_header_row, header=0)
        # DEBUG: Print columns to verify header detection
        print(f"DEBUG: {csv_path.name} columns: {df.columns.tolist()}")
    except Exception as e:
        print(f"Error reading CSV details: {e}")
        return
        
    conn = sqlite3.connect(get_db_path())
    create_snapshot_table_if_not_exists(conn)
    cursor = conn.cursor()
    
    jpy_guid = get_currency_guid(conn, 'JPY')
    
    count = 0
    for _, row in df.iterrows():
        # Check if row is valid (has Name)
        # Columns might be '銘柄' or '銘柄名' or 'ファンド名'
        name_col = None
        for col in ['銘柄', '銘柄名', 'ファンド名']:
            if col in df.columns:
                name_col = col
                break
        
        if not name_col or pd.isna(row.get(name_col)):
            continue
            
        name = row.get(name_col)
        if not name: continue
        
        # Quantity
        # Stocks: "保有数量" or "数量"
        # Funds: "保有口数" or "数量"
        units_raw = row.get('保有数量') if '保有数量' in df.columns else row.get('保有口数')
        if pd.isna(units_raw): units_raw = row.get('数量') # Fallback
        
        # Market Value
        market_val_raw = row.get('時価評価額') if '時価評価額' in df.columns else row.get('評価額')
        if pd.isna(market_val_raw): market_val_raw = row.get('時価評価額[円]')
        
        # Book Value (Acquisition Cost)
        book_val_raw = row.get('取得金額') if '取得金額' in df.columns else row.get('取得金額[円]')
        if pd.isna(book_val_raw): book_val_raw = row.get('平均取得価額') # Average acquisition price
        
        units = parse_amount(units_raw)
        market_val = parse_amount(market_val_raw)
        
        # Try to find account
        # Note: We might need to handle 'Code' for stocks
        code_col = '銘柄コード' if '銘柄コード' in df.columns else '銘柄コード・ティッカー'
        code = str(row.get(code_col)) if code_col in df.columns and not pd.isna(row.get(code_col)) else None
        
        # Search for account
        account_guid = None
        if code:
             # Try exact match or match stripping extension like .T
             cursor.execute("SELECT guid FROM accounts WHERE code = ?", (code,))
             res = cursor.fetchone()
             if res: account_guid = res[0]
        
        if not account_guid:
            cursor.execute("SELECT guid FROM accounts WHERE name = ?", (name,))
            res = cursor.fetchone()
            if res: account_guid = res[0]
            
        # If account not found, we can create it or skip.
        # For snapshots, better to create if we want to track everything.
        if not account_guid:
            # Assume it's an investment under Rakuten Sec
            account_guid = get_or_create_account_guid(
                conn, ['Assets', 'Investments', 'Rakuten Securities', name], 'ASSET', 'INVESTMENT', code=code
            )

        # Check Duplicate Snapshot
        # Same date, same account
        if date_str:
            cursor.execute("SELECT guid FROM asset_snapshots WHERE date = ? AND account_guid = ?", (date_str, account_guid))
            if cursor.fetchone(): continue
            
            snap_guid = uuid.uuid4().hex
            cursor.execute("""
                INSERT INTO asset_snapshots (guid, date, account_guid, market_value_num, units_num, currency_guid)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (snap_guid, date_str, account_guid, int(market_val), int(units), jpy_guid))
            count += 1
            
    conn.commit()
    conn.close()
    if count > 0:
        print(f"Imported {count} asset snapshots for {date_str}.")
    else:
        print(f"No new snapshots imported for {date_str} (or data empty/duplicate).")

def import_rakuten_sec_csv(csv_path: Path):
    print(f"Importing {csv_path.name}...")

    # 1. 入出金 (Withdrawallist)
    if "Withdrawallist" in csv_path.name:
        import_rakuten_withdrawal_list(csv_path)
    # 2. 投資信託 (tradehistory(INVST))
    elif "tradehistory(INVST)" in csv_path.name:
        import_rakuten_trade_invst(csv_path)
    # 3. 国内株式 (tradehistory(JP))
    elif "tradehistory(JP)" in csv_path.name:
        import_rakuten_trade_jp(csv_path)
    # 4. 資産残高 (assetbalance)
    elif "assetbalance" in csv_path.name:
        import_rakuten_asset_balance(csv_path)
    # 5. 調整履歴 (adjusthistory(JP)) — クレジットカード決済・配当金等
    elif "adjusthistory(JP)" in csv_path.name:
        import_rakuten_adjhistory_jp(csv_path)
    else:
        print(f"Unknown file type: {csv_path.name}")


def import_rakuten_adjhistory_jp(csv_path: Path):
    """adjusthistory(JP) からWithdrawalistや投資取引ファイルでカバーされない現金フローを取り込む。

    対象: 入金(クレジットカード決済ご利用分)、振替入金、国内株式配当金、譲渡益税
    スキップ: Withdrawallist済み項目、投資売買取引（tradehistory済み）、投信分配金/再投資
    """
    # Withdrawallist でカバー済み → 二重計上を防ぐためスキップ
    SKIP_TYPES = {
        'らくらく入金(楽天銀行)', 'らくらく出金(楽天銀行)', '自動出金(スイープ)',
        '国内株式(自動入金)', '入金(楽天ポイント交換)',
    }
    # tradehistory ファイルでカバー済みの投資売買 → スキップ
    INVEST_TYPES = {
        '株式投信購入', '株式投信購入（積立）', '株式投信解約',
        '株式購入', '株式売却', '投信再投資', '投信分配金', '外貨建てＭＭＦ買付',
    }

    try:
        df = pd.read_csv(csv_path, encoding='cp932', header=0)
    except Exception as e:
        print(f"Error reading CSV: {e}")
        return

    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()

    rakuten_sec_acct = get_or_create_account_guid(conn, ['Assets', 'Bank', 'Rakuten Securities'], 'ASSET', 'BANK')
    # 本体 importer(import_rakutencard)と同じ階層を使う。Credit Card を挟まないと
    # 迷子アカウント Liabilities>Rakuten Card が分離する(7550670e の積み残しで統合済み)。
    rakuten_card_acct = get_or_create_account_guid(conn, ['Liabilities', 'Credit Card', 'Rakuten Card'], 'LIABILITY')
    income_div_acct = get_or_create_account_guid(conn, ['Income', 'Dividend'], 'INCOME')
    expense_tax_acct = get_or_create_account_guid(conn, ['Expenses', 'Tax'], 'EXPENSE')
    transfer_acct = get_or_create_account_guid(conn, ['Assets', 'Transfer'], 'ASSET')
    jpy_guid = get_currency_guid(conn, 'JPY')
    conn.commit()

    count = 0
    for _, row in df.iterrows():
        tx_type = str(row['取引区分']).strip()

        if tx_type in SKIP_TYPES or tx_type in INVEST_TYPES:
            continue

        trade_date = parse_rakuten_date(str(row['約定日']))
        if not trade_date:
            continue

        recv = parse_amount(row['受渡金額（受取）'])
        pay = parse_amount(row['受渡金額（支払）'])

        if recv > 0:
            amount = recv
        elif pay > 0:
            amount = -pay
        else:
            continue

        if '入金(クレジットカード決済ご利用分)' in tx_type:
            peer = rakuten_card_acct
        elif '振替入金' in tx_type:
            peer = transfer_acct
        elif '国内株式配当金' in tx_type:
            peer = income_div_acct
        elif '譲渡益税' in tx_type:
            peer = expense_tax_acct
        else:
            print(f"  Unknown type skipped: {tx_type}")
            continue

        sec_name = str(row['対象証券名']).strip()
        desc = tx_type if sec_name == '-' else f"{tx_type} {sec_name}"

        fitid = 'SHA256:' + hashlib.sha256(
            f"RAKUTEN_ADJ:{trade_date}:{tx_type}:{amount}".encode()
        ).hexdigest()

        cursor.execute("SELECT 1 FROM transactions WHERE ofx_fitid = ?", (fitid,))
        if cursor.fetchone():
            continue

        tx_guid = uuid.uuid4().hex
        cursor.execute("""
            INSERT INTO transactions (guid, post_date, description, ofx_fitid, currency_guid)
            VALUES (?, ?, ?, ?, ?)
        """, (tx_guid, trade_date, desc, fitid, jpy_guid))

        cursor.execute("""
            INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
            VALUES (?, ?, ?, ?, 1, ?, 1)
        """, (uuid.uuid4().hex, tx_guid, rakuten_sec_acct, int(amount), int(amount)))

        cursor.execute("""
            INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
            VALUES (?, ?, ?, ?, 1, ?, 1)
        """, (uuid.uuid4().hex, tx_guid, peer, int(-amount), int(-amount)))

        count += 1

    conn.commit()
    conn.close()
    print(f"Imported {count} cash transactions from adjusthistory(JP).")

def import_rakuten_withdrawal_list(csv_path: Path):
    # Dynamically find header row
    header_row = -1
    try:
        with open(csv_path, 'r', encoding='cp932') as f:
            for i, line in enumerate(f):
                if '入出金日' in line and '入金額' in line:
                    header_row = i
                    break
    except Exception as e:
        print(f"Error inspecting CSV for header: {e}")
        return

    if header_row == -1:
        print(f"Header not found in {csv_path.name}")
        return

    try:
        # Use skiprows to accurately target the header line, avoiding index shifts
        df = pd.read_csv(csv_path, encoding='cp932', skiprows=header_row, header=0)
    except Exception as e:
        print(f"Error reading CSV: {e}")
        return
    
    # Check columns
    required = ['入出金日', '入金額[円]', '出金額[円]', '内容']
    if not all(col in df.columns for col in required):
        print(f"Missing columns in {csv_path.name}. Found: {df.columns}")
        return

    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    
    rakuten_sec_acct = get_or_create_account_guid(conn, ['Assets', 'Bank', 'Rakuten Securities'], 'ASSET', 'BANK')
    # Default expense/income for points etc
    income_point_acct = get_or_create_account_guid(conn, ['Income', 'Points'], 'INCOME')
    # 楽天銀行↔楽天証券の資金移動は Transfer を clearing 口座として経由する。
    # 楽天銀行口座の脚は銀行自身のCSV(import_rakutenbank.py)が独立に取り込む(RB/Transfer)ので、
    # ここで peer を Rakuten Bank に直接 posting すると RB が二重に借方計上される(二重計上バグ)。
    # 規範形: 銀行CSV=RB/Transfer, 証券CSV=RakSec/Transfer で Transfer がwashする。
    transfer_acct = get_or_create_account_guid(conn, ['Assets', 'Transfer'], 'ASSET')
    
    jpy_guid = get_currency_guid(conn, 'JPY')
    conn.commit()
    
    count = 0
    for _, row in df.iterrows():
        date = parse_rakuten_date(row['入出金日'])
        if not date: continue
        
        in_amt = parse_amount(row['入金額[円]'])
        out_amt = parse_amount(row['出金額[円]'])
        desc = row['内容']
        target = row['出金先'] if not pd.isna(row['出金先']) else ""
        
        amount = 0.0
        peer_account = None
        
        # Logic for Rakuten Sec Cash
        # This table represents Cash movements in Rakuten Sec.
        
        if in_amt > 0:
            amount = in_amt # Deposit to Sec
            # Peer determination
            if '楽天銀行' in desc or '楽天銀行' in target:
                peer_account = transfer_acct  # 銀行脚は銀行CSV側で計上。clearing 経由で wash
            elif 'ポイント' in desc:
                peer_account = income_point_acct
            else:
                peer_account = transfer_acct

        elif out_amt > 0:
            amount = -out_amt # Withdraw from Sec
            if '楽天銀行' in desc or '楽天銀行' in target:
                peer_account = transfer_acct  # 同上: RB 直接 posting は二重計上になる
            else:
                peer_account = transfer_acct
        else:
            continue

        raw_str = f"RAKUTEN_DW:{date}:{desc}:{amount}"
        fitid = 'SHA256:' + hashlib.sha256(raw_str.encode()).hexdigest()
        
        cursor.execute("SELECT 1 FROM transactions WHERE ofx_fitid = ?", (fitid,))
        if cursor.fetchone(): continue
        
        tx_guid = uuid.uuid4().hex
        cursor.execute("""
            INSERT INTO transactions (guid, post_date, description, ofx_fitid, currency_guid)
            VALUES (?, ?, ?, ?, ?)
        """, (tx_guid, date, desc, fitid, jpy_guid))
        
        # Split 1: Rakuten Sec Cash (Main)
        cursor.execute("""
            INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
            VALUES (?, ?, ?, ?, 1, ?, 1)
        """, (uuid.uuid4().hex, tx_guid, rakuten_sec_acct, int(amount), int(amount)))
        
        # Split 2: Peer
        cursor.execute("""
            INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
            VALUES (?, ?, ?, ?, 1, ?, 1)
        """, (uuid.uuid4().hex, tx_guid, peer_account, int(-amount), int(-amount)))
        
        count += 1
        
    conn.commit()
    conn.close()
    print(f"Imported {count} deposit/withdrawal transactions.")


def import_rakuten_trade_invst(csv_path: Path):
    # Header index 0
    try:
        df = pd.read_csv(csv_path, encoding='cp932')
    except Exception as e:
        print(f"Error reading CSV: {e}")
        return

    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    
    rakuten_sec_bank = get_or_create_account_guid(conn, ['Assets', 'Bank', 'Rakuten Securities'], 'ASSET', 'BANK')
    income_div_acct = get_or_create_account_guid(conn, ['Income', 'Dividend'], 'INCOME')
    
    count = 0
    for _, row in df.iterrows():
        date = parse_rakuten_date(row['約定日'])
        settle_date = parse_rakuten_date(row['受渡日'])
        if not date: continue
        
        fund_name = row['ファンド名']
        tx_type_raw = row['取引'] # 買付, 解約, 再投資
        units = parse_amount(row['数量［口］'])
        unit_price = parse_amount(row['単価'])
        total_amount = parse_amount(row['受渡金額/(ポイント利用)[円]'])
        currency_raw = row['決済通貨'] # 円
        
        currency_guid = get_currency_guid(conn, 'JPY') # Assuming JPY for now
        
        # Account
        sec_account = get_or_create_account_guid(
            conn, ['Assets', 'Investments', 'Rakuten Securities', fund_name], 'ASSET', 'INVESTMENT'
        )
        
        fitid = 'SHA256:' + hashlib.sha256(f"RAKUTEN_INVST:{date}:{fund_name}:{tx_type_raw}:{units}:{total_amount}".encode()).hexdigest()
        
        cursor.execute("SELECT 1 FROM transactions WHERE ofx_fitid = ?", (fitid,))
        if cursor.fetchone(): continue
        
        tx_guid = uuid.uuid4().hex
        desc = f"{tx_type_raw} {fund_name}"
        
        cursor.execute("""
            INSERT INTO transactions (guid, post_date, description, ofx_fitid, currency_guid)
            VALUES (?, ?, ?, ?, ?)
        """, (tx_guid, date, desc, fitid, currency_guid))
        
        inv_type = 'BUY'
        
        if tx_type_raw == '買付':
            inv_type = 'BUY'
            # Debit Asset (Increase)
            cursor.execute("""
                INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                VALUES (?, ?, ?, ?, 1, ?, 10000)
            """, (uuid.uuid4().hex, tx_guid, sec_account, int(total_amount), int(units * 10000))) # Assuming units is 1/10000 scale? Fund usually 1 unit = 1 yen usually 10000 units price? 
            # Actually Rakuten CSV "単価" is usually for 10,000 units. 
            # Quantity in CSV is "口". 
            # Price convention: If price is per 10000 units, and you bought X units.
            # We store quantity as X.
            
            # Credit Cash (Decrease)
            cursor.execute("""
                INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                VALUES (?, ?, ?, ?, 1, ?, 1)
            """, (uuid.uuid4().hex, tx_guid, rakuten_sec_bank, int(-total_amount), int(-total_amount)))
            
        elif tx_type_raw == '解約': # Sell — H1 修正: 3-way (Cash + Asset@cost + P/L)
            inv_type = 'SELL'
            cost_per_unit, _ = calc_moving_avg_cost_per_unit(conn, sec_account, date)
            cap_gain = get_capital_gain_account_guid(conn)
            cap_loss = get_capital_loss_account_guid(conn)
            build_sell_splits(
                cursor, tx_guid,
                cash_account_guid=rakuten_sec_bank,
                security_account_guid=sec_account,
                capital_gain_account_guid=cap_gain,
                capital_loss_account_guid=cap_loss,
                proceeds=total_amount,
                units=units,
                cost_per_unit=cost_per_unit,
                value_denom=1,
                asset_qty_denom=10000,
            )

        elif tx_type_raw == '再投資':
            inv_type = 'REINVEST'
             # Debit Asset (Increase)
            cursor.execute("""
                INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                VALUES (?, ?, ?, ?, 1, ?, 10000)
            """, (uuid.uuid4().hex, tx_guid, sec_account, int(total_amount), int(units * 10000)))
            
            # Credit Income (Increase)
            cursor.execute("""
                INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                VALUES (?, ?, ?, ?, 1, ?, 1)
            """, (uuid.uuid4().hex, tx_guid, income_div_acct, int(-total_amount), int(-total_amount)))

        # Inv Tx Table
        cursor.execute("""
            INSERT INTO investment_transactions (guid, tx_guid, security_guid, type, units, unit_price, total_amount, currency_guid, trade_date, settle_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (uuid.uuid4().hex, tx_guid, sec_account, inv_type, units, unit_price, total_amount, currency_guid, date, settle_date))
        
        count += 1
        
    conn.commit()
    conn.close()
    print(f"Imported {count} investment trades.")

def import_rakuten_trade_jp(csv_path: Path):
    try:
        df = pd.read_csv(csv_path, encoding='cp932')
    except Exception as e:
        print(f"Error reading CSV: {e}")
        return

    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    
    rakuten_sec_bank = get_or_create_account_guid(conn, ['Assets', 'Bank', 'Rakuten Securities'], 'ASSET', 'BANK')
    
    count = 0
    for _, row in df.iterrows():
        date = parse_rakuten_date(row['約定日'])
        settle_date = parse_rakuten_date(row['受渡日'])
        if not date: continue
        
        code = str(row['銘柄コード'])
        name = row['銘柄名']
        tx_type_raw = row['売買区分'] # 買付, 売付
        units = parse_amount(row['数量［株］'])
        unit_price = parse_amount(row['単価［円］'])
        total_amount = parse_amount(row['受渡金額［円］'])
        
        currency_guid = get_currency_guid(conn, 'JPY')
        
        sec_account = get_or_create_account_guid(
            conn, ['Assets', 'Investments', 'Rakuten Securities', name], 'ASSET', 'INVESTMENT', code=code
        )
        
        fitid = 'SHA256:' + hashlib.sha256(f"RAKUTEN_JP:{date}:{code}:{tx_type_raw}:{units}:{total_amount}".encode()).hexdigest()
        
        cursor.execute("SELECT 1 FROM transactions WHERE ofx_fitid = ?", (fitid,))
        if cursor.fetchone(): continue
        
        tx_guid = uuid.uuid4().hex
        desc = f"{tx_type_raw} {name}"
        
        cursor.execute("""
            INSERT INTO transactions (guid, post_date, description, ofx_fitid, currency_guid)
            VALUES (?, ?, ?, ?, ?)
        """, (tx_guid, date, desc, fitid, currency_guid))
        
        inv_type = 'BUY'
        
        if tx_type_raw == '買付':
            inv_type = 'BUY'
            # Debit Asset
            cursor.execute("""
                INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                VALUES (?, ?, ?, ?, 1, ?, 1)
            """, (uuid.uuid4().hex, tx_guid, sec_account, int(total_amount), int(units)))
            
            # Credit Cash
            cursor.execute("""
                INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                VALUES (?, ?, ?, ?, 1, ?, 1)
            """, (uuid.uuid4().hex, tx_guid, rakuten_sec_bank, int(-total_amount), int(-total_amount)))
            
        elif tx_type_raw == '売付':  # H1 修正: 3-way (Cash + Asset@cost + P/L)
            inv_type = 'SELL'
            cost_per_unit, _ = calc_moving_avg_cost_per_unit(conn, sec_account, date)
            cap_gain = get_capital_gain_account_guid(conn)
            cap_loss = get_capital_loss_account_guid(conn)
            build_sell_splits(
                cursor, tx_guid,
                cash_account_guid=rakuten_sec_bank,
                security_account_guid=sec_account,
                capital_gain_account_guid=cap_gain,
                capital_loss_account_guid=cap_loss,
                proceeds=total_amount,
                units=units,
                cost_per_unit=cost_per_unit,
                value_denom=1,
                asset_qty_denom=1,
            )

        cursor.execute("""
            INSERT INTO investment_transactions (guid, tx_guid, security_guid, type, units, unit_price, total_amount, currency_guid, trade_date, settle_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (uuid.uuid4().hex, tx_guid, sec_account, inv_type, units, unit_price, total_amount, currency_guid, date, settle_date))
        
        count += 1

    conn.commit()
    conn.close()
    print(f"Imported {count} JP stock trades.")

if __name__ == '__main__':
    project_root = get_project_root()
    csv_dir = project_root / "data/raw/rakuten_sec"
    
    csv_files = glob.glob(str(csv_dir / "*.csv"))
    if not csv_files:
        print("No CSV files found.")
    else:
        for f in csv_files:
            import_rakuten_sec_csv(Path(f))
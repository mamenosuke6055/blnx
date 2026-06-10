# Spec_Dneobank_Import

住信SBIネット銀行のCSVデータ（CP932/Shift-JISエンコーディング）を、OFX準拠の統合データベース（`finance.db`）に取り込むためのデータ構造とマッピング仕様。

既存の `init_db.py` で定義されたテーブル構造（複式簿記 + OFX拡張）をベースとする。

## 1. データベース構造（スキーマ）

銀行の入出金取引は主に `accounts`, `transactions`, `splits`, `currencies` の各テーブルを使用して記録する。

### accounts
*   **役割**: 口座（住信SBIネット銀行）および収入・費用勘定を管理。
*   `guid`: 一意なID
*   `name`: 勘定科目名（例: "SBI Sumishin Net Bank", "Uncategorized Expenses"）
*   `account_type`: "ASSET" (資産), "INCOME" (収入), "EXPENSE" (費用)
*   `ofx_type`: "BANK" (銀行口座)
*   `parent_guid`: 親アカウントのID（例: `Assets:Bank`）

### transactions
*   **役割**: 取引の基本情報（日付、説明、OFX FITID）を記録。
*   `guid`: 一意なID
*   `post_date`: 取引日
*   `description`: 取引内容
*   `ofx_fitid`: OFX準拠の一意なID。重複取り込み防止に使用。

### splits
*   **役割**: 資金の動き（複式簿記の仕訳）を記録。
*   `tx_guid`: `transactions` テーブルへのリンク
*   `account_guid`: 勘定科目（`accounts` テーブル）へのリンク
*   `value_num`: 金額の分子。入金は銀行口座側がプラス、費用/収益勘定側がマイナス。出金は銀行口座側がマイナス、費用/収益勘定側がプラス。
*   `value_denom`: 金額の分母 (JPYは1)

### currencies
*   **役割**: 通貨情報を管理。
*   `guid`: 一意なID
*   `mnemonic`: 通貨コード (例: "JPY")
*   `fraction`: 小数点以下の桁数 (JPYは100) -- 本DBでは歴史的経緯により100で管理されているため。

---

## 2. CSVデータとのマッピング定義

住信SBIネット銀行の入出金明細CSV（`nyushukinmeisai_*.csv`）の項目とデータベースカラムの対応。

| CSV項目 (日本語) | CSV項目 (推定英語) | DBテーブル.カラム | 備考 |
| :--- | :--- | :--- | :--- |
| **日付** | `date` | `transactions.post_date` | 日付形式を `YYYY-MM-DD` に変換 |
| **内容** | `description` | `transactions.description` | |
| **出金金額(円)** | `withdrawal` | `splits.value_num` (Expense Debit / Bank Credit) | 出金の場合に金額として使用 |
| **入金金額(円)** | `deposit` | `splits.value_num` (Bank Debit / Income Credit) | 入金の場合に金額として使用 |
| **残高(円)** | `balance` | (ofx_fitid生成に使用) | ユニークID生成の補助として使用 |
| **メモ** | `memo` | (未使用、必要に応じて拡張) | 現在は取り込みなし |

### ofx_fitidの生成ロジック

`import_dneobank.py` では、取引のユニーク性を保証し、重複インポートを避けるために `ofx_fitid` を生成する。

```python
def generate_fitid(row):
    raw_str = f"DNEOBANK:{row['date']}:{row['description']}:{row['withdrawal']}:{row['deposit']}:{row['balance']}"
    return 'SHA256:' + hashlib.sha256(raw_str.encode()).hexdigest()
```

---

## 3. インポート処理フロー

`py/importers/import_dneobank.py` スクリプトによるインポート処理の概要:

1.  **CSV読み込み**: `guess_encoding` で文字コードを推測（主に `cp932` を想定）し、`pandas.read_csv` で読み込む。
2.  **カラム名正規化**: CSVヘッダーの日本語名を英語の内部カラム名にマッピング。
    *   `日付` → `date`
    *   `内容` → `description`
    *   `出金金額(円)` → `withdrawal`
    *   `入金金額(円)` → `deposit`
    *   `残高(円)` → `balance`
3.  **データ変換**:
    *   `date` カラムを `YYYY-MM-DD` 形式に変換。
    *   `withdrawal`, `deposit`, `balance` カラムをカンマ除去後、整数に変換。
4.  **`ofx_fitid` 生成**: 各行から一意なハッシュIDを生成し、`ofx_fitid` カラムとして追加。
5.  **重複除外**: `ofx_fitid` に基づいて、CSVファイル内の重複行を除外。
6.  **データベース接続**: `config/settings.json` から `db_path` を取得し、SQLiteデータベースに接続。
7.  **勘定科目・通貨の取得/作成**:
    *   `Assets:Bank:SBI Sumishin Net Bank` (タイプ: `ASSET`, OFXタイプ: `BANK`)
    *   `Expenses:Uncategorized` (タイプ: `EXPENSE`)
    *   `Income:Uncategorized` (タイプ: `INCOME`)
    *   `JPY` 通貨
    *   これらの勘定科目や通貨がDBに存在しない場合、自動的に作成される。
8.  **取引インポート**: 各行について以下の処理を実行。
    *   既存の `ofx_fitid` と重複しない場合のみ処理を続行。
    *   新しい `transaction` を `transactions` テーブルに挿入。
    *   取引の種類（入金または出金）に応じて2つの `split` を `splits` テーブルに挿入（複式簿記）。
        *   **入金**: `SBI Sumishin Net Bank` (Debit +金額), `Income:Uncategorized` (Credit -金額)
        *   **出金**: `Expenses:Uncategorized` (Debit +金額), `SBI Sumishin Net Bank` (Credit -金額)
    *   各取引はデータベーストランザクション内でコミットされる。

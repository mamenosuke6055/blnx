# Spec_RakutenSec_Import

楽天証券のCSVデータ（CP932/Shift-JISエンコーディング）を、OFX準拠の統合データベース（`finance.db`）に取り込むためのデータ構造とマッピング仕様。

既存の `init_db.py` で定義されたテーブル構造（複式簿記 + OFX拡張）をベースとする。

## 1. データベース構造（スキーマ）

以下の4つのテーブルを使用して、投資取引を完全な複式簿記形式で記録する。

### accounts
*   **役割**: 口座（楽天証券口座）および **個々の保有銘柄（ファンド・株式）** を管理。
*   **ポイント**: 銘柄ごとに1つの「勘定科目（アカウント）」を作成。
*   `guid`: 一意なID
*   `name`: 銘柄名（例: "eMAXIS Slim 米国株式(S&P500)", "トヨタ自動車"）
*   `account_type`: "ASSET" (資産)
*   `ofx_type`: "INVESTMENT" (投資)
*   `parent_guid`: 楽天証券の親アカウント（例: `Assets:Investments:RakutenSec`）のID
*   `code`: 銘柄コード（例: "7203", "SP500"）

### transactions
*   **役割**: 取引の基本情報（日付、説明）を記録。
*   `post_date`: 約定日
*   `description`: 取引概要（例: "買い eMAXIS Slim..."）

### investment_transactions (OFX拡張)
*   **役割**: 投資特有の情報を記録。
*   `tx_guid`: `transactions` テーブルへのリンク
*   `security_guid`: 銘柄（`accounts` テーブル）へのリンク
*   `type`: 取引種類 (`BUY`: 買付, `SELL`: 売却, `DIV`: 配当, `REINVEST`: 再投資)
*   `units`: 数量（株数・口数）
*   `unit_price`: 単価
*   `total_amount`: 受渡金額
*   `commission`: 手数料

### splits
*   **役割**: 資金の動き（複式簿記の仕訳）を記録。
*   **買付の場合**:
    *   行1 (Debit): 銘柄勘定（資産増）
    *   行2 (Credit): 預り金勘定（資産減）または 銀行勘定

---

## 2. CSVデータとのマッピング定義

### A. 投資信託 (`tradehistory(INVST)_*.csv`)

| CSV項目 (推定) | DBテーブル.カラム | 備考 |
| :--- | :--- | :--- |
| **約定日** | `transactions.post_date`<br>`investment_transactions.trade_date` | 日付形式を `YYYY-MM-DD` に変換 |
| **受渡日** | `investment_transactions.settle_date` | |
| **ファンド名** | `accounts.name` (照合用)<br>`investment_transactions.security_guid` | マスタにない場合は新規作成 |
| **売買** (買付/解約) | `investment_transactions.type` | "買付"→`BUY`, "解約"→`SELL` |
| **数量** (口数) | `investment_transactions.units` | 正の値で登録 |
| **単価** (基準価額) | `investment_transactions.unit_price` | |
| **受渡金額** | `investment_transactions.total_amount` | |
| **手数料/税金** | `investment_transactions.commission` | 手数料 + 税金の合計 |

### B. 国内株式 (`tradehistory(JP)_*.csv`)

| CSV項目 (推定) | DBテーブル.カラム | 備考 |
| :--- | :--- | :--- |
| **約定日** | `transactions.post_date` | |
| **銘柄コード** | `accounts.code` | |
| **銘柄名** | `accounts.name` | |
| **売買** (現物買/売) | `investment_transactions.type` | "現物買"→`BUY`, "現物売"→`SELL` |
| **数量** (株数) | `investment_transactions.units` | |
| **単価** | `investment_transactions.unit_price` | |
| **受渡金額** | `investment_transactions.total_amount` | |

### C. 入出金・配当 (`Withdrawallist_*.csv`)

*   **入金/出金**: `transactions` と `splits` のみで記録（`investment_transactions` は作成しない）。
    *   資金移動: 銀行口座 ⇔ 証券口座(預り金)
*   **配当金**: 摘要に「配当」が含まれる場合。
    *   `investment_transactions`: `type` = `DIV`
    *   `splits`: (Debit) 預り金 / (Credit) 配当収入(`Income:Dividend`)

---

## 3. インポート処理フロー

1.  **CSV読み込み**: `pandas.read_csv(..., encoding='cp932')` でShift-JISとして読み込む。
2.  **銘柄マスタ登録**: 取引履歴からユニークな銘柄リストを抽出し、`accounts` テーブルに不足分を登録。
3.  **取引登録**: 各行をパースし、`transactions` -> `investment_transactions` -> `splits` の順にインサート。

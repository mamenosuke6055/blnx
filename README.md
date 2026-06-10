# blnx

日本の金融機関からエクスポートした CSV を、**複式簿記の SQLite データベース**に取り込む個人ファイナンスのデータ配管です。家計から投資まですべての資産移動をひとつの `finance.db` に正確に記録し、貸借対照表（BS）・キャッシュフロー計算書（CF）・月次レポートを生成します。

## なにをするもの

- 銀行・カード・証券 CSV の自動判別取込（`data/` にファイルを置くだけ）
- 複式仕訳での記録（カード利用と引き落としの Liability/Expense 振り分け、FITID による重複防止）
- 辞書＋機械学習によるカテゴリ分類、手動ラベリング GUI
- BS / CF / 月次支出レポート、Streamlit のインタラクティブダッシュボード
- 投資取引・保有スナップショットの追跡（NISA・特定口座）
- 金融機関 API へは接続しないオフライン設計（CSV エクスポートが入力のすべて）

## 対応金融機関

| ソース | 対応フォーマット |
|---|---|
| 楽天銀行 | 入出金明細 CSV |
| 楽天カード | e-NAVI 利用明細 CSV（新旧フォーマット・重複検知対応） |
| 楽天証券 | 入出金・調整履歴・投信/国内株取引履歴・資産残高 |
| 住信SBIネット銀行 | 入出金明細 CSV |
| SBI証券 | 円貨/外貨入出金・約定履歴・投信保有スナップショット・SaveFile |
| りそな銀行 | 全銀協系 21 列明細 |

ここに無い金融機関の importer 追加は大歓迎です → [CONTRIBUTING.md](CONTRIBUTING.md)

## クイックスタート

[uv](https://docs.astral.sh/uv/) を使います。

```bash
# 依存のインストール
uv sync

# 1. データベースの初期化
uv run python py/init/init_db.py

# 2. 同梱のサンプル CSV で試運転（架空データ）
mkdir -p data
cp examples/inbox/*.csv data/
uv run python scripts/run_import.py inbox

# 3. 貸借対照表を見る
uv run python scripts/run_balance_sheet.py
```

実際の CSV を使うときは、金融機関からエクスポートしたファイルを同じように
`data/` に置くだけです（ファイル名と内容から取込元を自動判別します）。
`data/` と `db/` はバージョン管理されません。

### 個人設定（任意）

銀行入金の分類（本人名義振込の自己資金移動扱い・給与振込元の判定）に使う
個人固有の名義は、コードではなく設定ファイルに置きます:

```bash
cp config/classify_local.sample.json config/classify_local.json
# 自分の名義に書き換える（このファイルはバージョン管理外）
```

## CSV の配置先

```text
data/                  # 直下にまとめて置くだけで自動判別インポート（推奨）
└── raw/               # 金融機関別ディレクトリ（個別インポート用）
    ├── rakuten_bank/      # 楽天銀行
    ├── rakuten_card/      # 楽天カード
    ├── rakuten_sec/       # 楽天証券（assetbalance・tradehistory）
    ├── dneobank/          # 住信SBIネット銀行
    ├── portfolio/         # 資産残高スナップショット
    └── sbi_sec/           # SBI証券（ファイル監視対象）
```

SBI証券は手動ダウンロードが必要なため、ファイル監視モードを用意しています。

```bash
uv run python scripts/watch_sbi_sec.py          # 置いたら自動インポート（常駐）
uv run python scripts/watch_sbi_sec.py --once   # 既存ファイルを一括処理して終了
```

## 主要コマンド

```bash
# インポート
uv run python scripts/run_import.py inbox         # data/ 直下の CSV を自動判別インポート（推奨）
uv run python scripts/run_import.py all           # 全データ（SBI証券除く）
uv run python scripts/run_import_status.py        # 取込状況（ウォーターマーク）

# 分類
uv run python scripts/run_categorize.py           # 辞書ベース自動分類
uv run python scripts/run_ai_training.py          # ML分類モデルの学習
uv run python scripts/run_ai_prediction.py        # ML分類の実行
uv run python scripts/run_manual_labeling.py      # 手動ラベリングGUI

# 財務諸表・レポート
uv run python scripts/run_balance_sheet.py        # 貸借対照表（資産・負債・純資産）
uv run python scripts/run_cashflow_statement.py   # キャッシュフロー計算書
uv run python scripts/run_monthly_report.py       # 月次支出グラフ
uv run python scripts/run_interactive_report.py   # インタラクティブダッシュボード
uv run python scripts/run_detect_subscriptions.py # 定期支出（サブスク）の検出
```

## ディレクトリ構成

```text
py/
├── init/          データベース初期化
├── importers/     CSVインポーター（金融機関ごと）＋自動判別取込
├── processing/    後処理（入金分類・重複除去・カテゴリ紐付け）
├── ai/            ML分類・LLMルーター
├── analysis/      財務諸表・レポート生成
├── gui/           Streamlit / PyQt5 GUI
└── exporters/     OFXエクスポート

scripts/           薄いランチャースクリプト
examples/inbox/    試運転用のサンプルCSV（架空データ）
config/            設定（settings.json / classify_local.sample.json）
docs/              設計ドキュメント
data/, db/         実データ（バージョン管理外）
```

## データベーススキーマ

複式簿記モデルを採用しています。

| テーブル | 内容 |
|---|---|
| `accounts` | 勘定科目ツリー（ASSET / LIABILITY / INCOME / EXPENSE / EQUITY） |
| `transactions` | 取引ヘッダー（日付・摘要・FITID） |
| `splits` | 仕訳明細（複式の各エントリ） |
| `investment_transactions` | 投資取引（BUY / SELL / REINVEST / DIV） |
| `asset_snapshots` | 資産評価額スナップショット |
| `sbi_fund_snapshots` | 投資信託保有スナップショット |
| `currencies` / `exchange_rates` | 通貨マスタ・為替レート履歴 |

`finance.db` は**読まれる側**として設計されています。外部ツールは read-only で参照し、
書き込みは blnx の importer / processing だけが行います。

## 開発

```bash
uv run pytest    # テスト（in-memory/tmp SQLite、実データ不要）
```

貢献の手引きと PII 規律（実データをテストに貼らない）は [CONTRIBUTING.md](CONTRIBUTING.md) を参照してください。

## ライセンス

[MIT](LICENSE)

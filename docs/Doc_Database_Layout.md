# データベース構成 — db/ 配下の 3 つの SQLite

blnx は役割の異なる 3 つの SQLite データベースを `db/` 配下に置く。パスは
`config/settings.json` で変更できる。

| ファイル | 役割 | 初期化 |
|---|---|---|
| `db/finance.db` | 複式簿記の本体（勘定科目・取引・仕訳・投資取引・為替） | `py/init/init_db.py` |
| `db/dictionary.db` | カテゴリ分類辞書（摘要 → カテゴリの対応） | `py/init/init_dictionary_schema.py` |
| `db/raw_imports.db` | 取込 CSV の生アーカイブ（バイト列のまま） | 初回取込時に自動作成 |
| `db/documents.db` | 契約書類・明細の原本保管（仕訳に入らない参照資料） | 初回 `blnx docs add` 時に自動作成 |

いずれも実データのため `db/` ごとバージョン管理外。

## finance.db — 複式簿記の本体

すべての資産移動を記録する唯一の帳簿。スキーマの一覧は README の
「データベーススキーマ」、仕訳モデルの設計思想は
[`Doc_Database_Design_Policy.md`](Doc_Database_Design_Policy.md) を参照。

**読まれる側**として設計されており、書き込みは blnx の importer / processing
だけが行う。外部ツールは read-only で参照する。

## dictionary.db — カテゴリ分類辞書

摘要文字列からカテゴリへの対応を蓄積する辞書。

- `category_dict_manual` — カテゴリマスタ（main_category × sub_category）
- `category_dict` — description → カテゴリの対応

手動ラベリング GUI（`blnx manual-label`）で育て、辞書ベース分類
（`blnx categorize`）が参照する。

**finance.db と分けている理由**: 分類辞書は取引データそのものではなく、
**蓄積された分類判断の資産**だから。finance.db を再初期化して CSV を取り込み
直しても、辞書はそのまま使い続けられる。

## raw_imports.db — 生 CSV アーカイブ

金融機関からダウンロードした CSV を、文字コード無加工（cp932 等そのまま）の
BLOB として保存する（実装は `py/importers/raw_archive.py`）。

役割:

- 同一ファイルの再取込を sha256 で冪等にスキップする
- importer を修正した後、再ダウンロードせずアーカイブから取込をやり直せる
  （リプレイ: `blnx replay`）
- いつ・何を・何行取り込んだかの来歴を永続記録する（`imported_at` / `new_rows` /
  `period_start` / `period_end`）

**finance.db と分けている理由**: 本体の複式簿記 DB を生 BLOB で肥大化させない
ため。アーカイブはサイズが単調増加するが、帳簿側のバックアップや配布の軽さを
損なわない。

**簿記の計算に使う CSV はここが正本**（2026-08-03 確定）。importer 未対応の
ファイルも `imported_at IS NULL`＝「DL 済み・未取込」として置けるので、対応後に
そのままリプレイできる。仕訳に入らない書類は documents.db へ。

## documents.db — 契約書類・明細の原本保管

マイページ等から手動ダウンロードした書類を、バイト列を改変せず content-addressed
で保管する（実装は `py/documents/store.py`、CLI は `blnx docs`）。

- `blobs` … 実体。同一内容は 1 回だけ保存（sha256 で dedup）
- `documents` … 取り込みログ。1 取り込み = 1 行で append-only（同じ内容を再取得
  しても追記する ─ 「この日に取得した」こと自体が記録）

役割:

- 契約書・約款・重要事項説明書・検針明細・登記事項証明書など、**読むために取って
  おくが仕訳には入らない**ものの原本を残す
- 元ファイルが散逸しても復元でき、「いつ・どこから・何を取得したか」を索引化する

**raw_imports.db との分担**: 簿記の計算に使うもの（finance.db への取込対象・
取込候補）は raw_imports.db が正本で、ここには入れない。両者は「仕訳に入るか
否か」で分かれる。

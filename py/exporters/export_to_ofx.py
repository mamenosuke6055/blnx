import sqlite3
import json
from pathlib import Path
from datetime import datetime

def get_project_root() -> Path:
    """プロジェクトのルートディレクトリを取得します。"""
    return Path(__file__).resolve().parent.parent.parent

def export_to_ofx(db_path: Path, output_file: Path):
    """
    finance.dbのデータをOFX形式でエクスポートします。
    これはスケルトン実装であり、OFXの仕様に合わせて詳細を実装する必要があります。
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # OFXヘッダーの生成
        ofx_content = [
            "OFXHEADER:100",
            "DATA:OFXSGML",
            "VERSION:102",
            "SECURITY:NONE",
            "ENCODING:UTF-8",
            "CHARSET:NONE",
            "COMPRESSION:NONE",
            "OLDFILEUID:NONE",
            "NEWFILEUID:NONE",
            "<OFX>",
            "  <SIGNONMSGSRQV1>",
            "    <SONRQ>",
            f"      <DTCLIENT>{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "      <USERID>your_user_id", # ユーザーIDは仮
            "      <USERPASS>your_password", # パスワードは仮
            "      <LANGUAGE>JPN",
            "      <FI>",
            "        <ORG>FinanceProject",
            "        <FID>12345", # 金融機関IDは仮
            "      </FI>",
            "      <APPID>FinanceApp",
            "      <APPVER>1000",
            "    </SONRQ>",
            "  </SIGNONMSGSRQV1>",
            "  <BANKMSGSRQV1>",
            "    <STMTTRNRQ>",
            "      <TRNUID>1",
            "      <STMTRQ>",
            "        <BANKACCTFROM>",
            "          <BANKID>your_bank_id", # 銀行IDは仮
            "          <ACCTID>your_account_id", # 口座IDは仮
            "          <ACCTTYPE>CHECKING", # 口座タイプは仮
            "        </BANKACCTFROM>",
            "        <ALLPOSTEDTRN>",
            "          <DTSTART>19000101", # 開始日は仮
            "          <DTEND>21001231", # 終了日は仮
            "        </ALLPOSTEDTRN>",
            "      </STMTRQ>",
            "    </STMTTRNRQ>",
            "  </BANKMSGSRQV1>",
            "  <INVSTMTMSGSRQV1>", # 投資口座のセクションを追加
            "    <INVSTMTTRNRQ>",
            "      <TRNUID>2",
            "      <INVSTMTREQ>",
            "        <INVACCTFROM>",
            "          <BROKERID>your_broker_id", # 証券会社IDは仮
            "          <ACCTID>your_investment_account_id", # 投資口座IDは仮
            "        </INVACCTFROM>",
            "        <INCTRAN>",
            "          <DTSTART>19000101", # 開始日は仮
            "          <DTEND>21001231", # 終了日は仮
            "        </INCTRAN>",
            "      </INVSTMTREQ>",
            "    </INVSTMTTRNRQ>",
            "  </INVSTMTMSGSRQV1>",
            "</OFX>"
        ]

        # ここにfinance.dbからデータを取得し、OFXトランザクションを生成するロジックを追加
        # 例:
        # cursor.execute("SELECT ... FROM transactions JOIN splits ON ...")
        # for row in cursor.fetchall():
        #     ofx_content.insert(OFX_TRANSACTION_INSERT_POINT, generate_ofx_transaction(row))

        with open(output_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(ofx_content))
        print(f"OFXデータを '{output_file}' にエクスポートしました。")

    except sqlite3.Error as e:
        print(f"データベースエラー: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    PROJECT_ROOT = get_project_root()
    CONFIG_FILE = PROJECT_ROOT / "config/settings.json"
    
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            settings = json.load(f)
        db_path = settings.get("db_path")
        if not db_path:
            print(f"エラー: db_pathが設定ファイルに見つかりません。")
        else:
            output_dir = PROJECT_ROOT / "data" / "ofx_exports"
            output_dir.mkdir(parents=True, exist_ok=True)
            output_file = output_dir / f"finance_export_{datetime.now().strftime('%Y%m%d')}.ofx"
            export_to_ofx(PROJECT_ROOT / db_path, output_file)
    except FileNotFoundError:
        print(f"エラー: 設定ファイル '{CONFIG_FILE}' が見つかりません。")
    except Exception as e:
        print(f"予期せぬエラーが発生しました: {e}")
import pandas as pd
import sqlite3
import joblib
from pathlib import Path
from sentence_transformers import SentenceTransformer
import numpy as np

# --- 定数定義 ---
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DB_PATH = BASE_DIR / "db" / "finance.db"
MODEL_DIR = BASE_DIR / "py" / "ai" / "models"
MODEL_FILE_MAIN = MODEL_DIR / "category_model_main.joblib"
MODEL_FILE_SUB = MODEL_DIR / "category_model_sub.joblib"
EMBEDDING_MODEL_NAME = 'distiluse-base-multilingual-cased-v1'

def predict_categories():
    """
    学習済みモデルを読み込み、未分類の取引データのカテゴリを予測し、
    データベースを更新する。
    """
    print("カテゴリ予測を開始します...")

    # --- 1. 学習済みモデルの読み込み ---
    try:
        model_main = joblib.load(MODEL_FILE_MAIN)
        model_sub = joblib.load(MODEL_FILE_SUB)
        print("学習済みモデルを正常に読み込みました。")
    except FileNotFoundError:
        print("学習済みモデルファイルが見つかりません。")
        print("先に `train_model.py` を実行してモデルを学習・保存してください。")
        return
    except Exception as e:
        print(f"モデルの読み込み中にエラーが発生しました: {e}")
        return

    # --- 2. データベースから予測対象のデータを読み込み ---
    try:
        with sqlite3.connect(DB_PATH) as conn:
            # `ai_category_guid` が NULL の取引を予測対象とする
            query = """
            SELECT guid, description
            FROM transactions
            WHERE ai_category_guid IS NULL AND description IS NOT NULL;
            """
            df_predict = pd.read_sql_query(query, conn, index_col='guid')

        if df_predict.empty:
            print("予測対象のデータが見つかりません。処理を終了します。")
            return
        print(f"{len(df_predict)}件の予測対象データを読み込みました。")

    except Exception as e:
        print(f"データベースからのデータ読み込み中にエラーが発生しました: {e}")
        return

    # --- 3. 特徴量生成 ---
    print(f"埋め込みモデル '{EMBEDDING_MODEL_NAME}' を使用して特徴量を生成します。")
    embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    X_predict = embedding_model.encode(df_predict['description'].tolist(), show_progress_bar=True)

    # --- 4. カテゴリ予測 ---
    print("カテゴリを予測中...")
    try:
        predicted_main_categories = model_main.predict(X_predict)
        predicted_sub_categories = model_sub.predict(X_predict)
        # 予測確率も取得可能
        # predicted_proba_main = model_main.predict_proba(X_predict)
        # predicted_proba_sub = model_sub.predict_proba(X_predict)
        print("予測が完了しました。")
    except Exception as e:
        print(f"カテゴリ予測中にエラーが発生しました: {e}")
        return

    df_predict['predicted_main'] = predicted_main_categories
    df_predict['predicted_sub'] = predicted_sub_categories

    # --- 5. 予測結果をデータベースに反映 ---
    print("データベースを更新中...")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            # 予測結果から `accounts` テーブルの `guid` を取得
            for guid, row in df_predict.iterrows():
                # Sub-categoryのaccount_guidを検索
                cursor.execute(
                    "SELECT guid FROM accounts WHERE name = ? AND (account_type = 'EXPENSE' OR account_type = 'INCOME')",
                    (row['predicted_sub'],)
                )
                result = cursor.fetchone()
                
                if result:
                    account_guid = result[0]
                    # `transactions` テーブルの `ai_category_guid` を更新
                    cursor.execute(
                        "UPDATE transactions SET ai_category_guid = ? WHERE guid = ?",
                        (account_guid, guid)
                    )
                else:
                    print(f"警告: 予測されたカテゴリ '{row['predicted_sub']}' が `accounts` テーブルに見つかりません。スキップします。")
            
            conn.commit()
        print(f"{len(df_predict)}件の取引についてカテゴリ予測結果をデータベースに反映しました。")

    except Exception as e:
        print(f"データベース更新中にエラーが発生しました: {e}")

    print("カテゴリ予測処理が完了しました。")

if __name__ == "__main__":
    predict_categories()

import pandas as pd
import sqlite3
import joblib
from pathlib import Path
from sentence_transformers import SentenceTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

# --- 定数定義 ---
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DB_PATH = BASE_DIR / "db" / "finance.db"
MODEL_DIR = BASE_DIR / "py" / "ai" / "models"
MODEL_FILE_MAIN = MODEL_DIR / "category_model_main.joblib"
MODEL_FILE_SUB = MODEL_DIR / "category_model_sub.joblib"
EMBEDDING_MODEL_NAME = 'distiluse-base-multilingual-cased-v1'

def train_and_save_model():
    """
    データベースから教師データを読み込み、カテゴリ分類モデルを学習して保存する。
    - `main_category` と `sub_category` それぞれのモデルを学習する。
    - 特徴量には `sentence-transformers` を利用する。
    """
    print("モデルの学習を開始します...")

    # モデル保存ディレクトリの作成
    MODEL_DIR.mkdir(exist_ok=True)

    # --- 1. データベースから教師データを読み込み ---
    try:
        with sqlite3.connect(DB_PATH) as conn:
            # `transactions` テーブルと `accounts` テーブルを結合し、
            # `manual_category` が設定されている取引を教師データとする
            query = """
            SELECT
                t.description,
                a_main.name AS main_category,
                a_sub.name AS sub_category
            FROM transactions t
            JOIN accounts a_sub ON t.manual_category_guid = a_sub.guid
            JOIN accounts a_main ON a_sub.parent_guid = a_main.guid
            WHERE
                t.manual_category_guid IS NOT NULL
                AND t.description IS NOT NULL
                AND a_main.name IS NOT NULL
                AND a_sub.name IS NOT NULL;
            """
            df_train = pd.read_sql_query(query, conn)

        if df_train.empty:
            print("教師データが見つかりません。学習を中止します。")
            return
        print(f"{len(df_train)}件の教師データを読み込みました。")

    except Exception as e:
        print(f"データベースからのデータ読み込み中にエラーが発生しました: {e}")
        return

    # --- 2. 特徴量エンジニアリングとモデル学習 ---
    print(f"埋め込みモデル '{EMBEDDING_MODEL_NAME}' を使用して特徴量を生成します。")
    
    # SentenceTransformerモデルの準備
    embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    
    # 説明文をベクトルに変換
    X_train = embedding_model.encode(df_train['description'].tolist(), show_progress_bar=True)
    
    # --- 3. `main_category` のモデルを学習 ---
    y_main = df_train['main_category']
    
    # パイプラインの作成 (スケーラー + ロジスティック回帰)
    pipeline_main = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, random_state=42))
    
    print("'main_category' モデルを学習中...")
    pipeline_main.fit(X_train, y_main)
    print("学習が完了しました。")

    # --- 4. `sub_category` のモデルを学習 ---
    y_sub = df_train['sub_category']
    
    pipeline_sub = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, random_state=42))
    
    print("'sub_category' モデルを学習中...")
    pipeline_sub.fit(X_train, y_sub)
    print("学習が完了しました。")

    # --- 5. 学習済みモデルと埋め込みモデルを保存 ---
    try:
        joblib.dump(pipeline_main, MODEL_FILE_MAIN)
        print(f"Main categoryモデルを '{MODEL_FILE_MAIN}' に保存しました。")
        
        joblib.dump(pipeline_sub, MODEL_FILE_SUB)
        print(f"Sub categoryモデルを '{MODEL_FILE_SUB}' に保存しました。")
        
        # Note: SentenceTransformerモデルは都度読み込むため、保存は不要
        
    except Exception as e:
        print(f"モデルの保存中にエラーが発生しました: {e}")

    print("モデルの学習と保存が完了しました。")

if __name__ == "__main__":
    train_and_save_model()

"""
LLMルーティング層

クラウド上位モデルとローカルLLMの振り分けを一元管理する。

フェーズ6: LLMによるインテリジェント分類
"""
from pathlib import Path
import json
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_FILE = BASE_DIR / "config" / "settings.json"

# デフォルト設定
DEFAULT_CONFIDENCE_THRESHOLD = 0.7
DEFAULT_LOCAL_MODEL = "qwen2.5:14b"
DEFAULT_LOCAL_BASE_URL = "http://localhost:11434"


def load_llm_config() -> dict:
    """
    LLM関連の設定を読み込む。

    Returns:
        dict: {
            'local_model': str,
            'local_base_url': str,
            'confidence_threshold': float,
            'cloud_api_key': str or None,
            'cloud_model': str or None,
        }
    """
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            settings = json.load(f)
    except FileNotFoundError:
        settings = {}

    return {
        'local_model': settings.get('local_model', DEFAULT_LOCAL_MODEL),
        'local_base_url': settings.get('local_base_url', DEFAULT_LOCAL_BASE_URL),
        'confidence_threshold': settings.get('confidence_threshold', DEFAULT_CONFIDENCE_THRESHOLD),
        'cloud_api_key': settings.get('cloud_api_key'),
        'cloud_model': settings.get('cloud_model'),
    }


def query_local(prompt: str, model: str = None) -> dict:
    """
    ローカルLLM (ollama) にクエリを送信する。

    Args:
        prompt: プロンプト
        model: モデル名。省略時は設定値。

    Returns:
        dict: {'response': str, 'confidence': float}
    """
    # TODO: ollama REST API (POST /api/generate) を呼び出し
    # TODO: レスポンスからconfidenceを抽出
    raise NotImplementedError


def query_cloud(prompt: str) -> dict:
    """
    クラウド上位モデルにクエリを送信する。

    Args:
        prompt: プロンプト

    Returns:
        dict: {'response': str, 'confidence': float}
    """
    # TODO: API呼び出し
    # TODO: プライバシーフィルタ適用後のプロンプトを送信
    raise NotImplementedError


def filter_pii(text: str) -> str:
    """
    個人特定可能な情報を除外する。

    Args:
        text: フィルタ対象テキスト

    Returns:
        str: フィルタ済みテキスト
    """
    # TODO: 口座番号、氏名等のパターンをマスク
    raise NotImplementedError


def route_query(prompt: str) -> dict:
    """
    ローカルLLMにクエリし、confidence が閾値未満ならクラウドにエスカレーションする。

    Args:
        prompt: プロンプト

    Returns:
        dict: {'response': str, 'confidence': float, 'source': 'local' | 'cloud'}
    """
    config = load_llm_config()

    local_result = query_local(prompt, config['local_model'])
    local_result['source'] = 'local'

    if local_result['confidence'] >= config['confidence_threshold']:
        return local_result

    logging.info("confidence が閾値未満のためクラウドにエスカレーションします")
    filtered_prompt = filter_pii(prompt)
    cloud_result = query_cloud(filtered_prompt)
    cloud_result['source'] = 'cloud'
    return cloud_result

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# pytest 起動時に site-packages の `py.py` (legacy pytest dep) が
# `sys.modules['py']` を占有している。これを除去しないとローカルの
# `py/` パッケージが解決できない。
sys.modules.pop("py", None)

from py.init.init_db import create_finance_tables  # noqa: E402


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    create_finance_tables(c)
    c.commit()
    yield c
    c.close()

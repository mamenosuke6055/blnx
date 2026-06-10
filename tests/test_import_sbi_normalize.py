"""SBI 証券 SaveFile_*.csv 銘柄名(fund_name)正規化のテスト。

新旧 CSV 形式で同じ取引が異なる銘柄名表記になる問題（fitid 衝突によらない重複登録）
への対処を検証する。詳細は fd 0280bc08。
"""
from py.importers.import_sbi_sec import _normalize_fund_name, _FUND_NAME_ALIAS


class TestNasdaq100Alias:
    """NISSAY NASDAQ100 は新旧で銘柄名表記が違う唯一のケース。"""

    OLD = "ニッセイＮＡＳＤＡＱ１００インデ＜購入・換金手数料なし＞"
    NEW = "ニッセイＮＡＳＤＡＱ１００インデックスファンド＜購入・換金手数料なし＞"

    def test_new_to_old(self):
        # 新形式の長い名前 → 旧形式の短い名前 (DB 既存形式)
        assert _normalize_fund_name(self.NEW) == self.OLD

    def test_old_unchanged(self):
        # 旧形式は変わらない (恒等)
        assert _normalize_fund_name(self.OLD) == self.OLD

    def test_whitespace_stripped(self):
        # 前後空白は strip され、辞書マッピングが効く
        assert _normalize_fund_name(f"  {self.NEW}  ") == self.OLD


class TestUnchangedFunds:
    """新旧で同表記の銘柄は変化しない。"""

    def test_fang_plus(self):
        # 全角スペース(　)込みで同表記
        name = "ｉＦｒｅｅＮＥＸＴ　ＦＡＮＧ＋インデックス"
        assert _normalize_fund_name(name) == name

    def test_sbi_japan_high_dividend(self):
        name = "ＳＢＩ日本高配当株式（分配）ファンド（年４回決算型）"
        assert _normalize_fund_name(name) == name

    def test_sbi_s_us_high_dividend(self):
        # 217件CSV にのみ存在する銘柄、新旧マッピングなし
        name = "ＳＢＩ・Ｓ・米国高配当株式ファンド（年４回決算型）"
        assert _normalize_fund_name(name) == name


class TestEdgeCases:
    def test_empty(self):
        assert _normalize_fund_name("") == ""

    def test_none(self):
        # pandas のセル値が NaN の場合への defensive
        assert _normalize_fund_name(None) == ""

    def test_alias_dict_has_known_mapping(self):
        # 正規化辞書に NASDAQ100 が登録されていること（辞書追加忘れ防止）
        assert TestNasdaq100Alias.NEW in _FUND_NAME_ALIAS
        assert _FUND_NAME_ALIAS[TestNasdaq100Alias.NEW] == TestNasdaq100Alias.OLD

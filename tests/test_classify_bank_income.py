"""py/processing/classify_bank_income.py の分類ルールのテスト。

実データに現れた摘要パターンを**匿名化した代表例**で、表記揺れ（全角/半角カナ）と
評価順（利息 > 給与）を検証する。個人固有キーワード（本人名義・給与振込元）は
コードでなく設定から注入される設計のため、テストもサンプル名義を明示注入する
（実行環境の config/classify_local.json に依存しない）。
"""
from py.processing.classify_bank_income import build_rules, classify_income

# サンプル名義（config/classify_local.sample.json と同じ値）
RULES = build_rules(
    owner_transfer_names=("ヤマダ", "ﾔﾏﾀﾞ"),
    salary_payer_names=("タナカ",),
)


def _classify(description):
    return classify_income(description, rules=RULES)


class TestTransfer:
    """自己資金移動 → Assets:Transfer。収入から外す本タスクの本体。"""

    def test_honnin_meigi_zenkaku(self):
        # 全角カナの本人名義振込
        assert _classify("ヤマダ　タロウ").category == "TRANSFER"

    def test_honnin_meigi_hankaku(self):
        # 半角カナの本人名義振込（最頻出パターン）
        assert _classify("振込　ﾔﾏﾀﾞ ﾀﾛｳ").category == "TRANSFER"

    def test_toushin_genkinka(self):
        # 投信現金化の振込（証券 → 銀行、本人名義）
        assert _classify("振込＊ヤマダ　タロウ").category == "TRANSFER"

    def test_jidou_sweep(self):
        assert _classify(
            "ラクテンショウケンカブシキガイシャ （自動スイ－プ）"
        ).category == "TRANSFER"

    def test_teigaku_jidou_nyukin(self):
        assert _classify("振込＊テイガクジドウニユウキン").category == "TRANSFER"

    def test_atm_genkin_nyukin(self):
        # ATM 現金入金（長音符が半角ハイフン「－」）
        assert _classify(
            "カ－ド入金　セブン銀行0034012345678"
        ).category == "TRANSFER"

    def test_furikomi_kummodoshi(self):
        assert _classify(
            "他行振込組戻（組戻事由：入金不能）　管理番号：20220"
        ).category == "TRANSFER"

    def test_transfer_account_path(self):
        klass = _classify("振込　ﾔﾏﾀﾞ ﾀﾛｳ")
        assert klass.account_path == ("Assets", "Transfer")
        assert klass.account_type == "ASSET"


class TestSalary:
    """給与・賞与 → Income:Salary。"""

    def test_kyuyo(self):
        assert _classify("給与　タナカ　ジロウ").category == "SALARY"

    def test_shoyo(self):
        assert _classify("賞与　タナカ　ジロウ").category == "SALARY"

    def test_meigi_only_no_kyuyo_word(self):
        # 「給与」の語がなく振込名義のみでも給与振込元なら SALARY
        assert _classify("タナカ　ジロウ").category == "SALARY"

    def test_kana_kyuyo(self):
        # 摘要側がカナ表記「キユウヨ」のパターン（一般語彙でマッチ）
        assert _classify("給与　サンプルマチ　キユウヨ").category == "SALARY"

    def test_salary_account_path(self):
        klass = _classify("給与　タナカ　ジロウ")
        assert klass.account_path == ("Income", "Salary")
        assert klass.account_type == "INCOME"


class TestInterest:
    """利息・金利 → Income:Interest。"""

    def test_risoku(self):
        assert _classify("利息").category == "INTEREST"

    def test_yokin_risoku(self):
        assert _classify("預金利息").category == "INTEREST"

    def test_kinri_risoku_beats_salary(self):
        # 「給与」を含むが実体は優遇金利 → 利息を優先（評価順の検証）
        assert _classify(
            "給与・賞与・年金受取ボ－ナス金利利息"
        ).category == "INTEREST"

    def test_interest_account_path(self):
        klass = _classify("利息")
        assert klass.account_path == ("Income", "Interest")
        assert klass.account_type == "INCOME"


class TestUnknownReturnsNone:
    """未知の摘要（グレー）は None。インポーターは Uncategorized に保留する。"""

    def test_tax_refund(self):
        assert _classify("振込＊サンプルゼイムシヨ") is None

    def test_insurance(self):
        assert _classify("Ｋホケンキン　サンプルホケン") is None

    def test_unknown_organization(self):
        assert _classify("サンプルキヨウカイ") is None

    def test_other_person(self):
        assert _classify("スズキ　イチロウ") is None

    def test_kotora_soukin(self):
        assert _classify("ことら送金　スズキ　イチロウ （20260215") is None

    def test_card_word_alone_is_not_transfer(self):
        # 「カード」単独は ATM 入金「カ－ド入金」とは別物 → 未知
        assert _classify("カード") is None

    def test_empty(self):
        assert _classify("") is None

    def test_none(self):
        assert _classify(None) is None


class TestDefaultRulesWithoutLocalConfig:
    """設定ファイル無しでも一般語彙のみで動作する（名義系はマッチしない）。"""

    def test_general_vocab_works(self):
        assert classify_income("利息", rules=build_rules()).category == "INTEREST"
        assert classify_income("給与　振込", rules=build_rules()).category == "SALARY"

    def test_names_unknown_without_config(self):
        assert classify_income("ヤマダ　タロウ", rules=build_rules()) is None

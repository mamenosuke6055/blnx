"""py/processing/classify_bank_outflow.py の分類ルールのテスト。

実データに現れた摘要パターンを**匿名化した代表例**で検証する。個人固有キーワード
（本人名義・目的別口座の呼び名・同姓の別人）はコードでなく設定から注入される設計の
ため、テストもサンプル名義を明示注入する（実行環境の config/classify_local.json に
依存しない）。

このテストの本体は **同姓の別人を費用のまま残すこと**。owner_transfer_names は姓の
一致で当たるので、除外リストが先に評価されないと家族への送金が自己振替に化け、
出金側では真の支出が P/L から消える（入金側と誤りの向きが逆になる）。
"""
from py.processing.classify_bank_outflow import build_rules, classify_outflow

# サンプル名義（config/classify_local.sample.json と同じ値）
RULES = build_rules(
    owner_transfer_names=("ヤマダ", "ﾔﾏﾀﾞ"),
    self_account_names=("生活防衛",),
    transfer_exclude_names=("ハナコ",),
)


def _classify(description):
    return classify_outflow(description, rules=RULES)


class TestSelfMove:
    """自己口座間の移動 → Assets:Transfer。費用から外す本タスクの本体。"""

    def test_shouken_furikae(self):
        # 住信SBI → SBI証券（日次積立の振替。実データで最頻の 149 件）
        assert _classify("振替　ＳＢＩ証券").category == "TRANSFER"

    def test_shouken_furikae_with_reference_number(self):
        # 参照番号が後続する表記揺れ
        assert _classify("振替　ＳＢＩ証券 （20260616021359324）").category == "TRANSFER"

    def test_hybrid_deposit(self):
        assert _classify("ＳＢＩハイブリッド預金").category == "TRANSFER"

    def test_daihyou_kouza(self):
        assert _classify("普通　代表口座").category == "TRANSFER"
        assert _classify("普通　円　代表口座").category == "TRANSFER"
        assert _classify("普通　米ドル　代表口座").category == "TRANSFER"

    def test_mokutekibetsu_kouza_comes_from_config(self):
        # 目的別口座の呼び名は本人が付けたもの → 設定から注入される
        assert _classify("普通　生活防衛").category == "TRANSFER"

    def test_honnin_meigi_soukin(self):
        # ことら送金・他行振込の本人名義（自分の別口座への移動）
        assert _classify("ことら送金　ヤマダ　タロウ （20260624014538024528）").category == "TRANSFER"
        assert _classify("振込＊ﾔﾏﾀﾞ ﾀﾛｳ").category == "TRANSFER"

    def test_returns_asset_account(self):
        k = _classify("振替　ＳＢＩ証券")
        assert k.account_path == ("Assets", "Transfer")
        assert k.account_type == "ASSET"


class TestExcludedSameSurname:
    """同姓の別人は費用のまま。入金側ルールの無条件流用を禁じる回帰テスト。"""

    def test_family_member_is_not_self_transfer(self):
        assert _classify("振込＊ヤマダ　ハナコ") is None

    def test_exclusion_is_evaluated_before_owner_name(self):
        # 姓(ヤマダ)も名(ハナコ)も含む摘要で、除外が勝つこと
        assert _classify("振込＊ヤマダ　ハナコ") is None
        # 本人は従来どおり TRANSFER のまま（除外が広がりすぎていない）
        assert _classify("振込＊ヤマダ　タロウ").category == "TRANSFER"


class TestUnknownStaysExpense:
    """判定できない摘要は None = Expenses:Uncategorized に保留（人間レビュー）。"""

    def test_debt_repayment_is_not_reclassified(self):
        # 奨学金返済は負債の減少だが、負債口座と開始残高が決まるまで費用のまま
        assert _classify("返済金　ﾆﾎﾝｶﾞｸｾｲｼｴﾝｷ") is None

    def test_atm_withdrawal_is_not_reclassified(self):
        # 手元現金の口座が無いので閉じられない。費用のまま残す
        assert _classify("ＡＴＭ　セブン銀行") is None

    def test_ordinary_expense(self):
        assert _classify("水道　ｽｲﾄﾞｳﾘﾖｳｷﾝﾄｳ") is None
        assert _classify("口座振替　ＡＩＧソンポ") is None

    def test_empty(self):
        assert _classify("") is None
        assert _classify(None) is None


class TestNoConfigIsSafe:
    """設定ファイルが無くても一般語彙だけで動く（個人名がコードに無いことの裏返し）。"""

    def test_generic_only(self):
        rules = build_rules()
        assert classify_outflow("振替　ＳＢＩ証券", rules=rules).category == "TRANSFER"
        assert classify_outflow("振込＊ヤマダ　タロウ", rules=rules) is None

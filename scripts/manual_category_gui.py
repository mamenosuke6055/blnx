import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
from datetime import date
from lib_manual_category import (
    get_uncategorized_transactions,
    get_categories,
    get_category_parents,
    assign_manual_category,
    assign_category_to_all_matching_descriptions,
    create_category_under,
)

# --- Streamlit 設定 ---
st.set_page_config(layout="wide", page_title="教師データ作成ツール")
st.title("教師データ作成ツール (手動カテゴリ割り当て)")

# --- サイドバー: 新規カテゴリ作成 ---
with st.sidebar:
    st.header("新規カテゴリ作成")
    st.caption("支出(EXPENSE)の親カテゴリの下に子カテゴリを作成します")
    parents = get_category_parents("EXPENSE")
    parent_options = {p["name"]: p["guid"] for p in parents}
    sel_parent = st.selectbox("親カテゴリ", list(parent_options.keys()))
    new_cat_name = st.text_input("子カテゴリ名を入力", placeholder="例: ライブ")

    if st.button("カテゴリを追加"):
        if new_cat_name and sel_parent:
            if create_category_under(parent_options[sel_parent], new_cat_name):
                st.success(f"作成完了: {sel_parent} > {new_cat_name}")
                st.rerun()
            else:
                st.error("作成に失敗しました（既に存在するか、エラーが発生しました）")
        else:
            st.warning("名前を入力してください")

# --- メインエリア: フィルタ設定 ---
use_date_filter = st.checkbox("年月でフィルタする", value=False)
if use_date_filter:
    col_d1, col_d2 = st.columns(2)
    with col_d1:
        year = st.number_input("年", min_value=2000, max_value=2100, value=date.today().year)
    with col_d2:
        month = st.number_input("月", min_value=1, max_value=12, value=date.today().month)
    filter_pattern = f"{year}-{month:02d}%"
else:
    filter_pattern = "%"

# --- データ読み込み ---
if 'uncategorized_df' not in st.session_state or st.sidebar.button("データを再読み込み"):
    st.session_state.uncategorized_df = get_uncategorized_transactions(filter_pattern)
    st.session_state.categories_df = get_categories()
    st.session_state.current_index = 0

df = st.session_state.uncategorized_df
cats = st.session_state.categories_df
index = st.session_state.current_index

if not df.empty and index < len(df):
    current = df.iloc[index]
    desc_count = int(current.get('description_count', 1))
    desc_total = int(current.get('description_total', current['amount']))

    # ヘッダー
    st.header(f"未分類取引 ({index+1} / {len(df)})")

    # 取引情報
    col_info, col_badge = st.columns([3, 1])
    with col_info:
        info_df = current[['post_date', 'description', 'amount']].to_frame().T
        info_df['amount'] = info_df['amount'].map('{:,.0f} 円'.format)
        st.table(info_df.set_index('post_date'))
    with col_badge:
        if desc_count > 1:
            st.metric("同じ摘要", f"{desc_count} 件", f"計 {desc_total:,} 円")
        else:
            st.metric("金額", f"{int(current['amount']):,} 円")

    st.subheader("カテゴリを割り当て")
    c1, c2 = st.columns(2)
    with c1:
        main_list = sorted(cats['main_category'].unique())
        sel_main = st.selectbox("メインカテゴリ (親)", main_list)
    with c2:
        sub_list = sorted(cats[cats['main_category'] == sel_main]['sub_category'].unique())
        sel_sub = st.selectbox("サブカテゴリ (子)", sub_list)

    cat_guid = cats[(cats['main_category'] == sel_main) & (cats['sub_category'] == sel_sub)].iloc[0]['guid']

    # アクションボタン
    b0, b1, b2, b3 = st.columns([1, 2, 3, 1])
    with b0:
        if st.button("← 前へ", use_container_width=True, disabled=(index == 0)):
            st.session_state.current_index = max(0, index - 1)
            st.rerun()
    with b1:
        if st.button("この取引に適用", type="primary", use_container_width=True):
            if assign_manual_category(current['guid'], cat_guid):
                st.session_state.uncategorized_df = df.drop(df.index[index]).reset_index(drop=True)
                st.session_state.current_index = min(index, len(st.session_state.uncategorized_df) - 1)
                st.rerun()
    with b2:
        bulk_label = f"同じ摘要 {desc_count} 件すべてに適用 (計 {desc_total:,} 円)" if desc_count > 1 else f"同じ摘要「{current['description']}」すべてに適用"
        if st.button(bulk_label, use_container_width=True):
            desc = current['description']
            updated = assign_category_to_all_matching_descriptions(desc, cat_guid)
            new_df = df[df['description'] != desc].reset_index(drop=True)
            st.session_state.uncategorized_df = new_df
            st.session_state.current_index = min(index, max(len(new_df) - 1, 0))
            st.toast(f"{updated} 件を更新しました")
            st.rerun()
    with b3:
        if st.button("スキップ →", use_container_width=True):
            st.session_state.current_index = min(index + 1, len(df) - 1)
            st.rerun()

    components.html("""
<script>
(function() {
    if (window.parent._blnxNavKeyListenerAdded) return;
    window.parent._blnxNavKeyListenerAdded = true;
    window.parent.document.addEventListener('keydown', function(e) {
        var active = window.parent.document.activeElement;
        if (active && ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName)) return;
        if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
        e.preventDefault();
        var target = e.key === 'ArrowLeft' ? '← 前へ' : 'スキップ →';
        var btns = window.parent.document.querySelectorAll('button');
        for (var i = 0; i < btns.length; i++) {
            if (btns[i].innerText.trim() === target) {
                btns[i].click();
                break;
            }
        }
    });
})();
</script>
""", height=0)
else:
    st.success("すべての取引が分類済みです！")
    if st.button("もう一度読み込む"):
        st.session_state.pop('uncategorized_df')
        st.rerun()
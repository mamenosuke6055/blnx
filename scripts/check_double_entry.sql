-- ============================================================
-- finance.db 二重計上・不変条件チェック（読み取り専用 / 破壊的操作なし）
-- 使い方: sqlite3 -box db/finance.db < scripts/check_double_entry.sql
--
-- 背景と各検査の根拠は fossil technote [289c50d8ac]
-- 「調査記録: 二重計上の構造的原因 = 冪等キーが importer ごとに別定義」(2026-09-05)
--
-- 注意: 借貸一致(INV-1)は本件の二重計上を検出できない。二重計上された仕訳は
-- 1件1件が正しい複式仕訳として成立しているため。検出は DUP-* が担う。
-- ============================================================

.print '=== [INV-1] 借貸不一致 (Σvalue ≠ 0) ==='
SELECT COUNT(*) AS unbalanced_tx FROM (
  SELECT tx_guid FROM splits GROUP BY tx_guid
  HAVING ABS(SUM(value_num*1.0/value_denom)) > 0.0001);

.print '=== [INV-2] 片側のみ / 孤児ヘッダ / 親不在split ==='
SELECT
  (SELECT COUNT(*) FROM (SELECT tx_guid FROM splits GROUP BY tx_guid HAVING COUNT(*)=1)) AS single_leg,
  (SELECT COUNT(*) FROM transactions t WHERE NOT EXISTS (SELECT 1 FROM splits s WHERE s.tx_guid=t.guid)) AS orphan_header,
  (SELECT COUNT(*) FROM splits s WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.guid=s.tx_guid)) AS orphan_split;

.print '=== [DUP-1] 冪等キー欠落 (fitid が NULL/空) = 再取込ですり抜ける母集団 ==='
SELECT CASE WHEN ofx_fitid IS NULL THEN 'NULL' WHEN ofx_fitid='' THEN 'EMPTY' ELSE 'set' END AS state,
       COUNT(*) n, MIN(post_date) mn, MAX(post_date) mx
FROM transactions GROUP BY state;

.print '=== [DUP-1b] fitid 欠落の明細（手動の決算仕訳か、importer の取りこぼしかを目視判定） ==='
SELECT t.guid, t.post_date, substr(t.enter_date,1,19) batch, substr(t.description,1,40) d
FROM transactions t WHERE t.ofx_fitid IS NULL OR t.ofx_fitid='' ORDER BY t.post_date;

.print '=== [DUP-2] 同一口座・同日・同額・同摘要の重複（batches>1 が再取込由来） ==='
WITH x AS (
  SELECT s.account_guid, a.name acct, t.post_date, t.description,
         s.value_num*1.0/s.value_denom amt, substr(t.enter_date,1,19) batch
  FROM splits s JOIN transactions t ON t.guid=s.tx_guid JOIN accounts a ON a.guid=s.account_guid
  WHERE a.account_type IN ('ASSET','LIABILITY'))
SELECT acct, post_date, description, amt, COUNT(*) n, COUNT(DISTINCT batch) batches
FROM x GROUP BY account_guid, post_date, description, amt HAVING COUNT(*)>1
ORDER BY batches DESC, n DESC;

.print '=== [DUP-3] 同日・同摘要・同額のヘッダ重複（口座跨ぎ含む・広い網） ==='
WITH tx AS (
  SELECT t.guid, t.post_date, t.description,
         (SELECT SUM(ABS(s.value_num*1.0/s.value_denom)) FROM splits s WHERE s.tx_guid=t.guid) absamt
  FROM transactions t)
SELECT COUNT(*) dup_groups, SUM(n) tx_in_groups, SUM(n-1) excess_tx
FROM (SELECT COUNT(*) n FROM tx GROUP BY post_date, description, absamt HAVING COUNT(*)>1);

.print '=== [DUP-4] 住信SBI 口座混入: 同一取引がハイブリッド預金側と代表口座側の両方に ==='
WITH h AS (SELECT t.post_date, MAX(ABS(s.value_num*1.0/s.value_denom)) amt FROM transactions t
           JOIN splits s ON s.tx_guid=t.guid JOIN accounts a ON a.guid=s.account_guid
           WHERE a.name='SBI Sumishin Hybrid Deposit' GROUP BY t.guid),
     n AS (SELECT t.post_date, MAX(ABS(s.value_num*1.0/s.value_denom)) amt FROM transactions t
           JOIN splits s ON s.tx_guid=t.guid JOIN accounts a ON a.guid=s.account_guid
           WHERE a.name='SBI Sumishin Net Bank' GROUP BY t.guid)
SELECT (SELECT COUNT(*) FROM h) hybrid_side,
       (SELECT COUNT(*) FROM n) netbank_side,
       (SELECT COUNT(*) FROM h JOIN n ON h.post_date=n.post_date AND h.amt=n.amt) matched_pairs,
       (SELECT SUM(h.amt) FROM h JOIN n ON h.post_date=n.post_date AND h.amt=n.amt) double_counted_yen;

.print '=== [DUP-5] 内部振替が費用(Expenses:Uncategorized)に化けている額（月次） ==='
SELECT strftime('%Y-%m',t.post_date) ym, COUNT(*) n, SUM(s.value_num*1.0/s.value_denom) yen
FROM transactions t JOIN splits s ON s.tx_guid=t.guid JOIN accounts a ON a.guid=s.account_guid
WHERE t.description LIKE '振替%' AND a.name='Uncategorized' AND a.account_type='EXPENSE'
GROUP BY ym ORDER BY ym;

.print '=== [DUP-6] 円建て口座に小数残高 = 別通貨/別口座の混入シグナル ==='
SELECT a.name, a.account_type, COUNT(s.guid) n_splits,
       SUM(s.value_num*1.0/s.value_denom) balance
FROM accounts a JOIN splits s ON s.account_guid=a.guid
WHERE a.account_type IN ('ASSET','LIABILITY')
GROUP BY a.guid HAVING ABS(balance - CAST(balance AS INTEGER)) > 0.0001
ORDER BY a.name;

.print '=== [SMP] 直近20件の仕訳サンプル ==='
SELECT t.post_date, substr(t.description,1,30) d, substr(t.enter_date,1,19) batch,
       CASE WHEN t.ofx_fitid IS NULL THEN '(no-fitid)' ELSE substr(t.ofx_fitid,8,10) END fit,
       (SELECT group_concat(a.name||' '||CAST(s.value_num AS TEXT),' | ')
        FROM splits s JOIN accounts a ON a.guid=s.account_guid WHERE s.tx_guid=t.guid) legs
FROM transactions t ORDER BY t.enter_date DESC, t.post_date DESC LIMIT 20;

---
name: consolidation
description: Financial consolidation — multi-entity rollup, intercompany elimination, currency translation, CTA, NCI/minority interest, segment reporting.
tags: [finance, consolidation, intercompany, elimination, currency, translation, cta, nci, minority-interest, segment, multi-entity]
---

# Financial Consolidation Skill

## When to Use This Skill

Use this skill when the user asks for:
- Multi-entity consolidation or rollup
- Intercompany elimination
- Currency translation (current rate or temporal method)
- Cumulative Translation Adjustment (CTA)
- Non-controlling interest (NCI / minority interest) calculation
- Segment reporting or breakdowns
- Reconciling subsidiary data to consolidated totals

## 1. Intercompany Elimination Types

| Category | Entity A (Debit) | Entity B (Credit) | Elimination |
|---|---|---|---|
| Revenue/COGS | IC Revenue | IC Purchases | DR IC Revenue, CR IC COGS |
| AR/AP | IC Receivable | IC Payable | DR IC AP, CR IC AR |
| Loans | Notes Receivable | Notes Payable | DR Notes Payable, CR Notes Receivable |
| Interest | Interest Income | Interest Expense | DR Interest Income, CR Interest Expense |
| Dividends | Dividend Income | Dividends Declared | DR Dividend Income, CR Dividends |
| Management Fees | Fee Income | Fee Expense | DR Fee Income, CR Fee Expense |

### Unrealized Profit in Inventory

When goods sold between entities remain unsold at period-end:
```
Unrealized Profit = Unsold Inventory Value x (Markup / (100 + Markup))
Elimination: DR COGS, CR Inventory
```

### IC Matching Heuristics for Uploaded Spreadsheets

1. Look for columns containing "intercompany", "IC", "related party"
2. Find offsetting amounts across entities (same amount, same period, opposite sign)
3. Pair IC Revenue with IC COGS; IC AR with IC AP
4. Allow tolerance for FX-driven differences (< 1%)

## 2. Currency Translation Methods

### Current Rate Method (autonomous subsidiary)

| Line Item | Rate |
|---|---|
| All assets & liabilities | Closing rate |
| Common stock | Historical rate |
| Revenue & Expenses | Average rate |

Translation gain/loss -> OCI (equity). Does NOT hit P&L.

### Temporal Method (integrated extension of parent)

| Line Item | Rate |
|---|---|
| Monetary items (cash, AR, AP) | Current rate |
| Non-monetary items (inventory at cost, PP&E) | Historical rate |
| Revenue & most expenses | Average rate |
| COGS, Depreciation | Historical rate |

Remeasurement gain/loss -> Net Income (P&L).

**Default to Current Rate Method unless user specifies otherwise.**

## 3. CTA (Cumulative Translation Adjustment)

CTA is the balancing residual from applying different rates to different line items:

```
CTA = Net Assets at Closing Rate
    - (Equity at Historical Rates + NI at Average Rate - Dividends at Historical Rate)
```

- Accumulates in AOCI (equity) each period
- Reclassified to P&L only upon disposal of the foreign entity

## 4. Consolidation Workflow

```
1. Collect entity-level trial balances (each sheet = one entity)
2. Map local chart of accounts -> group COA
3. Apply currency translation
4. Aggregate (SUM all entities)
5. Identify & post intercompany eliminations
6. Post top-side adjustments (goodwill, fair value)
7. Calculate NCI allocation
8. Produce consolidated statements
9. Validate (TB nets to zero, IC accounts net to zero)
```

### SQL: Map & Aggregate

```sql
-- Map local accounts to group COA and translate
SELECT cm.group_account_code, cm.group_account_name, cm.account_type,
    SUM(CASE
        WHEN cm.account_type IN ('ASSET','LIABILITY') THEN (tb.debit - tb.credit) * fx_close.rate
        WHEN cm.account_type = 'EQUITY' THEN (tb.debit - tb.credit) * fx_hist.rate
        WHEN cm.account_type IN ('REVENUE','EXPENSE') THEN (tb.debit - tb.credit) * fx_avg.rate
    END) AS translated_amount
FROM s_trial_balance tb
JOIN coa_mapping cm ON tb.entity_id = cm.entity_id AND tb.account_code = cm.local_account_code
LEFT JOIN fx_rates fx_close ON tb.currency = fx_close.from_currency AND fx_close.rate_type = 'CLOSING'
LEFT JOIN fx_rates fx_avg ON tb.currency = fx_avg.from_currency AND fx_avg.rate_type = 'AVERAGE'
LEFT JOIN fx_rates fx_hist ON tb.currency = fx_hist.from_currency AND fx_hist.rate_type = 'HISTORICAL'
GROUP BY cm.group_account_code, cm.group_account_name, cm.account_type;
```

### SQL: IC Pair Detection

```sql
SELECT a.entity_id AS entity_a, b.entity_id AS entity_b,
    a.translated_amount AS amount_a, b.translated_amount AS amount_b,
    ABS(a.translated_amount + b.translated_amount) AS mismatch,
    CASE
        WHEN ABS(a.translated_amount + b.translated_amount) < 0.01 THEN 'EXACT'
        WHEN ABS(a.translated_amount + b.translated_amount) / GREATEST(ABS(a.translated_amount), 1) < 0.01 THEN 'FX_TOLERANCE'
        ELSE 'MISMATCH'
    END AS match_status
FROM v_ic_balances a
JOIN v_ic_balances b ON a.ic_partner_id = b.entity_id AND b.ic_partner_id = a.entity_id
WHERE a.entity_id < b.entity_id;
```

## 5. NCI (Non-Controlling Interest)

When parent owns < 100% of a subsidiary:

```
NCI Income = Subsidiary Net Income x (1 - Ownership %)
NCI Equity = Subsidiary Total Equity x (1 - Ownership %)
NCI_ending = NCI_opening + NCI_Income - NCI_Dividends + NCI_OCI
```

100% of subsidiary is consolidated. NCI line shows what belongs to outside shareholders.

## 6. Consolidation Method by Ownership

| Ownership | Relationship | Method |
|---|---|---|
| > 50% (control) | Subsidiary | Full consolidation |
| 20-50% (influence) | Associate | Equity method |
| < 20% | Investment | Fair value / cost |

## 7. Segment Reporting

Segments identified by how the CODM reviews results. Reportable if ANY:
- Revenue >= 10% of total segment revenue
- Absolute profit/loss >= 10% of the greater of total profits or total losses
- Assets >= 10% of total segment assets

Coverage rule: Reportable segments must cover >= 75% of consolidated external revenue.

## 8. Validation Checks

After consolidation, verify:
- Consolidated trial balance nets to zero
- All IC-flagged accounts net to zero post-elimination
- NCI balances = ownership% x subsidiary equity
- Segment totals + eliminations = consolidated totals
- CTA reconciles

## Common Pitfalls

- **Mismatched COA**: Entities use different account codes. Maintain a mapping table and flag unmapped accounts.
- **Timing differences**: Entity A records IC sale in March, Entity B records purchase in April. Compare with tolerance.
- **FX rate inconsistency**: Centralize rates in one table. Validate all required rate types exist for each currency/period.
- **Incomplete elimination**: IC management fees and transfer pricing adjustments often slip through. Tag all IC accounts in COA.
- **Wrong method**: Don't full-consolidate a 30%-owned associate. Use equity method for 20-50% influence.
- **Multi-level hierarchy**: Sub-subsidiaries require bottom-up consolidation. Process leaf entities first.
- **Investment elimination**: Parent's "Investment in Sub" account must offset against subsidiary equity at acquisition.

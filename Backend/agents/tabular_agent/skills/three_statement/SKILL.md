---
name: three-statement
description: Three-statement financial analysis — P&L, Balance Sheet, Cash Flow linkages, common-size analysis, cross-statement validation, DuPont decomposition.
tags: [finance, three-statement, pnl, balance-sheet, cash-flow, dupont, common-size, horizontal-analysis]
---

# Three-Statement Financial Analysis Skill

## When to Use This Skill

Use this skill when the user asks for:
- Analysis linking income statement, balance sheet, and cash flow statement
- Common-size (vertical) analysis — each line as % of revenue or total assets
- Horizontal analysis — period-over-period changes, trend indexing, CAGR
- Cross-statement validation or consistency checks
- DuPont decomposition of ROE
- Building or auditing a three-statement model

## 1. Statement Linkages

### Income Statement -> Cash Flow Statement (Indirect Method)

```
Operating Cash Flow =
    Net Income
  + Depreciation & Amortization
  + Stock-Based Compensation
  + Impairment Charges / Write-downs
  + Deferred Tax Expense
  - Gains on Asset Sales
  + Losses on Asset Sales
  - Increase in Accounts Receivable
  + Decrease in Accounts Receivable
  - Increase in Inventory
  + Decrease in Inventory
  + Increase in Accounts Payable
  - Decrease in Accounts Payable
  +/- Other working capital changes
```

**Working capital sign rule:**
- Increase in current ASSET -> SUBTRACT (uses cash)
- Decrease in current ASSET -> ADD (frees cash)
- Increase in current LIABILITY -> ADD (source of cash)
- Decrease in current LIABILITY -> SUBTRACT (uses cash)

### Cash Flow Statement -> Balance Sheet

```
BS Cash (end) = BS Cash (begin) + CFO + CFI + CFF
```

### Income Statement -> Balance Sheet

```
Retained Earnings (end) = Retained Earnings (begin) + Net Income - Dividends
```

### Key Cross-Statement Linkages

| Source | Line Item | Target | Line Item |
|---|---|---|---|
| IS | Depreciation Expense | BS | Accumulated depreciation increases |
| IS | Depreciation Expense | CFS | Added back in CFO |
| CFS | CapEx (Investing) | BS | PP&E increases |
| CFS | Debt Issuance | BS | Long-term debt increases |
| CFS | Share Buyback | BS | Treasury stock increases |
| CFS | Dividends Paid | BS | Retained earnings decreases |

**Critical insight:** The CFS is a reconciliation of YoY changes in every Balance Sheet line. If you take every non-cash BS account's change and classify it into Operating/Investing/Financing, the sum must equal the change in cash.

## 2. Common-Size Analysis (Vertical)

### Income Statement: % of Revenue

```sql
WITH revenue AS (
    SELECT period, SUM(CASE WHEN line_item = 'Revenue' THEN amount END) AS rev
    FROM s_income_statement GROUP BY period
)
SELECT f.period, f.line_item, f.amount,
    ROUND(f.amount / NULLIF(r.rev, 0) * 100, 2) AS pct_of_revenue
FROM s_income_statement f
JOIN revenue r ON f.period = r.period
ORDER BY f.period, f.line_item;
```

### Balance Sheet: % of Total Assets

```sql
WITH totals AS (
    SELECT period, SUM(CASE WHEN line_item = 'Total Assets' THEN amount END) AS ta
    FROM s_balance_sheet GROUP BY period
)
SELECT f.period, f.line_item, f.amount,
    ROUND(f.amount / NULLIF(t.ta, 0) * 100, 2) AS pct_of_total_assets
FROM s_balance_sheet f
JOIN totals t ON f.period = t.period
ORDER BY f.period, f.line_item;
```

## 3. Horizontal Analysis (Period-over-Period)

```sql
SELECT
    line_item, period, amount,
    LAG(amount) OVER (PARTITION BY line_item ORDER BY period) AS prior_amount,
    amount - LAG(amount) OVER (PARTITION BY line_item ORDER BY period) AS abs_change,
    CASE WHEN LAG(amount) OVER (PARTITION BY line_item ORDER BY period) = 0 THEN NULL
         ELSE ROUND((amount - LAG(amount) OVER (PARTITION BY line_item ORDER BY period))
              / ABS(LAG(amount) OVER (PARTITION BY line_item ORDER BY period)) * 100, 2)
    END AS pct_change
FROM s_income_statement
ORDER BY line_item, period;
```

### CAGR

```
CAGR = (Ending Value / Beginning Value)^(1/n) - 1
```

## 4. Cross-Statement Validation Checks

Run these on any uploaded financial data to verify consistency:

| # | Check | Tolerance |
|---|---|---|
| 1 | Total Assets = Total Liabilities + Total Equity | Exact |
| 2 | RE_end = RE_begin + Net Income - Dividends | Exact |
| 3 | BS Cash_end = BS Cash_begin + CFO + CFI + CFF | Exact |
| 4 | PP&E_end = PP&E_begin + CapEx - Depreciation - Disposals | Rounding |
| 5 | Debt_end = Debt_begin + New Issuances - Repayments | Rounding |
| 6 | NI positive but OCF negative -> earnings quality flag | Soft |
| 7 | Revenue growth > 30% YoY with no asset growth -> recognition flag | Soft |
| 8 | Depreciation > CapEx for 3+ periods -> under-investment flag | Soft |

```sql
-- Balance sheet balance check
SELECT period,
    MAX(CASE WHEN line_item = 'Total Assets' THEN amount END) AS total_assets,
    MAX(CASE WHEN line_item = 'Total Liabilities' THEN amount END)
    + MAX(CASE WHEN line_item = 'Total Equity' THEN amount END) AS liab_plus_equity,
    MAX(CASE WHEN line_item = 'Total Assets' THEN amount END)
    - (MAX(CASE WHEN line_item = 'Total Liabilities' THEN amount END)
       + MAX(CASE WHEN line_item = 'Total Equity' THEN amount END)) AS imbalance
FROM s_balance_sheet
GROUP BY period
HAVING ABS(imbalance) > 0.01;
```

## 5. DuPont Decomposition

### 3-Factor

```
ROE = (Net Income / Revenue) x (Revenue / Avg Total Assets) x (Avg Total Assets / Avg Equity)
    = Net Margin x Asset Turnover x Equity Multiplier
```

### 5-Factor

```
ROE = (NI/EBT) x (EBT/EBIT) x (EBIT/Revenue) x (Revenue/Avg Assets) x (Avg Assets/Avg Equity)
    = Tax Burden x Interest Burden x EBIT Margin x Asset Turnover x Equity Multiplier
```

| Factor | Improving Means | Risk If Too High |
|---|---|---|
| Tax Burden (NI/EBT) | Lower effective tax rate | Unsustainable tax strategies |
| Interest Burden (EBT/EBIT) | Lower interest costs | Already at limit, rate risk |
| EBIT Margin | Better cost control / pricing power | Margin compression |
| Asset Turnover | More revenue per $ of assets | Asset quality deterioration |
| Equity Multiplier | Leverage amplifying returns | Insolvency risk |

## 6. Analytical Workflow

1. **Identify** which statements are present (IS, BS, CFS, or partial)
2. **Normalize** line item names, sign conventions, periods
3. **Validate** cross-statement checks (balance check, RE roll-forward, cash reconciliation)
4. **Compute** vertical analysis (common-size) for each statement
5. **Compute** horizontal analysis (YoY, QoQ, CAGR) for all line items
6. **Compute** DuPont decomposition (3-factor minimum, 5-factor if data allows)
7. **Flag** edge cases (negative equity, zero denominators, missing data)
8. **Interpret** which DuPont factor drives ROE changes; which margins expand/contract

## Common Pitfalls

- **Division by zero**: Always use `NULLIF(denominator, 0)` — never produce infinity
- **Negative equity**: ROE meaningless when equity < 0 (buyback-heavy companies). Use ROIC instead
- **Snapshot vs flow mismatch**: Use average BS values for ratios like ROE and asset turnover
- **GAAP vs Non-GAAP**: Never mix GAAP numerator with non-GAAP denominator
- **Fiscal year mismatch**: Note FY end dates when comparing across companies
- **Quarterly TTM**: Sum trailing 4 quarters for IS items; BS ratios are point-in-time
- **Non-recurring items**: If NI deviates sharply from operating income, flag non-operating items

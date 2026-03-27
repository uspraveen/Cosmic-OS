---
name: ratio-analysis
description: Financial ratio analysis — liquidity, profitability, leverage, efficiency, valuation ratios with industry benchmarks and Altman Z-Score.
tags: [finance, ratios, liquidity, profitability, leverage, efficiency, valuation, altman, z-score, benchmarks]
---

# Financial Ratio Analysis Skill

## When to Use This Skill

Use this skill when the user asks for:
- Financial ratio computation (current ratio, ROE, D/E, margins, etc.)
- Industry benchmarking or peer comparison
- Credit analysis or distress screening (Z-Score)
- DuPont decomposition (see also three-statement skill)
- Ratio trend analysis across periods

## 1. Liquidity Ratios

| Ratio | Formula | Interpretation |
|---|---|---|
| Current Ratio | Current Assets / Current Liabilities | >1.5 healthy; >3.0 may be inefficient |
| Quick Ratio | (Cash + ST Investments + AR) / Current Liabilities | Strips inventory; >1.0 is threshold |
| Cash Ratio | (Cash + Cash Equivalents) / Current Liabilities | Most conservative; 0.2-0.5 typical |
| Operating CF Ratio | Cash from Operations / Current Liabilities | Best liquidity measure; >1.0 is strong |

## 2. Profitability Ratios

| Ratio | Formula | Notes |
|---|---|---|
| Gross Margin | (Revenue - COGS) / Revenue | SaaS: 70-85%, Manufacturing: 25-40% |
| Operating Margin | EBIT / Revenue | Software: 20-35%, Retail: 3-8% |
| Net Margin | Net Income / Revenue | Most susceptible to one-time items |
| ROE | Net Income / Avg Equity | 15-20%+ is strong; decompose via DuPont |
| ROA | Net Income / Avg Total Assets | Banks: 1-2%, Tech: 10-20% |
| ROIC | NOPAT / Invested Capital | Gold standard (McKinsey). Compare to WACC |

**ROIC detail:**
```
NOPAT = EBIT x (1 - Tax Rate)
Invested Capital = Total Equity + Total Debt - Cash
```

## 3. Leverage Ratios

| Ratio | Formula | Notes |
|---|---|---|
| Debt-to-Equity | Total Debt / Total Equity | <1.0 conservative; utilities 1.5-3.0 |
| Debt-to-Assets | Total Debt / Total Assets | >0.6 is aggressive |
| Interest Coverage | EBIT / Interest Expense | <1.5 distress; >8 very safe |
| DSCR | EBITDA / (Interest + Principal) | >1.25 typical covenant threshold |

## 4. Efficiency Ratios

| Ratio | Formula | Notes |
|---|---|---|
| Asset Turnover | Revenue / Avg Total Assets | Retail: 2-3x, Utilities: 0.3-0.5x |
| Inventory Turnover | COGS / Avg Inventory | Higher = faster sales |
| DSO | 365 / (Revenue / Avg AR) | 30-45 days typical B2B |
| DIO | 365 / (COGS / Avg Inventory) | Rising DIO = overstocking risk |
| DPO | 365 / (COGS / Avg AP) | Higher = holding cash longer |
| Cash Conversion Cycle | DIO + DSO - DPO | Negative = collect before paying (Amazon) |

## 5. Valuation Ratios

| Ratio | Formula | Notes |
|---|---|---|
| P/E | Market Cap / Net Income | S&P median ~16-18x; negative earnings = N/A |
| EV/EBITDA | Enterprise Value / EBITDA | Capital-structure neutral; preferred for M&A |
| P/B | Market Cap / Total Equity | Banks valued on P/B (1.0-1.5x typical) |
| P/S | Market Cap / Revenue | Useful when earnings negative; most abused |

**Enterprise Value:**
```
EV = Market Cap + Total Debt - Cash + Minority Interest + Preferred Stock
```

## 6. Industry Benchmarks

| Ratio | Tech/SaaS | Manufacturing | Retail | Banking | Utilities |
|---|---|---|---|---|---|
| Current Ratio | 2.0-4.0 | 1.5-2.5 | 1.0-1.5 | N/A | 0.5-1.0 |
| Gross Margin | 70-85% | 25-40% | 25-35% | N/A | 40-60% |
| Operating Margin | 20-35% | 8-15% | 3-8% | 30-45% | 15-25% |
| ROE | 15-30% | 10-20% | 15-25% | 8-15% | 8-12% |
| D/E | 0-0.5 | 0.5-1.5 | 0.5-1.5 | 5-12 | 1.0-2.5 |
| P/E | 25-50 | 12-20 | 12-20 | 8-14 | 14-20 |
| EV/EBITDA | 15-25 | 8-12 | 6-10 | N/A | 8-10 |

Note: Banking uses Net Interest Margin, Efficiency Ratio, Tier 1 Capital instead of many standard ratios.

## 7. Altman Z-Score

### Public Manufacturing Firms

```
Z = 1.2*X1 + 1.4*X2 + 3.3*X3 + 0.6*X4 + 1.0*X5
```

| Variable | Formula |
|---|---|
| X1 | Working Capital / Total Assets |
| X2 | Retained Earnings / Total Assets |
| X3 | EBIT / Total Assets |
| X4 | Market Value of Equity / Total Liabilities |
| X5 | Sales / Total Assets |

**Zones:** Safe > 2.99 | Grey 1.81-2.99 | Distress < 1.81

### Private Firms (Z'-Score)

```
Z' = 0.717*X1 + 0.847*X2 + 3.107*X3 + 0.420*X4' + 0.998*X5
```

Where X4' = Book Value of Equity / Total Liabilities. Cutoffs: Safe > 2.90, Distress < 1.23.

### Non-Manufacturing (Z''-Score)

```
Z'' = 6.56*X1 + 3.26*X2 + 6.72*X3 + 1.05*X4'
```

Drops Sales/Assets (industry-dependent). Cutoffs: Safe > 2.60, Distress < 1.10.

## 8. SQL Patterns

```sql
-- Comprehensive ratio computation
WITH pivoted AS (
    SELECT period,
        MAX(CASE WHEN line_item = 'Revenue' THEN amount END) AS revenue,
        MAX(CASE WHEN line_item = 'COGS' THEN amount END) AS cogs,
        MAX(CASE WHEN line_item = 'Net Income' THEN amount END) AS net_income,
        MAX(CASE WHEN line_item = 'EBIT' THEN amount END) AS ebit,
        MAX(CASE WHEN line_item = 'Interest Expense' THEN amount END) AS interest_expense,
        MAX(CASE WHEN line_item = 'Total Assets' THEN amount END) AS total_assets,
        MAX(CASE WHEN line_item = 'Total Current Assets' THEN amount END) AS current_assets,
        MAX(CASE WHEN line_item = 'Total Current Liabilities' THEN amount END) AS current_liabilities,
        MAX(CASE WHEN line_item = 'Total Equity' THEN amount END) AS total_equity,
        MAX(CASE WHEN line_item = 'Total Debt' THEN amount END) AS total_debt,
        MAX(CASE WHEN line_item = 'Inventory' THEN amount END) AS inventory,
        MAX(CASE WHEN line_item = 'Accounts Receivable' THEN amount END) AS ar,
        MAX(CASE WHEN line_item = 'Cash' THEN amount END) AS cash
    FROM s_financials GROUP BY period
),
with_lag AS (
    SELECT *,
        LAG(total_assets) OVER (ORDER BY period) AS prev_assets,
        LAG(total_equity) OVER (ORDER BY period) AS prev_equity
    FROM pivoted
)
SELECT period,
    -- Liquidity
    current_assets / NULLIF(current_liabilities, 0) AS current_ratio,
    (cash + ar) / NULLIF(current_liabilities, 0) AS quick_ratio,
    -- Profitability
    (revenue - cogs) / NULLIF(revenue, 0) AS gross_margin,
    ebit / NULLIF(revenue, 0) AS operating_margin,
    net_income / NULLIF(revenue, 0) AS net_margin,
    CASE WHEN (total_equity + COALESCE(prev_equity, total_equity))/2 > 0
         THEN net_income / ((total_equity + COALESCE(prev_equity, total_equity))/2)
         ELSE NULL END AS roe,
    net_income / NULLIF((total_assets + COALESCE(prev_assets, total_assets))/2, 0) AS roa,
    -- Leverage
    total_debt / NULLIF(total_equity, 0) AS debt_to_equity,
    ebit / NULLIF(interest_expense, 0) AS interest_coverage,
    -- Efficiency
    365.0 / NULLIF(revenue / NULLIF(ar, 0), 0) AS dso,
    365.0 / NULLIF(cogs / NULLIF(inventory, 0), 0) AS dio
FROM with_lag
ORDER BY period;
```

## Common Pitfalls

- **Negative equity**: Suppresses ROE. Use ROIC instead. Flag for user.
- **COGS = 0**: Service/financial companies don't report COGS. Gross margin undefined.
- **One-time items**: Restructuring, impairments crater net income. Flag NI-to-EBIT divergence.
- **Snapshot vs flow**: Always average BS denominators for ROE, ROA, turnover ratios.
- **Stock-based comp**: 10-30% of revenue in tech. Report margins with and without SBC if available.
- **Cash hoarding**: Excess cash inflates invested capital, deflates ROIC. Consider net invested capital.

## Red Flag Detection

Flag any of these automatically:
- Z-Score in distress or grey zone
- Interest coverage below 2.0x
- Negative or rapidly declining operating cash flow
- DSO increasing faster than revenue growth
- D/E above industry norms and rising
- Declining gross margins for 3+ consecutive periods

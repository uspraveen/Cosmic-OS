---
name: expense-analysis
description: Expense and OpEx analysis — run-rate, cost per head, OpEx vs CapEx, vendor concentration, fixed vs variable decomposition, SG&A leverage.
tags: [finance, expense, opex, cost, run-rate, vendor, sga, headcount, budget, cost-center]
---

# Expense & OpEx Analysis Skill

## When to Use This Skill

Use this skill when the user asks for:
- Expense run-rate calculation or annualization
- Cost per employee / fully-loaded headcount cost
- OpEx vs CapEx classification
- Vendor or supplier spend concentration analysis
- Expense variance (budget vs actual by department)
- Fixed vs variable cost decomposition
- SG&A as % of revenue / operating leverage
- T&E (Travel & Entertainment) analysis

## 1. Run-Rate Calculation

### Basic Annualized Run-Rate

```
Annual Run-Rate = (Period Expense / Days in Period) x 365
```

Or for monthly data:
```
Annual Run-Rate = Monthly Expense x 12
Quarterly Run-Rate = Quarterly Expense x 4
```

### Adjusted Run-Rate (preferred)

Strip one-time items before annualizing:
```
Adjusted Run-Rate = (Total Expense - One-Time Items) / Period Months x 12
```

One-time items: restructuring charges, severance, signing bonuses, litigation, one-off consulting projects, asset write-offs.

### SQL: Run-Rate with One-Time Adjustment

```sql
SELECT cost_center,
    SUM(amount) AS ytd_spend,
    SUM(CASE WHEN is_recurring = true THEN amount ELSE 0 END) AS recurring_spend,
    SUM(CASE WHEN is_recurring = false THEN amount ELSE 0 END) AS one_time_spend,
    ROUND(SUM(CASE WHEN is_recurring = true THEN amount ELSE 0 END)
          / COUNT(DISTINCT month) * 12, 2) AS adjusted_annual_run_rate
FROM s_expenses
WHERE fiscal_year = 2025
GROUP BY cost_center
ORDER BY adjusted_annual_run_rate DESC;
```

## 2. Cost Per Employee

### Fully-Loaded Cost

```
Fully-Loaded Cost Per Employee = Total Compensation & Benefits / Average Headcount

Total Comp includes:
  Base salary + Bonus/commission + Stock-based comp
  + Payroll taxes + Health insurance + 401k match
  + Equipment/tools + Facilities allocation + Training
```

### SQL: Cost Per Head by Department

```sql
SELECT department,
    AVG(headcount) AS avg_headcount,
    SUM(total_comp) AS total_compensation,
    ROUND(SUM(total_comp) / NULLIF(AVG(headcount), 0), 0) AS cost_per_head,
    SUM(total_comp) / NULLIF(SUM(revenue), 0) AS comp_to_revenue_pct
FROM s_department_costs
GROUP BY department
ORDER BY cost_per_head DESC;
```

### Revenue Per Employee

```
Revenue Per Employee = Total Revenue / Average FTE Count
```

Benchmark: Tech/SaaS $200K-$500K+. Manufacturing: $150K-$300K. Consulting: $150K-$250K.

## 3. OpEx vs CapEx Classification

| OpEx (Income Statement) | CapEx (Balance Sheet) |
|---|---|
| Rent, utilities | Building purchase/improvement |
| Salaries, wages | Equipment (useful life > 1 year) |
| Software subscriptions (SaaS) | On-premise software licenses |
| Repairs & maintenance (routine) | Major renovations / upgrades |
| Office supplies | Furniture & fixtures (> threshold) |
| Cloud hosting (consumption) | Data center buildout |
| Training | Patent/IP development costs |

**Capitalization threshold**: Most companies set a threshold ($1K-$5K). Items below are expensed regardless of useful life.

**Gray areas**: Internal software development (ASC 350-40: capitalize after feasibility, expense research phase). Leasehold improvements (capitalize, amortize over shorter of lease term or useful life).

## 4. Vendor Concentration Analysis

### Top-N Vendor Spend

```sql
SELECT vendor_name,
    SUM(amount) AS total_spend,
    ROUND(SUM(amount) * 100.0 / SUM(SUM(amount)) OVER (), 1) AS pct_of_total,
    SUM(SUM(amount)) OVER (ORDER BY SUM(amount) DESC) * 100.0
      / SUM(SUM(amount)) OVER () AS cumulative_pct,
    COUNT(DISTINCT invoice_id) AS invoice_count,
    COUNT(DISTINCT category) AS categories
FROM s_ap_detail
GROUP BY vendor_name
ORDER BY total_spend DESC;
```

### Concentration Risk Assessment

| Metric | Healthy | Risky |
|---|---|---|
| Top vendor as % of total spend | < 15% | > 30% |
| Top 5 vendors as % of total | < 40% | > 60% |
| Single-source dependencies | 0 | Any critical category |

### Spend by Category

```sql
SELECT expense_category,
    SUM(amount) AS total_spend,
    ROUND(SUM(amount) * 100.0 / SUM(SUM(amount)) OVER (), 1) AS pct_of_total,
    COUNT(DISTINCT vendor_name) AS vendor_count
FROM s_expenses
GROUP BY expense_category
ORDER BY total_spend DESC;
```

## 5. Budget vs Actual Variance

```sql
SELECT department, line_item,
    SUM(budget_amount) AS budget,
    SUM(actual_amount) AS actual,
    SUM(actual_amount) - SUM(budget_amount) AS variance,
    ROUND((SUM(actual_amount) - SUM(budget_amount)) / NULLIF(SUM(budget_amount), 0) * 100, 1) AS variance_pct,
    CASE
        WHEN SUM(actual_amount) < SUM(budget_amount) THEN 'Favorable'
        WHEN SUM(actual_amount) > SUM(budget_amount) THEN 'Unfavorable'
        ELSE 'On Budget'
    END AS status
FROM s_budget_vs_actual
GROUP BY department, line_item
ORDER BY ABS(SUM(actual_amount) - SUM(budget_amount)) DESC;
```

**Sign convention for expense variance**: Under budget (actual < budget) = Favorable. Over budget = Unfavorable. For revenue it's the opposite.

## 6. Fixed vs Variable Cost Decomposition

### Identification Heuristics

| Fixed Costs | Variable Costs | Semi-Variable |
|---|---|---|
| Rent / leases | Raw materials | Utilities |
| Base salaries | Sales commissions | Shipping / freight |
| Insurance | Payment processing fees | Customer support staff |
| Depreciation | Cloud compute (usage) | Marketing (has fixed + variable) |
| Core SaaS tools | Direct labor (hourly) | Maintenance |

### High-Low Method (from historical data)

```
Variable Rate = (Highest Cost - Lowest Cost) / (Highest Activity - Lowest Activity)
Fixed Component = Total Cost - (Variable Rate x Activity Level)
```

### SQL: Fixed vs Variable Estimate

```sql
WITH cost_data AS (
    SELECT month, SUM(amount) AS total_cost, MAX(revenue) AS revenue
    FROM s_expenses GROUP BY month
),
high_low AS (
    SELECT
        (MAX(total_cost) - MIN(total_cost)) / NULLIF(MAX(revenue) - MIN(revenue), 0) AS variable_rate,
        MIN(total_cost) - (MAX(total_cost) - MIN(total_cost)) / NULLIF(MAX(revenue) - MIN(revenue), 0) * MIN(revenue) AS fixed_cost
    FROM cost_data
)
SELECT cd.month, cd.total_cost, cd.revenue,
    hl.fixed_cost AS estimated_fixed,
    cd.total_cost - hl.fixed_cost AS estimated_variable,
    ROUND(hl.fixed_cost / cd.total_cost * 100, 1) AS fixed_pct
FROM cost_data cd, high_low hl
ORDER BY cd.month;
```

## 7. SG&A Leverage

```
SG&A as % of Revenue = SG&A / Revenue x 100

Operating Leverage = % Change in Operating Income / % Change in Revenue
```

When revenue grows and fixed SG&A stays constant, operating margin improves. This is operating leverage.

### Trend Analysis

```sql
SELECT period,
    revenue,
    sga,
    ROUND(sga / NULLIF(revenue, 0) * 100, 1) AS sga_pct_revenue,
    LAG(ROUND(sga / NULLIF(revenue, 0) * 100, 1)) OVER (ORDER BY period) AS prior_sga_pct,
    ROUND(sga / NULLIF(revenue, 0) * 100, 1)
      - LAG(ROUND(sga / NULLIF(revenue, 0) * 100, 1)) OVER (ORDER BY period) AS sga_leverage_bps
FROM s_pnl_summary
ORDER BY period;
```

## Common Pitfalls

- **Annualizing with one-time items**: Strip restructuring, severance, litigation before computing run-rate
- **Capitalization inconsistency**: Same item expensed in one period, capitalized in another. Check policy.
- **Allocation methodology distortion**: Overhead allocation makes segment costs unreliable. Use direct costs when possible.
- **Headcount timing**: Use average headcount, not period-end. A hire on Dec 31 distorts per-head metrics.
- **Budget reforecasts**: Compare to original budget AND latest forecast separately. Reforecast-to-actual hides original misses.
- **Currency effects**: Multi-currency expense bases need constant-currency analysis to isolate operational changes.
- **Accrual vs cash**: Expense accruals (especially bonus, commission) can differ significantly from cash payment timing.

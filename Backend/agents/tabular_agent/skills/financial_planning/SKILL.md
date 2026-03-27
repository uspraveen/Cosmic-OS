---
name: financial-planning
description: FP&A financial planning — forecasting, scenario modeling, budgetary control.
tags: [finance, fpna, forecast, budget, scenario, planning, modelling]
---

# Financial Planning & Analysis (FP&A) Skill

## When to Use This Skill

Use this skill when the user asks for:
- Forecasting (trend-based, seasonal, driver-based)
- Budget creation or variance-to-budget analysis
-Scenario modeling and sensitivity analysis
- What-if analysis
- Capital expenditure (CapEx) planning
- Working capital projections
- Cash flow forecasting

## Core Concepts

### Forecasting Approaches

1. **Trend-Based**: Simple moving average or linear regression on historical data
2. **Seasonal**: Decompose into trend + seasonality components (e.g., Q4 spikes)
3. **Driver-Based**: Model driven by key metrics (headcount → payroll, revenue per employee → revenue)

### Key FP&A Formulas

- **Compound Annual Growth Rate (CAGR)**: (Ending Value / Beginning Value)^(1/Years) - 1
- **Revenue per Employee**: Total Revenue / FTE Count
- **Operating Margin**: (Operating Income / Revenue) × 100
- **Working Capital**: Current Assets - Current Liabilities
- **DSO (Days Sales Outstanding)**: (Accounts Receivable / Revenue) × Days

### Scenario Modeling Patterns

```python
# Sensitivity Analysis Template
base_case = revenue * margin
scenarios = {
    "optimistic": revenue * 1.1 * margin,
    "pessimistic": revenue * 0.9 * (margin - 0.02),
    "best_case": revenue * 1.15 * (margin + 0.01),
}
```

### Common SQL Patterns

#### Rolling 12-Month Trend
```sql
SELECT 
    period,
    SUM(revenue) OVER (ORDER BY period ROWS BETWEEN 11 PRECEDING AND CURRENT ROW) as rolling_12m
FROM sheet_
ORDER BY period;
```

#### Budget vs Actual by Category
```sql
SELECT 
    category,
    MAX(CASE WHEN type = 'budget' THEN amount END) as budget,
    MAX(CASE WHEN type = 'actual' THEN amount END) as actual,
    MAX(CASE WHEN type = 'actual' THEN amount END) - MAX(CASE WHEN type = 'budget' THEN amount END) as variance
FROM sheet_
GROUP BY category;
```

## Best Practices

1. **Document assumptions**: Every forecast needs explicit assumptions documented
2. **Scenario documentation**: Note which scenario base case vs upside/downside
3. **Bridge building**: Build clear bridges from forecast to prior period
4. **Sensible rounding**: Present millions/thousands, not decimals
5. **Include context**: Add narrative to explain "why this number"

## Avoiding Common Mistakes

- Don't forecast without first Understanding data quality
- Don't ignore one-time events that skew historicals
- Don't overcomplicate models (simpler is often better)
- Don't forget to align totals across statements (PL, BS, CF)
- Don't present without context (what does the number mean?)

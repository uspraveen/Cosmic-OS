---
name: financial-variance
description: Variance analysis — actual vs budget, YoY/MoM/QoQ deltas, bridge decomposition.
tags: [finance, variance, budget, actuals, yoy, mom, qoq]
---

# Financial Variance Analysis Skill

## When to Use This Skill

Use this skill when the user asks for:
- Variance analysis (actual vs budget, forecast, prior period)
- Year-over-year (YoY), Month-over-month (MoM), Quarter-over-quarter (QoQ) comparisons
- Bridge decompositions explaining delivery vs target
- Root cause analysis of financial variances
- Performance commentary on financial metrics

## Core Concepts

### Variance Types
1. **Price Variance**: Difference due to unit price changes
2. **Volume Variance**: Difference due to quantity/volume changes
3. **Mix Variance**: Difference due to product/segment mix shifts
4. **Rate Variance**: Exchange rate or rate-based variances
5. **Timing Variance**: Differences due to timing of transactions

### Variance Cascade
- **Delivery**: Actual - Target (or Forecast)
- **Price Impact**: (Actual Price - Budget Price) × Actual Volume
- **Volume Impact**: (Actual Volume - Budget Volume) × Budget Price
- **Mix Impact**: Sum of (Actual Mix % - Budget Mix %) × Budget Price × Total Volume

### Common Analysis Patterns

#### YoY Comparison
```sql
SELECT 
    period,
    SUM(CASE WHEN period = '2025' THEN revenue END) as actual_revenue,
    SUM(CASE WHEN period = '2024' THEN revenue END) as prior_period_revenue,
    SUM(CASE WHEN period = '2025' THEN revenue END) - SUM(CASE WHEN period = '2024' THEN revenue END) as yoy_delta,
    (SUM(CASE WHEN period = '2025' THEN revenue END) * 1.0 / NULLIF(SUM(CASE WHEN period = '2024' THEN revenue END), 0) - 1) * 100 as yoy_pct
FROM sheet_
GROUP BY period
ORDER BY period;
```

#### Budget Variance Bridge
```sql
SELECT 
    category,
    budget_amount,
    actual_amount,
    actual_amount - budget_amount as variance_abs,
    (actual_amount * 1.0 / NULLIF(budget_amount, 0) - 1) * 100 as variance_pct
FROM sheet_
ORDER BY ABS(actual_amount - budget_amount) DESC;
```

## Output Format

When presenting variance analysis:
1. State the overall variance (absolute and percentage)
2. Break down by main drivers (price, volume, mix)
3. Highlight outliers requiring attention
4. Provide context (seasonality, one-time items, etc.)

## Avoiding Common Mistakes

- Don't confuse sign conventions (positive variance isn't always good)
- Account for currency effects when comparing international entities
- Validate that restated budget vs actual is on comparable basis
- Watch for timing differences that will reverse

---
name: revenue-analytics
description: SaaS and subscription revenue analytics — MRR/ARR movements, cohort retention, NRR/GRR, LTV/CAC, SaaS Quick Ratio, Rule of 40.
tags: [saas, revenue, mrr, arr, churn, retention, cohort, ltv, cac, nrr, subscription]
---

# Revenue & SaaS Analytics Skill

## When to Use This Skill

Use this skill when the user asks for:
- MRR/ARR calculation or waterfall decomposition
- Churn analysis (logo or revenue churn)
- Cohort retention analysis
- Net Revenue Retention (NRR/NDR) or Gross Revenue Retention (GRR)
- LTV, CAC, or LTV:CAC ratio
- SaaS Quick Ratio, Rule of 40, Bessemer Efficiency Score
- Subscription or recurring revenue analysis from billing/payment data

## 1. MRR Movement Classification

Every month-over-month change classifies into exactly one category:

| Movement | Condition |
|---|---|
| **New** | No prior subscription record (first-ever payment) |
| **Expansion** | Existed last month AND curr_mrr > prev_mrr |
| **Contraction** | Existed last month AND curr_mrr < prev_mrr AND curr_mrr > 0 |
| **Churn** | Existed last month AND curr_mrr = 0 |
| **Reactivation** | Had mrr = 0 for 1+ months AND now mrr > 0 again |

```
Net New MRR = New + Reactivation + Expansion - Contraction - Churn
End-of-Month MRR = Beginning MRR + Net New MRR
ARR = MRR * 12
```

### DuckDB SQL: MRR Waterfall (dbt MRR Playbook pattern)

```sql
WITH customer_months AS (
  SELECT customer_id, DATE_TRUNC('month', payment_date) AS month,
    SUM(amount) AS mrr
  FROM s_payments WHERE is_recurring = true GROUP BY 1, 2
),
monthly_mrr AS (
  SELECT s.customer_id, s.month,
    COALESCE(cm.mrr, 0) AS mrr,
    LAG(COALESCE(cm.mrr, 0)) OVER (PARTITION BY s.customer_id ORDER BY s.month) AS prev_mrr,
    BOOL_OR(COALESCE(cm.mrr, 0) > 0) OVER (
      PARTITION BY s.customer_id ORDER BY s.month
      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    ) AS had_prior_revenue
  FROM spine s LEFT JOIN customer_months cm
    ON s.customer_id = cm.customer_id AND s.month = cm.month
),
classified AS (
  SELECT *,
    CASE
      WHEN prev_mrr IS NULL AND mrr > 0 THEN 'new'
      WHEN prev_mrr = 0 AND mrr > 0 AND had_prior_revenue THEN 'reactivation'
      WHEN prev_mrr > 0 AND mrr = 0 THEN 'churn'
      WHEN mrr > prev_mrr THEN 'expansion'
      WHEN mrr < prev_mrr AND mrr > 0 THEN 'contraction'
      ELSE 'no_change'
    END AS movement_type,
    mrr - COALESCE(prev_mrr, 0) AS mrr_change
  FROM monthly_mrr WHERE mrr > 0 OR prev_mrr > 0
)
SELECT month,
  SUM(CASE WHEN movement_type = 'new' THEN mrr_change ELSE 0 END) AS new_mrr,
  SUM(CASE WHEN movement_type = 'expansion' THEN mrr_change ELSE 0 END) AS expansion_mrr,
  SUM(CASE WHEN movement_type = 'reactivation' THEN mrr_change ELSE 0 END) AS reactivation_mrr,
  SUM(CASE WHEN movement_type = 'contraction' THEN mrr_change ELSE 0 END) AS contraction_mrr,
  SUM(CASE WHEN movement_type = 'churn' THEN mrr_change ELSE 0 END) AS churn_mrr,
  SUM(mrr_change) AS net_new_mrr
FROM classified GROUP BY month ORDER BY month;
```

## 2. Cohort Retention Matrix

```sql
WITH first_month AS (
  SELECT customer_id, DATE_TRUNC('month', MIN(start_date)) AS cohort_month
  FROM s_subscriptions GROUP BY customer_id
),
cohort_data AS (
  SELECT f.cohort_month,
    DATEDIFF('month', f.cohort_month, mr.month) AS months_since_signup,
    COUNT(DISTINCT mr.customer_id) AS active_customers,
    SUM(mr.mrr) AS total_mrr
  FROM first_month f JOIN customer_months mr ON f.customer_id = mr.customer_id
  WHERE mr.month >= f.cohort_month GROUP BY 1, 2
),
cohort_sizes AS (
  SELECT cohort_month, active_customers AS cohort_size, total_mrr AS cohort_mrr
  FROM cohort_data WHERE months_since_signup = 0
)
SELECT cd.cohort_month, cd.months_since_signup,
  ROUND(100.0 * cd.active_customers / cs.cohort_size, 1) AS customer_retention_pct,
  ROUND(100.0 * cd.total_mrr / cs.cohort_mrr, 1) AS revenue_retention_pct
FROM cohort_data cd JOIN cohort_sizes cs ON cd.cohort_month = cs.cohort_month
ORDER BY cd.cohort_month, cd.months_since_signup;
```

## 3. Net Revenue Retention (NRR) & Gross Revenue Retention (GRR)

```
NRR = (Starting MRR + Expansion + Reactivation - Contraction - Churn) / Starting MRR
GRR = (Starting MRR - Contraction - Churn) / Starting MRR
```

GRR is always <= 100%. NRR > 100% means negative net churn (expansion exceeds losses).

### Trailing 12-Month NRR (a16z / Bessemer method)

```sql
WITH period AS (
  SELECT customer_id, SUM(mrr) AS mrr_12m_ago
  FROM s_monthly_mrr
  WHERE month = DATE_TRUNC('month', CURRENT_DATE - INTERVAL '12 months') AND mrr > 0
  GROUP BY customer_id
),
current AS (
  SELECT customer_id, SUM(mrr) AS mrr_now
  FROM s_monthly_mrr
  WHERE month = DATE_TRUNC('month', CURRENT_DATE - INTERVAL '1 month')
  GROUP BY customer_id
)
SELECT
  ROUND(100.0 * SUM(COALESCE(c.mrr_now, 0)) / SUM(p.mrr_12m_ago), 1) AS nrr_pct,
  ROUND(100.0 * SUM(LEAST(COALESCE(c.mrr_now, 0), p.mrr_12m_ago)) / SUM(p.mrr_12m_ago), 1) AS grr_pct
FROM period p LEFT JOIN current c ON p.customer_id = c.customer_id;
```

**Benchmarks (Bessemer Cloud 100):** $1-10M ARR: NRR 145%+. $100M+: NRR 120%+. GRR healthy: 85-95%.

## 4. LTV & CAC

### Simple LTV

```
LTV = ARPA * Gross Margin % / Customer Churn Rate
Customer Lifetime = 1 / Churn Rate
```

### Cohort-Based LTV (most accurate)

```sql
SELECT cohort_month,
  SUM(total_mrr) OVER (PARTITION BY cohort_month ORDER BY months_since_signup) / cohort_size
    AS cumulative_ltv
FROM cohort_data JOIN cohort_sizes USING (cohort_month);
```

### CAC & LTV:CAC

```
CAC = Total Sales & Marketing Spend / New Customers Acquired
LTV:CAC ratio: 3:1 is healthy (David Skok). >5:1 may mean under-investing.
CAC Payback (months) = CAC / (ARPA * Gross Margin %)
```

Benchmark: CAC Payback < 12 months is good. > 24 months is a concern.

## 5. SaaS Quick Ratio

```
Quick Ratio = (New MRR + Expansion MRR) / (|Contraction MRR| + |Churn MRR|)
```

| Quick Ratio | Interpretation |
|---|---|
| < 1 | Shrinking. Losses exceed gains. |
| 1-2 | Growing but high churn erodes growth |
| 2-4 | Healthy growth |
| 4+ | Excellent. Gold standard. |

## 6. Rule of 40 & Efficiency Metrics

```
Rule of 40 = YoY ARR Growth Rate (%) + Free Cash Flow Margin (%)
```

Target >= 40%. Companies exceeding get ~2x revenue multiple premium.

**Bessemer Efficiency Score:**
```
Efficiency Score = Net New ARR / Net Burn
```

## Common Pitfalls

- **Annualizing churn wrong**: `Annual = 1 - (1 - Monthly)^12`, NOT `Monthly * 12`. 5% monthly = 46% annual, not 60%.
- **Mixing gross vs net churn**: Always track and report both. Net churn can mask high gross churn.
- **Customer vs revenue churn**: 10% logo churn != 10% revenue churn. Specify which.
- **Annualizing immature cohorts**: Only compute annual NRR for cohorts with >= 12 months observation.
- **Including one-time revenue in MRR**: Setup fees, professional services must be excluded.
- **Trial contamination**: Define a seasoning period; don't count trial-to-churn as regular churn.
- **Counting reactivation as new**: Check prior subscription history before classifying as "new".
- **Annual plan normalization**: `MRR = Annual Plan Price / 12`. Don't count full annual in one month.
- **Denominator**: Use beginning-of-month MRR for churn rate, not end-of-month.

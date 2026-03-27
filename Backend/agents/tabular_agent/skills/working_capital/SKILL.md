---
name: working-capital
description: Working capital analysis — DSO, DPO, DIO, cash conversion cycle, AR/AP/inventory aging, FCF conversion, working capital forecasting.
tags: [finance, working-capital, dso, dpo, dio, ccc, aging, fcf, cash-flow, receivables, payables, inventory]
---

# Working Capital Analysis Skill

## When to Use This Skill

Use this skill when the user asks for:
- DSO, DPO, DIO calculation
- Cash conversion cycle analysis
- AR aging, AP aging, or inventory aging reports
- Free cash flow or FCF conversion analysis
- Working capital trend analysis or forecasting
- Net working capital as % of revenue

## 1. Core Metrics

### Days Sales Outstanding (DSO)

```
DSO = (Average AR / Net Revenue) x Days in Period
```

### Days Payable Outstanding (DPO)

```
DPO = (Average AP / COGS) x Days in Period
```

### Days Inventory Outstanding (DIO)

```
DIO = (Average Inventory / COGS) x Days in Period
```

**Important:** DPO and DIO use COGS as denominator. Only DSO uses Revenue.

### Cash Conversion Cycle

```
CCC = DIO + DSO - DPO
```

| CCC Value | Interpretation |
|---|---|
| Positive (e.g., 45) | Company finances 45 days of working capital |
| Zero | Collections fund payments exactly |
| Negative (e.g., -15) | Collects before paying suppliers (Amazon, Dell) |
| Declining trend | Improving efficiency |
| Increasing trend | Deteriorating — investigate which component |

## 2. AR Aging Report

```sql
WITH aged AS (
    SELECT customer_name, invoice_number, amount_outstanding,
        DATEDIFF('day', due_date, CURRENT_DATE) AS days_past_due
    FROM s_ar_invoices
    WHERE payment_date IS NULL OR amount_outstanding > 0
)
SELECT customer_name,
    SUM(CASE WHEN days_past_due <= 0 THEN amount_outstanding ELSE 0 END) AS "Current",
    SUM(CASE WHEN days_past_due BETWEEN 1 AND 30 THEN amount_outstanding ELSE 0 END) AS "1-30",
    SUM(CASE WHEN days_past_due BETWEEN 31 AND 60 THEN amount_outstanding ELSE 0 END) AS "31-60",
    SUM(CASE WHEN days_past_due BETWEEN 61 AND 90 THEN amount_outstanding ELSE 0 END) AS "61-90",
    SUM(CASE WHEN days_past_due > 90 THEN amount_outstanding ELSE 0 END) AS "90+",
    SUM(amount_outstanding) AS total
FROM aged GROUP BY customer_name ORDER BY total DESC;
```

### AP Aging Report

Same structure but age from **due date**, not invoice date:

```sql
SELECT vendor_name,
    SUM(CASE WHEN days_past_due <= 0 THEN amount_outstanding ELSE 0 END) AS "Not Yet Due",
    SUM(CASE WHEN days_past_due BETWEEN 1 AND 30 THEN amount_outstanding ELSE 0 END) AS "1-30 Past Due",
    SUM(CASE WHEN days_past_due BETWEEN 31 AND 60 THEN amount_outstanding ELSE 0 END) AS "31-60 Past Due",
    SUM(CASE WHEN days_past_due > 60 THEN amount_outstanding ELSE 0 END) AS "60+ Past Due",
    SUM(amount_outstanding) AS total_owed
FROM aged_ap GROUP BY vendor_name ORDER BY total_owed DESC;
```

### Inventory Aging

```sql
SELECT sku, product_name, quantity_on_hand * unit_cost AS extended_value,
    DATEDIFF('day', date_received, CURRENT_DATE) AS days_on_hand,
    CASE
        WHEN DATEDIFF('day', date_received, CURRENT_DATE) <= 30 THEN 'Fast-Moving'
        WHEN DATEDIFF('day', date_received, CURRENT_DATE) <= 90 THEN 'Normal'
        WHEN DATEDIFF('day', date_received, CURRENT_DATE) <= 180 THEN 'At Risk'
        ELSE 'Obsolescence Candidate'
    END AS aging_category,
    CASE
        WHEN DATEDIFF('day', date_received, CURRENT_DATE) <= 90 THEN 0.00
        WHEN DATEDIFF('day', date_received, CURRENT_DATE) <= 180 THEN 0.25
        WHEN DATEDIFF('day', date_received, CURRENT_DATE) <= 365 THEN 0.50
        ELSE 1.00
    END AS suggested_reserve_pct
FROM s_inventory WHERE quantity_on_hand > 0
ORDER BY days_on_hand DESC;
```

## 3. Free Cash Flow & FCF Conversion

```
FCF = Operating Cash Flow - Capital Expenditures
FCF Conversion = FCF / EBITDA x 100
```

| FCF Conversion | Interpretation |
|---|---|
| > 100% | Exceptional — favorable working capital |
| 80-100% | Strong |
| 50-80% | Moderate — may indicate WC drag or high capex |
| < 50% | Weak — investigate |

## 4. Working Capital Trend

```sql
SELECT period_date,
    current_assets - current_liabilities AS nwc,
    ROUND((current_assets - current_liabilities)::DECIMAL / NULLIF(revenue, 0) * 100, 1) AS nwc_pct_of_revenue,
    (current_assets - current_liabilities)
      - LAG(current_assets - current_liabilities) OVER (ORDER BY period_date) AS nwc_change
FROM s_balance_sheet ORDER BY period_date;
```

**Operating NWC** (more precise):
```
Operating NWC = AR + Inventory - AP
```

## 5. Forecasting Approaches

### Percent-of-Revenue (most common)
```
Forecast AR = Forecast Revenue x (Historical DSO / 365)
Forecast Inventory = Forecast COGS x (Historical DIO / 365)
Forecast AP = Forecast COGS x (Historical DPO / 365)
```

### Scenario-Based
- What if DSO increases 5-10 days?
- What if a key supplier shortens terms from Net 60 to Net 30?
- What if inventory builds due to supply chain disruption?

## Common Pitfalls

- **Seasonal distortion**: Use trailing 12-month averages or same-quarter YoY comparison
- **Window dressing**: Companies accelerate collections at period-end. Compare period-end vs intra-period averages.
- **Negative WC misread**: Structural negative WC (prepaid customers, fast turns) is a strength. Distressed negative WC (can't pay obligations) is a problem. Test: Is revenue growing and are suppliers paid within terms?
- **COGS vs Revenue in DIO/DPO**: Always use COGS. Only DSO uses revenue.
- **Growth distortion**: Rapidly growing companies show rising absolute NWC naturally. Always normalize to NWC as % of revenue.
- **Intercompany items**: Strip non-trade items for operating WC analysis. Use AR + Inventory - AP, not total current assets - total current liabilities.

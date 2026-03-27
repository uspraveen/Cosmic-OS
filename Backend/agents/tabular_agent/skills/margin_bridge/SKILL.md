---
name: margin-bridge
description: Margin bridge and waterfall analysis — Price-Volume-Mix decomposition, gross/operating margin bridges, segment profitability, incremental margin.
tags: [finance, margin, bridge, waterfall, pvm, price-volume-mix, segment, profitability, contribution-margin, incremental]
---

# Margin Bridge / Waterfall Analysis Skill

## When to Use This Skill

Use this skill when the user asks for:
- Margin bridge or waterfall chart between two periods
- Price-Volume-Mix (PVM) decomposition
- Segment or product profitability analysis
- Contribution margin analysis
- Incremental margin or operating leverage analysis
- Explaining why margins changed period-over-period

## 1. Gross Margin Bridge

```
GM(t0) -> +/- Volume Effect -> +/- Price Effect -> +/- Mix Effect -> +/- Cost Effect -> GM(t1)
```

### Core Formulas

**Volume Effect:**
```
Volume_Effect = (Total_Qty_t1 - Total_Qty_t0) x Avg_GM_per_unit_t0
```

**Price Effect:**
```
Price_Effect = SUM_products[ (ASP_i_t1 - ASP_i_t0) x Qty_i_t1 ]
```

**Cost Effect:**
```
Cost_Effect = -1 x SUM_products[ (UnitCost_i_t1 - UnitCost_i_t0) x Qty_i_t1 ]
```

**Mix Effect:**
```
Mix_Effect = SUM_products[ (MixShare_i_t1 - MixShare_i_t0) x Total_Qty_t1 x GM_per_unit_i_t0 ]
```

**Verification:** Volume + Price + Mix + Cost = GM(t1) - GM(t0)

## 2. PVM Decomposition Methods

### Method A: Sequential (Base-Period Weighted)
Produces a cross-term (`delta_P * delta_Q`) that must be allocated. Order-dependent.

### Method B: Current-Period Weighted (most common in FP&A)
```
Price_Effect  = SUM_i[ (P_i_t1 - P_i_t0) * Q_i_t1 ]
Volume_Effect = (Q_total_t1 - Q_total_t0) * Weighted_Avg_Price_t0
Mix_Effect    = Total Change - Price - Volume (residual)
```

### Method C: Symmetric / Average-Base (FTI Consulting recommended)
Uses average of both periods, eliminating order dependency and cross-terms:
```
P_bar_i = (P_i_t0 + P_i_t1) / 2
Q_bar   = (Q_total_t0 + Q_total_t1) / 2
```

**Default to Method C for robustness. Use Method B if user expects traditional FP&A format.**

## 3. Operating Margin Bridge

```
Op_Income(t0)
  -> +/- Gross Margin Change (Volume, Price, Mix, COGS)
  -> +/- SG&A Leverage (fixed cost absorption on growth)
  -> +/- R&D Spend Change
  -> +/- D&A Changes
  -> +/- One-Time Items
  -> Op_Income(t1)
```

**Operating Leverage:**
```
Operating_Leverage = % Change in Operating Income / % Change in Revenue
```
Ratio > 1 = positive leverage (margin expansion with growth).

## 4. Segment Profitability (Multi-Level Contribution Margin)

```
Revenue
- Variable COGS
- Variable SG&A
= Contribution Margin (CM)

- Traceable Fixed Costs
= Segment Margin

- Common Fixed Costs (NEVER allocate to segments)
= Operating Income
```

### SQL: Segment Profitability

```sql
SELECT segment,
    SUM(revenue) AS revenue,
    SUM(revenue - cogs - variable_sga) AS contribution_margin,
    ROUND(SUM(revenue - cogs - variable_sga) * 100.0 / NULLIF(SUM(revenue), 0), 1) AS cm_pct,
    SUM(revenue - cogs - variable_sga - traceable_fixed) AS segment_margin,
    ROUND(SUM(revenue - cogs - variable_sga - traceable_fixed) * 100.0 / NULLIF(SUM(revenue), 0), 1) AS segment_margin_pct
FROM s_pnl WHERE period_id = '2025Q1'
GROUP BY segment ORDER BY segment_margin DESC;
```

## 5. SQL: Revenue PVM Bridge (Method B)

```sql
WITH base AS (
    SELECT product_id,
        SUM(CASE WHEN period_id = 't0' THEN quantity END) AS qty_t0,
        SUM(CASE WHEN period_id = 't1' THEN quantity END) AS qty_t1,
        SUM(CASE WHEN period_id = 't0' THEN revenue END) / NULLIF(SUM(CASE WHEN period_id = 't0' THEN quantity END), 0) AS asp_t0,
        SUM(CASE WHEN period_id = 't1' THEN revenue END) / NULLIF(SUM(CASE WHEN period_id = 't1' THEN quantity END), 0) AS asp_t1
    FROM s_pnl WHERE period_id IN ('t0', 't1') GROUP BY product_id
),
totals AS (
    SELECT SUM(qty_t0) AS total_qty_t0, SUM(qty_t1) AS total_qty_t1,
        SUM(qty_t0 * asp_t0) AS total_rev_t0, SUM(qty_t1 * asp_t1) AS total_rev_t1
    FROM base
)
SELECT
    SUM((b.asp_t1 - b.asp_t0) * b.qty_t1) AS price_effect,
    (SELECT (total_qty_t1 - total_qty_t0) * (total_rev_t0 / NULLIF(total_qty_t0, 0)) FROM totals) AS volume_effect,
    (SELECT total_rev_t1 - total_rev_t0 FROM totals)
      - SUM((b.asp_t1 - b.asp_t0) * b.qty_t1)
      - (SELECT (total_qty_t1 - total_qty_t0) * (total_rev_t0 / NULLIF(total_qty_t0, 0)) FROM totals) AS mix_effect
FROM base b;
```

## 6. Waterfall Chart Data Structure

```json
{
  "bars": [
    {"label": "Prior Period GM", "value": 45000000, "type": "absolute"},
    {"label": "Volume Effect",   "value": 3200000,  "type": "relative"},
    {"label": "Price Effect",    "value": 1800000,  "type": "relative"},
    {"label": "Mix Effect",      "value": -900000,  "type": "relative"},
    {"label": "COGS Inflation",  "value": -2100000, "type": "relative"},
    {"label": "Current Period GM","value": 47500000, "type": "total"}
  ]
}
```

## 7. Incremental Margin

```
Incremental GM % = (GP_t1 - GP_t0) / (Rev_t1 - Rev_t0) x 100
Incremental Op Margin % = (OI_t1 - OI_t0) / (Rev_t1 - Rev_t0) x 100
```

| Incremental vs Base | Meaning |
|---|---|
| Incremental > Base | Growth is accretive; margins expanding |
| Incremental = Base | Growth maintaining profitability |
| Incremental < Base (>0) | Growth is dilutive; margins compressing |
| Incremental < 0 | Growth is value-destroying |

## Common Pitfalls

- **Never allocate common fixed costs to segments** for profitability decisions
- **PVM cross-term**: Use symmetric method to eliminate order dependency
- **Mix at wrong aggregation level**: Compute PVM at most granular level (SKU), then aggregate
- **Acquisitions**: Separate organic vs inorganic bridges for structural changes
- **FX buried in price**: Add separate "FX Translation Effect" bar for multinationals
- **One-time items**: Isolate as explicit bar; present both "as reported" and "adjusted" bridges
- **Revenue decline**: Reframe as "decremental margin" — high decremental margin = dangerous leverage
- **Standard vs actual cost**: Use fully absorbed actual costs, not ERP standard costs

Return exactly one JSON object for this document asset.

Schema:
{
  "summary": "2-4 sentence grounded summary of what matters in this asset",
  "visual_type": "chart|diagram|slide|ui|design|photo|table|mixed|other",
  "visible_text": ["important visible labels, headings, legend items, callouts, or large text"],
  "chart_observations": ["chart type, axes, units, series, values, and trends when present"],
  "diagram_relationships": ["nodes, arrows, flows, containment, or causal relationships when present"],
  "design_observations": ["layout hierarchy, comparison framing, KPI cards, product mockups, or visual emphasis that changes meaning"],
  "key_entities": ["named products, teams, metrics, systems, or people explicitly visible"],
  "uncertainties": ["what is partially unreadable, ambiguous, or too small to verify"],
  "confidence": "high|medium|low"
}

Rules:
- Ground everything in the image. Do not infer identities, names, or values that are not actually visible.
- For PPT or slide-like visuals, describe the slide's message, layout hierarchy, and how charts, screenshots, or callouts support that message.
- For charts, capture chart type, axes, legends, units, values, comparative direction, and anomalies. Mention if labels are too small to read.
- For diagrams, capture entities and relationships, not just appearance.
- For UI, dashboard, or design slides, capture panels, modules, KPIs, states, and calls to action.
- Ignore purely decorative styling unless it changes meaning or emphasis.
- Keep arrays concise and factual.

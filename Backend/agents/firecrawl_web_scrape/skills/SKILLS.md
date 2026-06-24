# Firecrawl Web Scrape Agent Skills

## When To Use `firecrawl.scrape`
- Single URL.
- Need clean markdown or normalized page formats.
- Native web fetch is insufficient because the page is dynamic, noisy, or inconsistent.
- The data is locked inside an image (a benchmark table, chart, or infographic rendered as a picture, not text). Request `formats: ["screenshot"]`; the screenshot is persisted as an `image/png` artifact that the orchestrator's vision model can read directly. The default text orchestrator cannot read images, but COSMIC automatically escalates to the vision-capable model when an image artifact is surfaced.
- The source is a PDF or scanned document whose text does not come through. Pass `parsers: ["pdf"]` (or `[{"type": "pdf"}]`) to force OCR/parsing.

## When To Use `firecrawl.extract`
- Need structured data instead of raw page text.
- Need the same prompt applied across multiple URLs.
- Need returned sources alongside extracted fields.
- Extraction is text/DOM based: it cannot reliably read numbers that live inside an image. For image-locked tables/charts, prefer `firecrawl.scrape` with `formats: ["screenshot"]` and let the vision model read it.
- Fields whose values are not explicitly present in the page are returned as `null`; the agent instructs Firecrawl never to guess or fabricate. Cross-check any surprising number against the page text or the screenshot before trusting it.
- Pass `parsers: ["pdf"]` for PDF/scanned-document sources before extraction.

## When To Use `firecrawl.agent`
- Simpler firecrawl.scrape or firecrawl.extract has failed or is clearly insufficient.
- Complex extractions that require autonomous multi-page navigation or site interaction.
- You do not have the right seed URLs and need the agent to discover them autonomously.
- Provide optional seed URLs to focus the agent when you have partial knowledge of the target.
- Do NOT default to this mode; it is slower and more expensive than scrape or extract.

## Reading Image-Locked Data (tables/charts as pictures)
- Symptom: the page renders the numbers you need as an image, so markdown/extract come back empty, partial, or suspiciously round.
- Fix: scrape the page with `formats: ["screenshot"]`. The agent saves the screenshot as an `image/png` artifact and exposes a fetchable URL on the artifact reference, so the orchestrator surfaces it to the vision model automatically — no separate image agent needed.
- Do not fabricate or approximate numbers from a partial text read. If the authoritative values are in an image, get the screenshot and read it visually.

## Output Discipline
- Return a compact human-readable summary.
- Put raw provider payloads and large extracted bodies into task artifacts.
- Inline excerpts are bounded; when an excerpt is truncated the output flags `*_truncated` and names the full-content artifact (e.g. `page.md`). Read the artifact when the table/data you need is past the excerpt cutoff.
- Keep session recall rows short and useful: target URL(s), summary, artifact references, timestamp.

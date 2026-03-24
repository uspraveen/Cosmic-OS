# Firecrawl Web Scrape Agent Skills

## When To Use `firecrawl.scrape`
- Single URL.
- Need clean markdown or normalized page formats.
- Native web fetch is insufficient because the page is dynamic, noisy, or inconsistent.

## When To Use `firecrawl.extract`
- Need structured data instead of raw page text.
- Need the same prompt applied across multiple URLs.
- Need returned sources alongside extracted fields.

## When To Use `firecrawl.agent`
- Simpler firecrawl.scrape or firecrawl.extract has failed or is clearly insufficient.
- Complex extractions that require autonomous multi-page navigation or site interaction.
- You do not have the right seed URLs and need the agent to discover them autonomously.
- Provide optional seed URLs to focus the agent when you have partial knowledge of the target.
- Do NOT default to this mode; it is slower and more expensive than scrape or extract.

## Output Discipline
- Return a compact human-readable summary.
- Put raw provider payloads and large extracted bodies into task artifacts.
- Keep session recall rows short and useful: target URL(s), summary, artifact references, timestamp.

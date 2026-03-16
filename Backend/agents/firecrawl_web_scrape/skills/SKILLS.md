# Firecrawl Web Scrape Agent Skills

## When To Use `firecrawl.scrape`
- Single URL.
- Need clean markdown or normalized page formats.
- Native web fetch is insufficient because the page is dynamic, noisy, or inconsistent.

## When To Use `firecrawl.extract`
- Need structured data instead of raw page text.
- Need the same prompt applied across multiple URLs.
- Need returned sources alongside extracted fields.

## Output Discipline
- Return a compact human-readable summary.
- Put raw provider payloads and large extracted bodies into task artifacts.
- Keep session recall rows short and useful: target URL(s), summary, artifact references, timestamp.

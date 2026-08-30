import { describe, expect, it } from 'vitest'
import {
  domainName,
  extractDomains,
  faviconUrl,
  resolveAgentSignal,
  stripActorPrefix,
  summarizeAgentSignals,
} from './agentSignals'

describe('extractDomains', () => {
  it('reads hosts out of full URLs', () => {
    expect(extractDomains('Firecrawl scraped https://www.example.com/a/b?q=1')).toEqual(['example.com'])
  })

  it('reads bare hostnames the orchestrator writes into activity text', () => {
    expect(extractDomains('web search found nytimes.com, arstechnica.com and 3 more'))
      .toEqual(['nytimes.com', 'arstechnica.com'])
  })

  it('does not mistake filenames for websites', () => {
    // Every one of these appears verbatim in real activity lines.
    const noise = 'wrote cosmic.md, bundle.duckdb, runtime.py, task_ledger.db, deck.pptx, artifact.bin'
    expect(extractDomains(noise)).toEqual([])
  })

  it('ignores email addresses', () => {
    expect(extractDomains('emailed harj@ycombinator.com about the deck')).toEqual([])
  })

  it('de-duplicates and preserves first-seen order', () => {
    expect(extractDomains('x.com then youtube.com then x.com')).toEqual(['x.com', 'youtube.com'])
  })

  it('honours the limit', () => {
    expect(extractDomains('a.com b.com c.com d.com', 2)).toEqual(['a.com', 'b.com'])
  })
})

describe('domainName', () => {
  it('takes the registrable name', () => {
    expect(domainName('mail.google.com')).toBe('google')
    expect(domainName('www.nytimes.com')).toBe('nytimes')
  })

  it('steps over a second-level suffix', () => {
    expect(domainName('bbc.co.uk')).toBe('bbc')
  })
})

describe('resolveAgentSignal', () => {
  it('prefers an explicit agent id', () => {
    const result = resolveAgentSignal({ agentId: 'cosmic/x-twitter-search-agent:1.0.0', label: 'searched posts' })
    expect(result.glyph).toBe('x')
  })

  it('reads the gateway snake_case shape mobile stores', () => {
    const result = resolveAgentSignal({ agent_id: 'cosmic/gmail-agent', label: 'read email messages' })
    expect(result.glyph).toBe('gmail')
    expect(result.brand).toBe('#EA4335')
  })

  it('falls back to the intent when there is no agent id', () => {
    expect(resolveAgentSignal({ intent: 'calendar.create_event', label: 'made an event' }).glyph).toBe('calendar')
  })

  it('separates the Google Docs agent from the document parser under one prefix', () => {
    expect(resolveAgentSignal({ intent: 'docs.create', label: 'created a doc' }).glyph).toBe('gdocs')
    expect(resolveAgentSignal({ intent: 'docs.read_bundle', label: 'read a bundle' }).glyph).toBe('doc')
  })

  it('recognises the orchestrator local tools from their sentence alone', () => {
    // These entries carry no agent id at all; wording is the only evidence.
    expect(resolveAgentSignal({ label: 'ran the local code sandbox' }).glyph).toBe('sandbox')
    expect(resolveAgentSignal({ label: 'searched memory for "harj"' }).glyph).toBe('memory')
    expect(resolveAgentSignal({ label: 'searched the specialist catalog' }).glyph).toBe('agent')
    expect(resolveAgentSignal({ label: 'Revisiting exact session history...' }).glyph).toBe('recall')
    expect(resolveAgentSignal({ label: 'revisited exact session history' }).glyph).toBe('recall')
    expect(resolveAgentSignal({ label: 'loaded full memory block "Session summary"' }).glyph).toBe('memory')
    expect(resolveAgentSignal({ label: 'Creating reminder: Portfolio new visitor check' }).glyph).toBe('schedule')
    expect(resolveAgentSignal({ label: 'Creating reminder: Portfolio new visitor check' }).brand).toBeUndefined()
  })

  it('keeps Google Calendar on the calendar mark without a product tint', () => {
    const result = resolveAgentSignal({ intent: 'calendar.create_event', label: 'made an event' })
    expect(result.glyph).toBe('calendar')
    expect(result.brand).toBeUndefined()
  })

  it('gives Firecrawl its own mark rather than the generic globe', () => {
    // It does most of the page reading, so it is the mark seen most often.
    expect(resolveAgentSignal({ agentId: 'cosmic/firecrawl-web-scrape-agent', label: 'read a page' }).glyph)
      .toBe('firecrawl')
    expect(resolveAgentSignal({ intent: 'firecrawl.scrape', label: 'read a page' }).glyph).toBe('firecrawl')
    expect(resolveAgentSignal({ label: 'Firecrawl pulled the listing' }).glyph).toBe('firecrawl')
  })

  it('still falls back to the plain globe for web work that is not Firecrawl', () => {
    expect(resolveAgentSignal({ label: 'opened the vendor portal at example.com' }).glyph).toBe('web')
  })

  it('lets a recognised product domain outrank the agent that fetched it', () => {
    const result = resolveAgentSignal({
      agentId: 'cosmic/firecrawl-web-scrape-agent',
      label: 'Firecrawl scraped youtube.com/watch',
    })
    expect(result.glyph).toBe('youtube')
  })

  it('does not repeat the domain that chose the mark as a favicon beside it', () => {
    const result = resolveAgentSignal({ label: 'searched x.com for posts about the launch' })
    expect(result.glyph).toBe('x')
    expect(result.domains).toEqual([])
  })

  it('keeps the other sites when one of them chose the mark', () => {
    const result = resolveAgentSignal({ label: 'read youtube.com then nytimes.com' })
    expect(result.glyph).toBe('youtube')
    expect(result.domains).toEqual(['nytimes.com'])
  })

  it('carries the domains through for a favicon cluster', () => {
    const result = resolveAgentSignal({ label: 'web search found nytimes.com and theverge.com' })
    expect(result.glyph).toBe('search')
    expect(result.domains).toEqual(['nytimes.com', 'theverge.com'])
  })

  it('always returns something drawable', () => {
    expect(resolveAgentSignal({}).glyph).toBe('cosmic')
    expect(resolveAgentSignal({ label: '' }).domains).toEqual([])
  })
})

describe('summarizeAgentSignals', () => {
  it('returns one entry per distinct mark', () => {
    const marks = summarizeAgentSignals([
      { agentId: 'cosmic/gmail-agent', label: 'read email' },
      { agentId: 'cosmic/gmail-agent', label: 'read more email' },
      { label: 'ran the local code sandbox' },
    ])
    expect(marks.map((item) => item.glyph)).toEqual(['gmail', 'sandbox'])
  })

  it('hides the generic fallback when real work is present', () => {
    const marks = summarizeAgentSignals([
      { label: 'Cosmic is writing the response.' },
      { agentId: 'cosmic/x-twitter-search-agent', label: 'searched X' },
    ])
    expect(marks.map((item) => item.glyph)).toEqual(['x'])
  })

  it('still shows something when every step was generic', () => {
    const marks = summarizeAgentSignals([{ label: 'Cosmic is writing the response.' }])
    expect(marks).toHaveLength(1)
  })

  it('caps the preview', () => {
    const marks = summarizeAgentSignals(
      [
        { agentId: 'cosmic/gmail-agent', label: 'a' },
        { agentId: 'cosmic/calendar-agent', label: 'b' },
        { agentId: 'cosmic/map-agent', label: 'c' },
        { agentId: 'cosmic/slide-agent', label: 'd' },
        { agentId: 'cosmic/diagram-agent', label: 'e' },
      ],
      3,
    )
    expect(marks).toHaveLength(3)
  })

  it('tolerates no entries', () => {
    expect(summarizeAgentSignals(undefined)).toEqual([])
    expect(summarizeAgentSignals([])).toEqual([])
  })
})

describe('stripActorPrefix', () => {
  const x = resolveAgentSignal({ agentId: 'cosmic/x-twitter-search-agent', label: 'searched X' })

  it('drops the lead-in the mark already says', () => {
    expect(stripActorPrefix('X twitter search agent: Searching X for shipping chatter', x))
      .toBe('Searching X for shipping chatter')
  })

  it('capitalises what is left', () => {
    expect(stripActorPrefix('Gmail agent: reading the thread', x)).toBe('Reading the thread')
  })

  it('keeps a sentence that merely mentions an agent mid-way', () => {
    const line = 'Handed the shipping question to a specialist and waited'
    expect(stripActorPrefix(line, x)).toBe(line)
  })

  it('leaves the line alone when the mark is the generic one', () => {
    const generic = resolveAgentSignal({ label: 'Cosmic is writing the response.' })
    expect(stripActorPrefix('Research agent: looking around', generic)).toBe('Research agent: looking around')
  })

  it('keeps a prefix that would leave nothing behind', () => {
    expect(stripActorPrefix('X agent: ok', x)).toBe('X agent: ok')
  })

  it('tolerates an empty line', () => {
    expect(stripActorPrefix('', x)).toBe('')
    expect(stripActorPrefix(undefined, x)).toBe('')
  })
})

describe('faviconUrl', () => {
  it('escapes the domain', () => {
    expect(faviconUrl('example.com')).toBe('https://www.google.com/s2/favicons?domain=example.com&sz=64')
  })
})

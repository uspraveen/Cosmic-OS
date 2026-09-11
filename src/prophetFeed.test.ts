import { describe, expect, it } from 'vitest'
import {
  editionStoryCount,
  formatProphetDateline,
  formatProphetRelativeTime,
  normalizeProphetEdition,
  normalizeProphetSettings,
  prophetAccent,
  prophetDomain,
  resolveProphetLayout,
  type ProphetSection,
  type ProphetStory,
} from './prophetFeed'

const story = (overrides: Partial<ProphetStory> = {}): ProphetStory => ({
  id: 's1',
  headline: 'A headline',
  body: [],
  role: 'standard',
  importance: 50,
  tags: [],
  accent: 'rose',
  ...overrides,
})

const section = (overrides: Partial<ProphetSection> = {}): ProphetSection => ({
  id: 'tech',
  label: 'Technology',
  layout: 'columns',
  stories: [story()],
  ...overrides,
})

const editionRecord = (payloadOverrides: Record<string, unknown> = {}) => ({
  edition_id: 'ped_123',
  edition_date: '2026-09-11',
  slot: 'morning',
  schema_version: 1,
  revision: 2,
  updated_at: '2026-09-11T10:00:00Z',
  payload: {
    schema_version: 1,
    edition_date: '2026-09-11',
    slot: 'morning',
    lead: {
      headline: 'Lead',
      body: ['para'],
      role: 'lead',
      importance: 90,
      source: { name: 'Reuters', url: 'https://example.com/lead' },
    },
    sections: [
      {
        id: 'tech',
        label: 'Technology',
        layout: 'briefs',
        stories: [
          { headline: 'One', role: 'brief', body: [] },
          { headline: 'Two', role: 'brief', body: [] },
        ],
      },
    ],
    ...payloadOverrides,
  },
})

describe('normalizeProphetEdition', () => {
  it('normalizes an edition record from the gateway', () => {
    const edition = normalizeProphetEdition(editionRecord())
    expect(edition).not.toBeNull()
    expect(edition?.editionId).toBe('ped_123')
    expect(edition?.revision).toBe(2)
    expect(edition?.lead?.headline).toBe('Lead')
    expect(edition?.lead?.role).toBe('lead')
    expect(edition?.sections[0].stories).toHaveLength(2)
    expect(editionStoryCount(edition!)).toBe(3)
  })

  it('accepts a bare payload and forces the lead role', () => {
    const edition = normalizeProphetEdition({
      edition_date: '2026-09-11',
      slot: 'evening',
      lead: { headline: 'Bare lead', role: 'standard' },
      sections: [{ id: 'science', label: 'Science', stories: [{ headline: 'S' }] }],
    })
    expect(edition?.slot).toBe('evening')
    expect(edition?.lead?.role).toBe('lead')
  })

  it('rejects payloads without stories', () => {
    expect(normalizeProphetEdition({ sections: [] })).toBeNull()
    expect(normalizeProphetEdition(null)).toBeNull()
  })

  it('drops malformed sections and defaults unknown roles/layouts', () => {
    const edition = normalizeProphetEdition({
      sections: [
        { id: 'tech', layout: 'nonsense', stories: [{ headline: 'Kept', role: 'nonsense' }] },
        { id: 'empty', stories: [] },
      ],
    })
    expect(edition?.sections).toHaveLength(1)
    expect(edition?.sections[0].layout).toBe('columns')
    expect(edition?.sections[0].stories[0].role).toBe('standard')
  })
})

describe('resolveProphetLayout', () => {
  it('maps semantic intents to deterministic templates', () => {
    expect(resolveProphetLayout(section({ layout: 'briefs', stories: [story(), story(), story(), story()] }))).toBe('m4')
    expect(resolveProphetLayout(section({ layout: 'gallery', stories: [story({ image: { url: 'a' } }), story({ image: { url: 'b' } })] }))).toBe('m5')
    expect(resolveProphetLayout(section({ layout: 'gallery', stories: [story({ image: { url: 'a' } }), story()] }))).toBe('m2')
    expect(resolveProphetLayout(section({ layout: 'feature', stories: [story(), story(), story()] }))).toBe('m3')
    expect(resolveProphetLayout(section({ stories: [story()] }))).toBe('m1')
    expect(resolveProphetLayout(section({ stories: [story(), story()] }))).toBe('m2')
    expect(resolveProphetLayout(section({ stories: [story(), story(), story()] }))).toBe('m3')
    expect(resolveProphetLayout(section({ stories: [story(), story(), story(), story()] }))).toBe('m4')
    expect(resolveProphetLayout(section({ stories: [story(), story(), story(), story(), story(), story(), story()] }))).toBe('m3')
  })
})

describe('prophetAccent', () => {
  it('prefers canonical section tones', () => {
    expect(prophetAccent('breaking', 'x')).toBe('rose')
    expect(prophetAccent('science', 'x')).toBe('mint')
  })

  it('is deterministic for unknown sections', () => {
    expect(prophetAccent('custom', 'same headline')).toBe(prophetAccent('custom', 'same headline'))
  })
})

describe('formatProphetRelativeTime', () => {
  const now = Date.parse('2026-09-11T12:00:00Z')
  it('formats relative windows', () => {
    expect(formatProphetRelativeTime('2026-09-11T11:58:00Z', now)).toBe('2m ago')
    expect(formatProphetRelativeTime('2026-09-11T09:00:00Z', now)).toBe('3h ago')
    expect(formatProphetRelativeTime('2026-09-09T12:00:00Z', now)).toBe('2d ago')
    expect(formatProphetRelativeTime('', now)).toBe('')
  })
})

describe('formatProphetDateline', () => {
  it('formats an edition date and falls back for junk', () => {
    expect(formatProphetDateline('2026-09-11')).toContain('2026')
    expect(formatProphetDateline('not-a-date')).toBe('not-a-date')
    expect(formatProphetDateline(undefined)).toBe('')
  })
})

describe('normalizeProphetSettings', () => {
  it('normalizes gateway settings', () => {
    const settings = normalizeProphetSettings({
      enabled: false,
      morning_time: '06:15',
      evening_enabled: false,
      evening_time: '20:00',
      max_stories: 22,
      notifications_enabled: false,
      paper_style: 'newsprint',
      sections: [{ id: 'tech', label: 'Tech', enabled: false }],
    })
    expect(settings.enabled).toBe(false)
    expect(settings.morningTime).toBe('06:15')
    expect(settings.eveningEnabled).toBe(false)
    expect(settings.maxStories).toBe(22)
    expect(settings.paperStyle).toBe('newsprint')
    expect(settings.sections).toEqual([{ id: 'tech', label: 'Tech', enabled: false }])
  })

  it('falls back to defaults on junk', () => {
    const settings = normalizeProphetSettings({ max_stories: 999, paper_style: 'cardboard' })
    expect(settings.maxStories).toBe(15)
    expect(settings.paperStyle).toBe('parchment')
    expect(settings.sections.length).toBe(5)
  })
})

describe('prophetDomain', () => {
  it('extracts a clean display domain', () => {
    expect(prophetDomain('https://www.reuters.com/technology/x')).toBe('reuters.com')
    expect(prophetDomain('not a url')).toBe('')
    expect(prophetDomain(undefined)).toBe('')
  })
})

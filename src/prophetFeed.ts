export type ProphetSlot = 'morning' | 'evening'
export type ProphetLayoutIntent = 'feature' | 'columns' | 'briefs' | 'gallery' | 'essay'
export type ProphetStoryRole = 'lead' | 'feature' | 'standard' | 'brief' | 'pull_quote' | 'image_led'
export type ProphetLayoutMode = 'm1' | 'm2' | 'm3' | 'm4' | 'm5'
export type ProphetAccent = 'rose' | 'azure' | 'slate' | 'mint' | 'gold'

export interface ProphetStoryImage {
  url: string
  caption?: string
  credit?: string
}

export interface ProphetStorySource {
  name?: string
  url?: string
}

export interface ProphetStory {
  id: string
  headline: string
  dek?: string
  body: string[]
  role: ProphetStoryRole
  importance: number
  source?: ProphetStorySource
  publishedAt?: string
  image?: ProphetStoryImage
  whySelected?: string
  tags: string[]
  sectionId?: string
  accent: ProphetAccent
}

export interface ProphetSection {
  id: string
  label: string
  layout: ProphetLayoutIntent
  quietDayText?: string
  stories: ProphetStory[]
}

export interface ProphetEdition {
  editionId: string
  editionDate: string
  slot: ProphetSlot
  schemaVersion: number
  editorNote?: string
  lead: ProphetStory | null
  sections: ProphetSection[]
  footer?: string
  publishedAt?: string
  revision: number
}

export interface ProphetSectionSetting {
  id: string
  label: string
  enabled: boolean
}

export interface ProphetSettings {
  enabled: boolean
  morningTime: string
  eveningEnabled: boolean
  eveningTime: string
  maxStories: number
  notificationsEnabled: boolean
  sections: ProphetSectionSetting[]
}

export interface ProphetInterest {
  topic: string
  origin: string
  weight: number
  muted: boolean
}

export interface ProphetSource {
  sourceId: string
  kind: string
  value: string
  label?: string
  origin: string
  active: boolean
}

export const EMPTY_PROPHET_SETTINGS: ProphetSettings = {
  enabled: true,
  morningTime: '05:00',
  eveningEnabled: true,
  eveningTime: '19:00',
  maxStories: 15,
  notificationsEnabled: true,
  sections: [
    { id: 'breaking', label: 'Breaking Dispatch', enabled: true },
    { id: 'tech', label: 'Technology & Innovation', enabled: true },
    { id: 'markets', label: 'Markets & Industry', enabled: true },
    { id: 'social', label: 'Social Feed', enabled: true },
    { id: 'science', label: 'Science & Discovery', enabled: true },
  ],
}

const VALID_SLOTS: ProphetSlot[] = ['morning', 'evening']
const VALID_ROLES: ProphetStoryRole[] = ['lead', 'feature', 'standard', 'brief', 'pull_quote', 'image_led']
const VALID_LAYOUTS: ProphetLayoutIntent[] = ['feature', 'columns', 'briefs', 'gallery', 'essay']
const ACCENTS: ProphetAccent[] = ['rose', 'azure', 'slate', 'mint', 'gold']
const SECTION_ACCENTS: Record<string, ProphetAccent> = {
  breaking: 'rose',
  tech: 'azure',
  markets: 'gold',
  social: 'slate',
  science: 'mint',
}

const asText = (value: unknown): string => (typeof value === 'string' ? value.trim() : '')
const asObject = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null
const asTextList = (value: unknown): string[] =>
  Array.isArray(value) ? value.map((item) => asText(item)).filter(Boolean) : []

export const prophetAccent = (sectionId: string | undefined, key: string): ProphetAccent => {
  const normalized = (sectionId || '').trim().toLowerCase()
  if (SECTION_ACCENTS[normalized]) return SECTION_ACCENTS[normalized]
  let hash = 0
  for (let index = 0; index < key.length; index += 1) {
    hash = (hash * 31 + key.charCodeAt(index)) % 9973
  }
  return ACCENTS[hash % ACCENTS.length]
}

const normalizeStory = (
  raw: unknown,
  sectionId: string | undefined,
): ProphetStory | null => {
  const record = asObject(raw)
  if (!record) return null
  const headline = asText(record.headline)
  if (!headline) return null
  const role = asText(record.role).toLowerCase() as ProphetStoryRole
  const source = asObject(record.source)
  const image = asObject(record.image)
  const imageUrl = image ? asText(image.url) : ''
  const importanceRaw = Number(record.importance)
  return {
    id: asText(record.id) || `story_${headline.slice(0, 24)}`,
    headline,
    dek: asText(record.dek) || undefined,
    body: asTextList(record.body),
    role: VALID_ROLES.includes(role) ? role : 'standard',
    importance: Number.isFinite(importanceRaw) ? Math.max(0, Math.min(100, importanceRaw)) : 50,
    source: source
      ? { name: asText(source.name) || undefined, url: asText(source.url) || undefined }
      : undefined,
    publishedAt: asText(record.published_at) || undefined,
    image: imageUrl
      ? {
          url: imageUrl,
          caption: asText(image?.caption) || undefined,
          credit: asText(image?.credit) || undefined,
        }
      : undefined,
    whySelected: asText(record.why_selected) || undefined,
    tags: asTextList(record.tags),
    sectionId,
    accent: prophetAccent(sectionId, headline),
  }
}

export const normalizeProphetEdition = (raw: unknown): ProphetEdition | null => {
  const outer = asObject(raw)
  if (!outer) return null
  const payload = asObject(outer.payload) || outer
  const sectionsRaw = Array.isArray(payload.sections) ? payload.sections : []
  const sections: ProphetSection[] = []
  for (const sectionRaw of sectionsRaw) {
    const record = asObject(sectionRaw)
    if (!record) continue
    const sectionId = asText(record.id) || `section_${sections.length + 1}`
    const layout = asText(record.layout).toLowerCase() as ProphetLayoutIntent
    const stories = (Array.isArray(record.stories) ? record.stories : [])
      .map((story) => normalizeStory(story, sectionId))
      .filter((story): story is ProphetStory => story !== null)
    if (stories.length === 0) continue
    sections.push({
      id: sectionId,
      label: asText(record.label) || sectionId.replace(/_/g, ' '),
      layout: VALID_LAYOUTS.includes(layout) ? layout : 'columns',
      quietDayText: asText(record.quiet_day_text) || undefined,
      stories,
    })
  }
  const lead = normalizeStory(payload.lead, undefined)
  if (lead) lead.role = 'lead'
  if (!lead && sections.length === 0) return null
  const slotRaw = asText(payload.slot).toLowerCase() as ProphetSlot
  const revisionRaw = Number(outer.revision)
  return {
    editionId: asText(outer.edition_id) || asText(payload.edition_id) || 'prophet_edition',
    editionDate: asText(payload.edition_date) || asText(outer.edition_date),
    slot: VALID_SLOTS.includes(slotRaw) ? slotRaw : 'morning',
    schemaVersion: Number(payload.schema_version) || 1,
    editorNote: asText(payload.editor_note) || undefined,
    lead,
    sections,
    footer: asText(payload.footer) || undefined,
    publishedAt: asText(outer.updated_at) || asText(outer.created_at) || undefined,
    revision: Number.isFinite(revisionRaw) && revisionRaw > 0 ? revisionRaw : 1,
  }
}

export const editionStoryCount = (edition: ProphetEdition): number =>
  (edition.lead ? 1 : 0) + edition.sections.reduce((total, section) => total + section.stories.length, 0)

const hasImage = (story: ProphetStory): boolean => Boolean(story.image?.url)

export const resolveProphetLayout = (section: ProphetSection): ProphetLayoutMode => {
  const count = section.stories.length
  const images = section.stories.filter(hasImage).length
  if (count <= 1) return 'm1'
  switch (section.layout) {
    case 'briefs':
      return 'm4'
    case 'gallery':
      return images >= 2 ? 'm5' : count === 2 ? 'm2' : 'm3'
    case 'feature':
    case 'essay':
      return 'm3'
    case 'columns':
    default:
      if (count === 2) return 'm2'
      if (count === 3) return 'm3'
      if (images >= 2 && count <= 4) return 'm5'
      if (count <= 6) return 'm4'
      return 'm3'
  }
}

export const formatProphetRelativeTime = (value: string | undefined, nowMs: number = Date.now()): string => {
  const text = asText(value)
  if (!text) return ''
  const timestamp = Date.parse(text)
  if (!Number.isFinite(timestamp)) return ''
  const deltaMs = Math.max(0, nowMs - timestamp)
  const minutes = Math.floor(deltaMs / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days}d ago`
  return new Date(timestamp).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export const formatProphetDateline = (editionDate: string | undefined): string => {
  const text = asText(editionDate)
  if (!text) return ''
  const parsed = new Date(`${text}T12:00:00`)
  if (!Number.isFinite(parsed.getTime())) return text
  return parsed.toLocaleDateString(undefined, {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
    year: 'numeric',
  })
}

export const normalizeProphetSettings = (raw: unknown): ProphetSettings => {
  const record = asObject(raw)
  if (!record) return EMPTY_PROPHET_SETTINGS
  const sections = Array.isArray(record.sections)
    ? record.sections
        .map((section) => {
          const entry = asObject(section)
          if (!entry) return null
          const id = asText(entry.id)
          if (!id) return null
          return {
            id,
            label: asText(entry.label) || id.replace(/_/g, ' '),
            enabled: entry.enabled !== false,
          }
        })
        .filter((section): section is ProphetSectionSetting => section !== null)
    : []
  const maxStoriesRaw = Number(record.max_stories)
  return {
    enabled: record.enabled !== false,
    morningTime: asText(record.morning_time) || EMPTY_PROPHET_SETTINGS.morningTime,
    eveningEnabled: record.evening_enabled !== false,
    eveningTime: asText(record.evening_time) || EMPTY_PROPHET_SETTINGS.eveningTime,
    maxStories:
      Number.isFinite(maxStoriesRaw) && maxStoriesRaw >= 5 && maxStoriesRaw <= 30
        ? Math.round(maxStoriesRaw)
        : EMPTY_PROPHET_SETTINGS.maxStories,
    notificationsEnabled: record.notifications_enabled !== false,
    sections: sections.length > 0 ? sections : EMPTY_PROPHET_SETTINGS.sections,
  }
}

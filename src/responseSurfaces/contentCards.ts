import type {
  ContentCardAction,
  ContentCardBlock,
  ContentCardBrand,
  ContentCardGroup,
  ContentCardPreset,
  ContentCardSection,
} from './types'

const PRESETS = new Set<ContentCardPreset>(['social_post', 'copy_payload', 'option_set', 'checklist'])
const BRANDS = new Set<ContentCardBrand>(['x', 'gmail', 'github', 'generic'])
const MAX_SECTIONS = 8
const MAX_ACTIONS = 3

const clip = (value: unknown, limit: number) => {
  const text = String(value ?? '').replace(/\u0000/g, '').trim()
  if (!text) return ''
  return text.length > limit ? `${text.slice(0, Math.max(0, limit - 1)).trimEnd()}…` : text
}

const stringList = (value: unknown, limit: number) => {
  const source = Array.isArray(value) ? value : typeof value === 'string' ? [value] : []
  const items: string[] = []
  const seen = new Set<string>()
  for (const item of source) {
    const text = clip(item, 80)
    if (!text || seen.has(text)) continue
    seen.add(text)
    items.push(text)
    if (items.length >= limit) break
  }
  return items
}

const safeHttpsUrl = (value: unknown) => {
  const raw = String(value ?? '').trim()
  if (!raw || raw.length > 500 || /\s/.test(raw)) return null
  try {
    const url = new URL(raw)
    if (url.protocol !== 'https:') return null
    const host = url.hostname.toLowerCase()
    if (!host || host === 'localhost' || host === '127.0.0.1' || host.endsWith('.local')) return null
    return url.toString()
  } catch {
    return null
  }
}

const normalizeSection = (raw: any): ContentCardSection | null => {
  const type = String(raw?.type || '').trim().toLowerCase().replace(/-/g, '_')
  if (type === 'text' || type === 'quote') {
    const text = clip(raw.text || raw.body, 4000)
    return text ? { type, text } : null
  }
  if (type === 'code') {
    const code = clip(raw.code || raw.text, 6000)
    if (!code) return null
    const language = clip(raw.language, 32) || null
    return { type: 'code', code, language }
  }
  if (type === 'chips') {
    const items = stringList(raw.items || raw.chips, 16)
    return items.length > 0 ? { type: 'chips', items } : null
  }
  if (type === 'list') {
    const items = stringList(raw.items, 12)
    if (items.length <= 0) return null
    const style = String(raw.style || '').trim().toLowerCase() || null
    return { type: 'list', items, style }
  }
  if (type === 'key_value') {
    const source = Array.isArray(raw.rows || raw.items || raw.fields) ? (raw.rows || raw.items || raw.fields) : []
    const rows: Array<{ label: string; value: string }> = []
    for (const item of source) {
      const label = clip(item?.label || item?.key, 40)
      const value = clip(item?.value || item?.text, 240)
      if (label && value) rows.push({ label, value })
      if (rows.length >= 12) break
    }
    return rows.length > 0 ? { type: 'key_value', rows } : null
  }
  return null
}

const normalizeActions = (raw: unknown, copyText: string): ContentCardAction[] => {
  const source = Array.isArray(raw) ? raw : [{ type: 'copy' }]
  const actions: ContentCardAction[] = []
  const seen = new Set<string>()
  for (const item of source) {
    if (actions.length >= MAX_ACTIONS) break
    const type = String((item as any)?.type || item || '').trim().toLowerCase().replace(/-/g, '_')
    if (seen.has(type)) continue
    if (type === 'copy') {
      const text = clip((item as any)?.text || copyText, 4000)
      if (!text) continue
      actions.push({ type: 'copy', label: clip((item as any)?.label, 24) || 'Copy', text })
      seen.add(type)
      continue
    }
    if (type === 'open_url') {
      const url = safeHttpsUrl((item as any)?.url)
      if (!url) continue
      actions.push({ type: 'open_url', label: clip((item as any)?.label, 24) || 'Open', url })
      seen.add(type)
    }
  }
  return actions
}

const normalizeGroup = (raw: any): ContentCardGroup | null => {
  const group = raw?.group && typeof raw.group === 'object' ? raw.group : {}
  const id = clip(raw?.group_id || raw?.groupId || group.id, 64)
  if (!id) return null
  const indexRaw = Number(raw?.variant_index ?? raw?.variantIndex ?? group.index)
  const totalRaw = Number(raw?.variant_total ?? raw?.variantTotal ?? group.total)
  return {
    id,
    index: Number.isFinite(indexRaw) && indexRaw > 0 ? Math.floor(indexRaw) : null,
    total: Number.isFinite(totalRaw) && totalRaw > 0 ? Math.floor(totalRaw) : null,
  }
}

const firstText = (sections: ContentCardSection[], body: string) => {
  if (body) return body
  for (const section of sections) {
    if (section.type === 'text' || section.type === 'quote') return section.text
    if (section.type === 'code') return section.code
  }
  return ''
}

export const normalizeContentCard = (raw: any, fallbackId: string): ContentCardBlock | null => {
  if (!raw || typeof raw !== 'object') return null
  const type = String(raw.type || '').trim()
  if (type && type !== 'content_card') return null
  const presetRaw = String(raw.preset || '').trim().toLowerCase().replace(/-/g, '_')
  const preset = PRESETS.has(presetRaw as ContentCardPreset) ? presetRaw as ContentCardPreset : null
  const brandRaw = String(raw.brand || raw.platform || '').trim().toLowerCase()
  const brand = brandRaw === 'twitter' || brandRaw === 'x.com'
    ? 'x'
    : BRANDS.has(brandRaw as ContentCardBrand) ? brandRaw as ContentCardBrand : (brandRaw ? 'generic' : null)
  const title = clip(raw.title, 120)
  const subtitle = clip(raw.subtitle, 200) || null
  const body = clip(raw.body, 4000) || null
  const sections = (Array.isArray(raw.sections) ? raw.sections : [])
    .map(normalizeSection)
    .filter(Boolean)
    .slice(0, MAX_SECTIONS) as ContentCardSection[]
  if (!title && sections.length <= 0 && !body) return null
  const copySource = firstText(sections, body || '')
  const characterLimitRaw = Number(raw.character_limit ?? raw.characterLimit ?? raw.limit)
  const characterLimit = Number.isFinite(characterLimitRaw) && characterLimitRaw > 0
    ? Math.floor(characterLimitRaw)
    : (preset === 'social_post' ? 280 : null)
  const characterCountRaw = Number(raw.character_count ?? raw.characterCount)
  const characterCount = Number.isFinite(characterCountRaw) && characterCountRaw >= 0
    ? Math.floor(characterCountRaw)
    : (copySource ? copySource.length : null)
  return {
    id: clip(raw.id, 80) || fallbackId,
    type: 'content_card',
    schemaVersion: Number(raw.schema_version ?? raw.schemaVersion) || 1,
    preset,
    brand,
    title: title || (preset === 'social_post' && brand === 'x' ? 'X draft' : 'Card'),
    subtitle,
    body,
    sections,
    actions: normalizeActions(raw.actions, copySource),
    tags: stringList(raw.tags, 16),
    mentions: stringList(raw.mentions, 16),
    characterCount,
    characterLimit,
    group: normalizeGroup(raw),
    fallbackText: clip(raw.fallback_text || raw.fallbackText, 8000) || null,
  }
}

export const groupContentCardBlocks = (blocks: ContentCardBlock[]) => {
  const grouped: Array<{ kind: 'card'; card: ContentCardBlock } | { kind: 'stack'; cards: ContentCardBlock[] }> = []
  let stack: ContentCardBlock[] = []
  const flush = () => {
    if (stack.length === 1) grouped.push({ kind: 'card', card: stack[0] })
    else if (stack.length > 1) grouped.push({ kind: 'stack', cards: stack })
    stack = []
  }
  for (const card of blocks) {
    const groupId = card.group?.id
    if (!groupId) {
      flush()
      grouped.push({ kind: 'card', card })
      continue
    }
    if (stack.length > 0 && stack[0].group?.id !== groupId) flush()
    stack.push(card)
  }
  flush()
  return grouped
}

export const contentCardKicker = (card: ContentCardBlock) => {
  if (card.preset === 'social_post' && card.brand === 'x') return 'X draft'
  if (card.preset === 'social_post') return 'Draft post'
  if (card.brand === 'x') return 'On X'
  if (card.preset === 'checklist') return 'Checklist'
  if (card.preset === 'option_set') return 'Options'
  if (card.preset === 'copy_payload') return 'Copy'
  return 'Card'
}

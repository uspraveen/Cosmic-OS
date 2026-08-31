/**
 * Who is doing the work, resolved from one activity-log entry.
 *
 * The flow list used to say "step 4" and leave the reader to parse a sentence
 * to find out that step 4 was Gmail. A mark is faster than a sentence: the eye
 * lands on the Gmail red or the terminal bracket and knows what happened before
 * it has read a word. That is the whole job of this module -- turn an entry into
 * a mark, a name, and (when the work touched real sites) the domains behind it.
 *
 * Written to be platform-free so the desktop app and the React Native app share
 * one answer. Desktop hands us camelCase (`agentId`), mobile hands us the raw
 * gateway snake_case (`agent_id`); both are read here so neither has to convert.
 */

export type GlyphId =
  // Brand marks. Drawn as the real logo, in the real colour.
  | 'x'
  | 'youtube'
  | 'gmail'
  | 'firecrawl'
  // Tinted functional marks -- drawn as their own glyph rather than a logo we
  // would only approximate. Calendar and schedule stay neutral monoline marks.
  | 'calendar'
  | 'gdocs'
  | 'gsheets'
  // Neutral functional marks.
  | 'web'
  | 'search'
  | 'sandbox'
  | 'terminal'
  | 'doc'
  | 'table'
  | 'slides'
  | 'diagram'
  | 'map'
  | 'image'
  | 'memory'
  | 'recall'
  | 'schedule'
  | 'think'
  | 'brain'
  | 'agent'
  | 'cosmic'

export interface AgentSignal {
  /** Which mark to draw. */
  glyph: GlyphId
  /** Short human name for the actor, for tooltips and screen readers. */
  label: string
  /** Brand colour, only where the mark is a real logo or a real product tint. */
  brand?: string
  /** Domains this entry actually touched, for a favicon cluster. */
  domains: string[]
}

/**
 * Both shapes of an activity entry: the desktop's normalised camelCase and the
 * gateway's snake_case as mobile stores it.
 */
export interface FlowEntryLike {
  label?: string | null
  detail?: string | null
  kind?: string | null
  stage?: string | null
  status?: string | null
  flowRole?: string | null
  flow_role?: string | null
  agentId?: string | null
  agent_id?: string | null
  agentLabel?: string | null
  agent_label?: string | null
  intent?: string | null
}

const BRAND = {
  x: '#FFFFFF',
  youtube: '#FF0033',
  gmail: '#EA4335',
  firecrawl: '#FF6A1F',
  gdocs: '#4285F4',
  gsheets: '#34A853',
  map: '#EA4335',
} as const

const text = (value: unknown) => String(value ?? '').trim()
const lower = (value: unknown) => text(value).toLowerCase()

const readAgentId = (entry: FlowEntryLike) => lower(entry.agentId ?? entry.agent_id)
const readAgentLabel = (entry: FlowEntryLike) => text(entry.agentLabel ?? entry.agent_label)

/**
 * Real TLDs we are willing to believe when a domain appears bare in a sentence.
 * Deliberately excludes anything that doubles as a file extension -- activity
 * text is full of `cosmic.md`, `bundle.duckdb`, `runtime.py`, and a naive
 * "dot followed by letters" rule turns every one of them into a website.
 */
const TLDS = new Set([
  'com', 'org', 'net', 'edu', 'gov', 'mil', 'int',
  'io', 'ai', 'co', 'dev', 'app', 'xyz', 'me', 'tv', 'news', 'gg', 'fm', 'cc',
  'info', 'biz', 'cloud', 'tech', 'site', 'online', 'blog', 'live', 'store',
  'page', 'wiki', 'press', 'media', 'design', 'studio', 'space', 'world',
  'life', 'today', 'agency', 'systems', 'network', 'digital', 'works', 'group',
  'team', 'zone', 'social', 'chat', 'email', 'finance', 'health', 'science',
  'uk', 'us', 'ca', 'au', 'de', 'fr', 'jp', 'cn', 'in', 'br', 'mx', 'kr',
  'nl', 'se', 'no', 'dk', 'fi', 'es', 'it', 'ru', 'ch', 'at', 'be', 'pt',
  'pl', 'ie', 'nz', 'sg', 'hk', 'tw', 'za', 'ae', 'il', 'tr',
])

/** Second-level suffixes that are never the site itself: `bbc.co.uk`, not `co.uk`. */
const SECOND_LEVEL = new Set([
  'co', 'com', 'org', 'net', 'gov', 'edu', 'ac', 'mil',
])

const URL_RE = /https?:\/\/[^\s<>"'`)\]},]+/gi
const BARE_RE = /(?<![@\w.-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,24})(?![\w-])/gi

const normalizeHost = (raw: string): string | null => {
  let host = raw.trim().toLowerCase()
  if (!host) return null
  host = host.replace(/^www\./, '').replace(/\.+$/, '')
  if (!host.includes('.')) return null
  const parts = host.split('.')
  const tld = parts[parts.length - 1]
  if (!TLDS.has(tld)) return null
  // A domain has to have a name, not just a suffix.
  if (parts.length < 2) return null
  if (!/[a-z]/.test(parts[0])) return null
  return host
}

/**
 * Domains mentioned in an activity line. Full URLs are trusted outright; bare
 * hostnames have to clear the TLD list above.
 */
export const extractDomains = (value: unknown, limit = 8): string[] => {
  const source = text(value)
  if (!source) return []
  const found: string[] = []
  const seen = new Set<string>()
  const push = (host: string | null) => {
    if (!host || seen.has(host) || found.length >= limit) return
    seen.add(host)
    found.push(host)
  }

  let match: RegExpExecArray | null
  const urlRe = new RegExp(URL_RE.source, 'gi')
  while ((match = urlRe.exec(source)) !== null) {
    try {
      push(normalizeHost(new URL(match[0]).hostname))
    } catch {
      /* a malformed URL just means no favicon for this one */
    }
  }

  const bareRe = new RegExp(BARE_RE.source, 'gi')
  while ((match = bareRe.exec(source)) !== null) {
    push(normalizeHost(match[1]))
  }

  return found
}

/** `mail.google.com` -> `google`, `bbc.co.uk` -> `bbc`. For short site labels. */
export const domainName = (domain: string): string => {
  const parts = text(domain).toLowerCase().replace(/^www\./, '').split('.')
  if (parts.length <= 1) return parts[0] || ''
  let index = parts.length - 2
  if (index > 0 && SECOND_LEVEL.has(parts[index])) index -= 1
  return parts[index] || parts[0] || ''
}

/** Domains that are really a product we already have a mark for. */
const DOMAIN_GLYPHS: Array<[RegExp, GlyphId]> = [
  [/(^|\.)(x|twitter)\.com$/, 'x'],
  [/(^|\.)youtube\.com$|(^|\.)youtu\.be$/, 'youtube'],
  [/(^|\.)mail\.google\.com$/, 'gmail'],
  [/(^|\.)calendar\.google\.com$/, 'calendar'],
  [/(^|\.)docs\.google\.com$/, 'gdocs'],
  [/(^|\.)sheets\.google\.com$/, 'gsheets'],
]

const glyphForDomain = (domain: string): GlyphId | null => {
  for (const [pattern, glyph] of DOMAIN_GLYPHS) {
    if (pattern.test(domain)) return glyph
  }
  return null
}

/** agent_id fragment -> mark. First match wins, so order is specificity. */
const AGENT_GLYPHS: Array<[string, GlyphId, string]> = [
  ['x-twitter-search', 'x', 'X'],
  ['gmail', 'gmail', 'Gmail'],
  ['email', 'gmail', 'Email'],
  ['calendar', 'calendar', 'Calendar'],
  ['google-docs', 'gdocs', 'Google Docs'],
  ['google-sheets', 'gsheets', 'Google Sheets'],
  ['docs-parser', 'doc', 'Documents'],
  ['tabular', 'table', 'Spreadsheets'],
  ['slide', 'slides', 'Slides'],
  ['diagram', 'diagram', 'Diagrams'],
  ['map', 'map', 'Maps'],
  ['image-generator', 'image', 'Images'],
  ['firecrawl', 'firecrawl', 'Firecrawl'],
  ['scheduler', 'schedule', 'Schedule'],
  ['alpha-agent', 'terminal', 'Alpha agent'],
  ['orchestrator', 'cosmic', 'Cosmic'],
]

/** intent prefix -> mark. Checked before the looser label sniffing. */
const INTENT_GLYPHS: Array<[RegExp, GlyphId, string]> = [
  [/^x\./, 'x', 'X'],
  [/^(gmail|email)\./, 'gmail', 'Gmail'],
  [/^calendar\./, 'calendar', 'Calendar'],
  // Both the Google Docs agent and the document parser live under `docs.`;
  // only create/edit are the Google product.
  [/^docs\.(create|edit|share|append)/, 'gdocs', 'Google Docs'],
  [/^docs\./, 'doc', 'Documents'],
  [/^sheets\./, 'gsheets', 'Google Sheets'],
  [/^tabular\./, 'table', 'Spreadsheets'],
  [/^firecrawl\./, 'firecrawl', 'Firecrawl'],
  [/^alpha\./, 'terminal', 'Alpha agent'],
  [/^diagram\./, 'diagram', 'Diagrams'],
  [/^map\./, 'map', 'Maps'],
  [/^slide(s)?\./, 'slides', 'Slides'],
  [/^image\./, 'image', 'Images'],
  [/^memory\./, 'memory', 'Memory'],
  [/^session\./, 'recall', 'Session'],
  [/^scheduler\./, 'schedule', 'Schedule'],
  [/^reminder\./, 'schedule', 'Schedule'],
]

/**
 * Phrases the orchestrator writes for its own local tools. These entries carry
 * no agent id at all -- the sentence is the only evidence of what ran, so it is
 * matched against the exact wording `_summarize_local_tool_activity` produces.
 */
const LABEL_GLYPHS: Array<[RegExp, GlyphId, string]> = [
  [/\bcode sandbox\b/, 'sandbox', 'Code sandbox'],
  [/\bfirecrawl\b/, 'firecrawl', 'Firecrawl'],
  [/\bweb search(es)?\b|\bsearched the web\b|\bperplexity\b|\bresearch(ed)?\b/, 'search', 'Research'],
  [/\bsession (history|state|turns)\b|\brevisit(ed|ing)?\b|\bnotebook\b|\bexact history\b|\bdetailed session history\b/, 'recall', 'Session'],
  [/\bsearched memory\b|\bmemory block\b|\bcore fact\b|\bremember(ed)?\b|\bfull memory\b/, 'memory', 'Memory'],
  [/\bspecialist catalog\b|\bspecialist intents\b/, 'agent', 'Specialists'],
  [/\bdelegat(ed|ing|e)\b/, 'agent', 'Specialist'],
  [/\bcapability wishlist\b|\bcustom tool\b/, 'cosmic', 'Cosmic'],
  [/\breminder\b|\bevent automation\b|\bschedul(ed|e)\b|\bcron\b/, 'schedule', 'Schedule'],
  [/\bspreadsheet\b|\bworkbook\b|\bsheet\b/, 'table', 'Spreadsheets'],
  [/\bslide(s|deck)?\b|\bpresentation\b/, 'slides', 'Slides'],
  [/\bdiagram\b/, 'diagram', 'Diagrams'],
  [/\bimage\b|\bpicture\b|\bcollage\b/, 'image', 'Images'],
  [/\bterminal\b|\bcursor cli\b|\bcodex\b|\balpha agent\b/, 'terminal', 'Alpha agent'],
  [/\breason(ing|ed)?\b|\bthinking\b|\bthought\b/, 'think', 'Reasoning'],
  [/\bwrit(ing|es|e) the response\b|\bcomposing\b/, 'cosmic', 'Cosmic'],
]

const signal = (glyph: GlyphId, label: string, domains: string[]): AgentSignal => {
  const brand = (BRAND as Record<string, string | undefined>)[glyph]
  return brand ? { glyph, label, brand, domains } : { glyph, label, domains }
}

/**
 * The mark, name, and site list for one flow entry.
 *
 * Evidence is ranked by how much it can be trusted: an explicit agent id beats
 * an intent, an intent beats a sentence, and a sentence beats the fallback. A
 * recognised product domain outranks all of them, because a line that names
 * `youtube.com` is about YouTube whichever agent happened to fetch it.
 */
export const resolveAgentSignal = (entry: FlowEntryLike): AgentSignal => {
  const label = text(entry.label)
  const detail = text(entry.detail)
  const body = `${label} ${detail}`
  const domains = extractDomains(body)

  // A reasoning escalation/reset surfaces with an explicit kind from the
  // orchestrator rather than relying on label wording.
  if (text(entry.kind) === 'thinking') {
    return signal('brain', 'Thinking deeper', domains)
  }

  for (const domain of domains) {
    const glyph = glyphForDomain(domain)
    if (glyph) {
      const name = domainName(domain)
      // The tile is already this site's logo, so repeating it as a favicon
      // would say the same thing twice in the width of one row.
      const rest = domains.filter((item) => glyphForDomain(item) !== glyph)
      return signal(glyph, name ? name.charAt(0).toUpperCase() + name.slice(1) : domain, rest)
    }
  }

  const agentId = readAgentId(entry)
  if (agentId) {
    for (const [fragment, glyph, name] of AGENT_GLYPHS) {
      if (agentId.includes(fragment)) return signal(glyph, readAgentLabel(entry) || name, domains)
    }
  }

  const intent = lower(entry.intent)
  if (intent) {
    for (const [pattern, glyph, name] of INTENT_GLYPHS) {
      if (pattern.test(intent)) return signal(glyph, readAgentLabel(entry) || name, domains)
    }
  }

  const haystack = body.toLowerCase()
  for (const [pattern, glyph, name] of LABEL_GLYPHS) {
    if (pattern.test(haystack)) return signal(glyph, name, domains)
  }

  // A line with sites and nothing else identifying it is web work.
  if (domains.length > 0) return signal('web', 'Web', domains)

  const agentName = readAgentLabel(entry)
  if (agentName) return signal('agent', agentName, domains)
  return signal('cosmic', 'Cosmic', domains)
}

/**
 * The distinct marks across a run, newest evidence last, for the preview shown
 * on a collapsed Flow header. Collapsing the section by default only works if
 * the header still says who was involved.
 */
export const summarizeAgentSignals = (
  entries: FlowEntryLike[] | undefined | null,
  limit = 4,
): AgentSignal[] => {
  if (!entries || entries.length === 0) return []
  const byGlyph = new Map<GlyphId, AgentSignal>()
  for (const entry of entries) {
    const resolved = resolveAgentSignal(entry)
    // `cosmic` is the fallback for "the orchestrator did something ordinary";
    // it says nothing a reader wants from a summary, so it only shows up when
    // there is genuinely nothing else to show.
    const existing = byGlyph.get(resolved.glyph)
    if (existing) {
      if (resolved.domains.length > existing.domains.length) byGlyph.set(resolved.glyph, resolved)
      continue
    }
    byGlyph.set(resolved.glyph, resolved)
  }
  const all = [...byGlyph.values()]
  const meaningful = all.filter((item) => item.glyph !== 'cosmic' && item.glyph !== 'think')
  return (meaningful.length > 0 ? meaningful : all).slice(0, limit)
}

/**
 * The sentence, with a lead-in that only names the actor removed.
 *
 * The orchestrator writes "X twitter search agent: Searching X for ...". Beside
 * a mark that is already the X logo, the first four words are the mark said
 * again in words -- and they cost a third of a footer that only gets one line.
 * Only stripped when we actually drew a mark for that actor, so a line with the
 * generic mark keeps whatever context its wording carries.
 */
export const stripActorPrefix = (label: unknown, signal: AgentSignal): string => {
  const value = text(label)
  if (!value || signal.glyph === 'cosmic') return value
  const match = /^.{2,44}?\b(?:agent|specialist|subagent)\s*:\s*(\S.*)$/is.exec(value)
  if (!match) return value
  const rest = match[1].trim()
  if (rest.length < 3) return value
  return rest.charAt(0).toUpperCase() + rest.slice(1)
}

/**
 * The tail of a reasoning stream, for the header of a collapsed Thinking
 * section. Collapsing that section by default trades a wall of text for one
 * live line, which is the trade only if the line keeps moving -- so this takes
 * the newest complete thought rather than the opening one.
 */
export const thinkingPreview = (value: unknown, limit = 96): string => {
  const lines = text(value)
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
  const last = lines[lines.length - 1] || ''
  if (last.length <= limit) return last
  return `${last.slice(0, Math.max(0, limit - 1)).trimEnd()}…`
}

/** Favicon for a domain, using the service the sources list already uses. */
export const faviconUrl = (domain: string, size = 64): string =>
  `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=${size}`

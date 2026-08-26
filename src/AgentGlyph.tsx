import { memo, useState } from 'react'
import {
  Boxes,
  CalendarDays,
  Database,
  FileText,
  Globe,
  Image as ImageIcon,
  MapPin,
  Orbit,
  Presentation,
  Search,
  Sheet,
  Sparkles,
  SquareTerminal,
  Table2,
  Terminal,
  Workflow,
} from 'lucide-react'
import { faviconUrl, type AgentSignal, type GlyphId } from './agentSignals'

/**
 * The marks that go in front of a flow step.
 *
 * Everything functional is a Lucide icon, because the rest of the app already
 * draws its chrome from that set and a hand-rolled SVG would sit at a different
 * stroke weight next to it. Only the three real logos are hand-written paths --
 * Lucide has no brand icons, and X, YouTube and Gmail are the marks a reader
 * actually recognises at 12px.
 *
 * A Google product with no logo here (Calendar, Docs, Sheets) gets its own
 * neutral glyph in the product's colour instead of a logo drawn from memory.
 */

const BrandX = ({ size }: { size: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden>
    <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231Zm-1.161 17.52h1.833L7.084 4.126H5.117Z" />
  </svg>
)

const BrandYouTube = ({ size }: { size: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden>
    {/* evenodd carves the play triangle out of the body, so the mark reads
        correctly on any tile colour instead of needing a matching fill. */}
    <path
      fillRule="evenodd"
      d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814ZM9.545 15.568V8.432L15.818 12l-6.273 3.568Z"
    />
  </svg>
)

const BrandGmail = ({ size }: { size: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden>
    <path d="M24 5.457v13.909c0 .904-.732 1.636-1.636 1.636h-3.819V11.73L12 16.64l-6.545-4.91v9.273H1.636A1.636 1.636 0 0 1 0 19.366V5.457c0-2.023 2.309-3.178 3.927-1.964L5.455 4.64 12 9.548l6.545-4.91 1.528-1.145C21.69 2.28 24 3.434 24 5.457Z" />
  </svg>
)

const LUCIDE: Partial<Record<GlyphId, typeof Globe>> = {
  calendar: CalendarDays,
  gdocs: FileText,
  gsheets: Sheet,
  web: Globe,
  search: Search,
  sandbox: SquareTerminal,
  terminal: Terminal,
  doc: FileText,
  table: Table2,
  slides: Presentation,
  diagram: Workflow,
  map: MapPin,
  image: ImageIcon,
  memory: Database,
  think: Sparkles,
  agent: Boxes,
  cosmic: Orbit,
}

export const GlyphMark = ({ glyph, size = 12 }: { glyph: GlyphId; size?: number }) => {
  if (glyph === 'x') return <BrandX size={size} />
  if (glyph === 'youtube') return <BrandYouTube size={size} />
  if (glyph === 'gmail') return <BrandGmail size={size} />
  const Icon = LUCIDE[glyph] || Orbit
  return <Icon size={size} strokeWidth={1.9} aria-hidden />
}

/**
 * One site's favicon, from the same service the sources list already uses.
 * Falls back to the globe rather than leaving a broken-image gap, which is the
 * common case for intranet hosts and anything behind a login.
 */
const DomainChip = memo(({ domain, size }: { domain: string; size: number }) => {
  const [failed, setFailed] = useState(false)
  if (failed) {
    return (
      <span className="agent-domain-chip is-fallback" title={domain} style={{ width: size, height: size }}>
        <Globe size={Math.round(size * 0.62)} strokeWidth={2} aria-hidden />
      </span>
    )
  }
  return (
    <img
      className="agent-domain-chip"
      style={{ width: size, height: size }}
      src={faviconUrl(domain)}
      alt=""
      title={domain}
      loading="lazy"
      draggable={false}
      onError={() => setFailed(true)}
    />
  )
})
DomainChip.displayName = 'DomainChip'

/**
 * The overlapping favicon stack. Reads as "these places", at a glance, without
 * spending a line of the flow on a list of hostnames.
 */
export const DomainCluster = memo(({
  domains,
  max = 4,
  size = 15,
}: {
  domains: string[]
  max?: number
  size?: number
}) => {
  if (!domains || domains.length === 0) return null
  const shown = domains.slice(0, max)
  const overflow = domains.length - shown.length
  return (
    <span className="agent-domain-cluster" title={domains.join(', ')}>
      {shown.map((domain) => (
        <DomainChip key={domain} domain={domain} size={size} />
      ))}
      {overflow > 0 && <span className="agent-domain-more">+{overflow}</span>}
    </span>
  )
})
DomainCluster.displayName = 'DomainCluster'

/**
 * The tile a flow step leads with. `active` is the running step: it takes the
 * accent tint so the eye can find the live row in a long list without the row
 * having to move or pulse.
 */
export const AgentGlyph = memo(({
  signal,
  size = 22,
  iconSize,
  active = false,
  title,
}: {
  signal: AgentSignal
  size?: number
  iconSize?: number
  active?: boolean
  title?: string
}) => (
  <span
    className={`agent-glyph${active ? ' is-active' : ''}${signal.brand ? ' is-brand' : ''}`}
    style={{
      width: size,
      height: size,
      ...(signal.brand && !active ? { color: signal.brand } : null),
    }}
    title={title ?? signal.label}
    aria-hidden
  >
    <GlyphMark glyph={signal.glyph} size={iconSize ?? Math.round(size * 0.58)} />
  </span>
))
AgentGlyph.displayName = 'AgentGlyph'

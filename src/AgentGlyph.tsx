import { memo, useState, type ReactNode } from 'react'
import { faviconUrl, type AgentSignal, type GlyphId } from './agentSignals'

/**
 * The marks that lead a flow step, a collapsed section header, and the live
 * footer.
 *
 * Two families, on purpose, because the roster splits cleanly in two.
 *
 * Almost everything Cosmic delegates to is a real product -- Gmail, Google
 * Docs, Google Sheets, X, Firecrawl -- and a real product gets its real logo,
 * solid, in its own colours. A hand-drawn "page with a folded corner" standing
 * in for Google Docs is strictly worse than the mark the reader already knows,
 * and it is why a column of these looked generic however well each was drawn.
 *
 * What is left is Cosmic's own machinery -- a sandbox, a memory lookup, a
 * delegation -- which has no logo to borrow. Those are monoline: one stroke
 * weight, round caps, drawn large and lightly. Solid fills at this size turn
 * into blobs, and six blobs that are all rounded rectangles are six marks a
 * reader cannot tell apart at a glance.
 *
 * There is no tile behind either family. A logo wants the panel behind it, not
 * a grey chip, and the fixed-width slot does the aligning instead.
 */

type MarkProps = { size: number }

/** Cosmic's own machinery. One stroke weight, so the set reads as one hand. */
const line = (size: number, children: ReactNode) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.7}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden
  >
    {children}
  </svg>
)

/** A real logo, in its real colours. */
const logo = (size: number, children: ReactNode) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
    {children}
  </svg>
)

/* -- Product logos ------------------------------------------------------- */

// X is the one logo whose colour is the surface it sits on, so it follows the
// tint and flips black-on-light / white-on-dark.
const MarkX = ({ size }: MarkProps) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden>
    <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231Zm-1.161 17.52h1.833L7.084 4.126H5.117Z" />
  </svg>
)

const MarkYouTube = ({ size }: MarkProps) =>
  logo(
    size,
    <>
      <path
        d="M23.5 6.2a3 3 0 0 0-2.12-2.14C19.5 3.55 12 3.55 12 3.55s-7.5 0-9.38.51A3 3 0 0 0 .5 6.2C0 8.07 0 12 0 12s0 3.93.5 5.81a3 3 0 0 0 2.12 2.14c1.88.5 9.38.5 9.38.5s7.5 0 9.38-.5a3 3 0 0 0 2.12-2.14C24 15.93 24 12 24 12s0-3.93-.5-5.8Z"
        fill="#FF0033"
      />
      <path d="M9.55 15.57V8.43L15.82 12l-6.27 3.57Z" fill="#FFFFFF" />
    </>,
  )

// The whole Gmail mark, not a red letter. The white envelope is half of what
// makes it recognisable this small, and the Google colours are the other half.
const MarkGmail = ({ size }: MarkProps) =>
  logo(
    size,
    <>
      <path d="M2.5 19.9h3V11.4L1 8v10.4a1.5 1.5 0 0 0 1.5 1.5Z" fill="#4285F4" />
      <path d="M18.5 19.9h3a1.5 1.5 0 0 0 1.5-1.5V8l-4.5 3.4Z" fill="#34A853" />
      <path d="M18.5 5.6v5.8L23 8V6.4c0-1.86-2.12-2.92-3.6-1.8Z" fill="#FBBC04" />
      <path d="M5.5 11.4V5.6L12 10.5l6.5-4.9v5.8L12 16.3Z" fill="#FFFFFF" />
      <path d="M1 6.4V8l4.5 3.4V5.6L4.6 4.6C3.12 3.48 1 4.54 1 6.4Z" fill="#C5221F" />
      <path d="M5.5 5.6 12 10.5l6.5-4.9V4.5L12 9.4 5.5 4.5Z" fill="#EA4335" />
    </>,
  )

const GoogleDocPage = ({ body, fold }: { body: string; fold: string }) => (
  <>
    <path
      d="M6 1.6h8.2L19.6 7v14.1a1.5 1.5 0 0 1-1.5 1.5H6a1.5 1.5 0 0 1-1.5-1.5V3.1A1.5 1.5 0 0 1 6 1.6Z"
      fill={body}
    />
    <path d="M14.2 1.6 19.6 7h-3.9a1.5 1.5 0 0 1-1.5-1.5Z" fill={fold} />
  </>
)

const MarkGoogleDocs = ({ size }: MarkProps) =>
  logo(
    size,
    <>
      <GoogleDocPage body="#4285F4" fold="#A1C2FA" />
      <path d="M7.9 11.3h8.2M7.9 14.3h8.2M7.9 17.3h5.6" stroke="#FFFFFF" strokeWidth="1.5" strokeLinecap="round" />
    </>,
  )

const MarkGoogleSheets = ({ size }: MarkProps) =>
  logo(
    size,
    <>
      <GoogleDocPage body="#0F9D58" fold="#8ED1B1" />
      <path d="M7.5 11h9v8.2h-9Z" fill="#FFFFFF" />
      <path d="M7.5 15.1h9M12 11v8.2" stroke="#0F9D58" strokeWidth="1.4" />
    </>,
  )

// Firecrawl does most of the page reading and had been sharing a generic globe
// with everything else on the web. Its flame is unmistakable and shares a
// silhouette with nothing else in the set.
const MarkFirecrawl = ({ size }: MarkProps) =>
  logo(
    size,
    <>
      <path
        d="M12 1.9c.7 2.9 2.3 4.4 3.9 6 1.9 1.9 3.1 3.9 3.1 6.3a7 7 0 1 1-14 0c0-1.8.7-3.5 1.8-4.9.2 1.3.9 2.3 1.9 2.9C7.9 8.6 9 5 12 1.9Z"
        fill="#FF6A1F"
      />
      <path
        d="M12 22.1a4.3 4.3 0 0 1-4.3-4.3c0-2 1.5-3.4 2.7-4.7.7 1.6 1.7 2.3 2.9 2.8 1.7.8 3 2.1 3 4a4.3 4.3 0 0 1-4.3 2.2Z"
        fill="#FFC93F"
      />
    </>,
  )

/* -- Cosmic's own machinery ---------------------------------------------- */

const MarkWeb = ({ size }: MarkProps) =>
  line(
    size,
    <>
      <circle cx="12" cy="12" r="8.6" />
      <path d="M3.4 12h17.2" />
      <path d="M12 3.4c2.5 2.4 3.8 5.3 3.8 8.6S14.5 18.2 12 20.6c-2.5-2.4-3.8-5.3-3.8-8.6S9.5 5.8 12 3.4Z" />
    </>,
  )

const MarkSearch = ({ size }: MarkProps) =>
  line(size, <><circle cx="10.6" cy="10.6" r="6.9" /><path d="M15.7 15.7 21 21" /></>)

/** The orchestrator's own sandbox: the boxed prompt, the way OpenAI draws it. */
const MarkSandbox = ({ size }: MarkProps) =>
  line(
    size,
    <>
      <rect x="2.7" y="4" width="18.6" height="16" rx="3.4" />
      <path d="M7.4 10 10 12.6l-2.6 2.6" />
      <path d="M13 15.4h4.1" />
    </>,
  )

/** A machine we drive rather than own: the same prompt, no box around it. */
const MarkTerminal = ({ size }: MarkProps) =>
  line(size, <><path d="M3.8 6.2 9.6 12l-5.8 5.8" /><path d="M12.4 17.8h7.8" /></>)

const MarkDoc = ({ size }: MarkProps) =>
  line(
    size,
    <>
      <path d="M14 2.7H7.6a2.4 2.4 0 0 0-2.4 2.4v13.8a2.4 2.4 0 0 0 2.4 2.4h8.8a2.4 2.4 0 0 0 2.4-2.4V7.4Z" />
      <path d="M14 2.7v3.2a1.6 1.6 0 0 0 1.6 1.6h3.2" />
      <path d="M8.6 13.2h6.8M8.6 16.6h4.4" />
    </>,
  )

/** Rows under a header, wide -- so it is not one more rounded square. */
const MarkTable = ({ size }: MarkProps) =>
  line(
    size,
    <>
      <rect x="2.4" y="5" width="19.2" height="14" rx="2.4" />
      <path d="M2.4 9.6h19.2" />
      <path d="M2.4 14.3h19.2" />
      <path d="M9.4 9.6V19" />
    </>,
  )

const MarkSlides = ({ size }: MarkProps) =>
  line(
    size,
    <>
      <rect x="2.4" y="3.6" width="19.2" height="12.4" rx="2.4" />
      <path d="M6.8 7.8h10.4M6.8 11.6h6.2" />
      <path d="M12 16v3.2" />
      <path d="M8.6 20.9h6.8" />
    </>,
  )

const MarkDiagram = ({ size }: MarkProps) =>
  line(
    size,
    <>
      <rect x="8.8" y="2.6" width="6.4" height="4.8" rx="1.6" />
      <rect x="2.2" y="16.6" width="6.4" height="4.8" rx="1.6" />
      <rect x="15.4" y="16.6" width="6.4" height="4.8" rx="1.6" />
      <path d="M12 7.4v3.4M5.4 16.6v-3.4h13.2v3.4" />
    </>,
  )

const MarkMap = ({ size }: MarkProps) =>
  line(
    size,
    <>
      <path d="M12 21.6s7.3-6.1 7.3-11.5a7.3 7.3 0 0 0-14.6 0C4.7 15.5 12 21.6 12 21.6Z" />
      <circle cx="12" cy="10" r="2.7" />
    </>,
  )

const MarkImage = ({ size }: MarkProps) =>
  line(
    size,
    <>
      <rect x="2.6" y="3.8" width="18.8" height="16.4" rx="3" />
      <circle cx="8.3" cy="9.2" r="2.1" />
      <path d="M2.9 17.6 8.9 11l4 4.2 2.7-2.6 5.5 5.3" />
    </>,
  )

const MarkMemory = ({ size }: MarkProps) =>
  line(
    size,
    <>
      <ellipse cx="12" cy="5.4" rx="8.4" ry="3.4" />
      <path d="M3.6 5.4v6.4c0 1.9 3.8 3.4 8.4 3.4s8.4-1.5 8.4-3.4V5.4" />
      <path d="M3.6 11.8v6.4c0 1.9 3.8 3.4 8.4 3.4s8.4-1.5 8.4-3.4v-6.4" />
    </>,
  )

const MarkThink = ({ size }: MarkProps) =>
  line(
    size,
    <>
      <path d="M9.4 2.6c.4 3.7 1.1 5.9 2.44 7.24S15.5 12 19.2 12.4c-3.7.4-5.9 1.1-7.36 2.56S9.8 19.5 9.4 23.2c-.4-3.7-1.1-5.9-2.44-7.24S3.6 12.8-.1 12.4c3.7-.4 5.9-1.1 7.36-2.56S9 6.3 9.4 2.6Z" />
      <path d="M18.4 15.4c.16 1.5.44 2.4.98 2.94s1.44.82 2.94.98c-1.5.16-2.4.44-2.94.98s-.82 1.44-.98 2.94c-.16-1.5-.44-2.4-.98-2.94s-1.44-.82-2.94-.98c1.5-.16 2.4-.44 2.94-.98s.82-1.44.98-2.94Z" />
    </>,
  )

const MarkAgent = ({ size }: MarkProps) =>
  line(
    size,
    <>
      <path d="M12 2.4 21.4 7.4v9.2L12 21.6 2.6 16.6V7.4Z" />
      <path d="M2.6 7.4 12 12.5l9.4-5.1M12 12.5v9.1" />
    </>,
  )

const MarkCosmic = ({ size }: MarkProps) =>
  line(
    size,
    <>
      <circle cx="12" cy="12" r="2.7" fill="currentColor" stroke="none" />
      <path d="M16.4 7.6a6.2 6.2 0 0 1 0 8.8M7.6 16.4a6.2 6.2 0 0 1 0-8.8" />
      <path d="M19.6 4.4a10.7 10.7 0 0 1 0 15.2M4.4 19.6a10.7 10.7 0 0 1 0-15.2" />
    </>,
  )

const MarkCalendar = ({ size }: MarkProps) =>
  line(
    size,
    <>
      <rect x="2.8" y="4.4" width="18.4" height="17" rx="3" />
      <path d="M2.8 9.6h18.4" />
      <path d="M7.9 2.4v4M16.1 2.4v4" />
      <path d="M7.6 13.9h.01M12 13.9h.01M16.4 13.9h.01M7.6 17.6h.01M12 17.6h.01" strokeWidth="2.6" />
    </>,
  )

/** Only for a site whose favicon will not load: an intranet host, or a login wall. */
const MarkGlobe = MarkWeb

const MARKS: Record<GlyphId, (props: MarkProps) => ReactNode> = {
  x: MarkX,
  youtube: MarkYouTube,
  gmail: MarkGmail,
  firecrawl: MarkFirecrawl,
  gdocs: MarkGoogleDocs,
  gsheets: MarkGoogleSheets,
  calendar: MarkCalendar,
  web: MarkWeb,
  search: MarkSearch,
  sandbox: MarkSandbox,
  terminal: MarkTerminal,
  doc: MarkDoc,
  table: MarkTable,
  slides: MarkSlides,
  diagram: MarkDiagram,
  map: MarkMap,
  image: MarkImage,
  memory: MarkMemory,
  think: MarkThink,
  agent: MarkAgent,
  cosmic: MarkCosmic,
}

/** Logos carry their own colours and must not be tinted by the row. */
const SELF_COLOURED = new Set<GlyphId>(['youtube', 'gmail', 'gdocs', 'gsheets', 'firecrawl'])

/**
 * Optical size, not measured size. Some marks carry their own margin -- a
 * document is a narrow portrait shape, a flame is tall and thin -- so drawn to
 * the same box they sit visibly smaller than a mark that runs edge to edge.
 * These nudge them back onto the same optical line as X and YouTube.
 */
const OPTICAL: Partial<Record<GlyphId, number>> = {
  gdocs: 1.16,
  gsheets: 1.16,
  firecrawl: 1.14,
  youtube: 1.06,
  x: 0.94,
  think: 1.06,
}

export const GlyphMark = ({ glyph, size = 16 }: { glyph: GlyphId; size?: number }) => {
  const Mark = MARKS[glyph] || MarkCosmic
  return <>{Mark({ size })}</>
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
        <MarkGlobe size={Math.round(size * 0.86)} />
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
 * The favicon row. Says "these places" in the width of a few characters,
 * instead of spending a line of the flow on hostnames.
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
 * The mark a flow step leads with, in a fixed-width slot so a column of them
 * aligns without a chip drawn behind each one.
 *
 * A logo keeps its own colours in every state. Tinting it with the app accent
 * -- which is what `active` used to do -- turned every brand into the same blue
 * square, so liveness is left to the row behind it instead.
 */
export const AgentGlyph = memo(({
  signal,
  size = 20,
  iconSize,
  active = false,
  title,
}: {
  signal: AgentSignal
  size?: number
  iconSize?: number
  active?: boolean
  title?: string
}) => {
  const selfColoured = SELF_COLOURED.has(signal.glyph)
  return (
    <span
      className={`agent-glyph${active ? ' is-active' : ''}${selfColoured ? ' is-logo' : ''}`}
      style={{
        width: size,
        height: size,
        ...(signal.brand && !selfColoured ? { color: signal.brand } : null),
      }}
      title={title ?? signal.label}
      aria-hidden
    >
      <GlyphMark glyph={signal.glyph} size={iconSize ?? Math.round(size * (OPTICAL[signal.glyph] ?? 1))} />
    </span>
  )
})
AgentGlyph.displayName = 'AgentGlyph'

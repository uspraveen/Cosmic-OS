import { memo, useState, type ReactNode } from 'react'
import { faviconUrl, type AgentSignal, type GlyphId } from './agentSignals'
import cosmicBallLogo from './assets/cosmic-ball-logo-v1.1.png'
import brainMark from './assets/brain-flaticon.png'

/**
 * The marks that lead a flow step, a collapsed section header, and the live
 * footer.
 *
 * Two families, on purpose, because the roster splits cleanly in two.
 *
 * Almost everything Cosmic delegates to is a real product -- Gmail, Google
 * Calendar, Google Docs, Google Sheets, X, Firecrawl -- and a real product gets
 * its real logo, solid, in its own colours. A hand-drawn "page with a folded corner" standing
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
    strokeWidth={1.85}
    strokeLinecap="round"
    strokeLinejoin="round"
    preserveAspectRatio="xMidYMid meet"
    aria-hidden
  >
    {children}
  </svg>
)

/** A real logo, in its real colours. */
const logo = (size: number, children: ReactNode) => (
  <svg
    className="agent-glyph-logo"
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="none"
    preserveAspectRatio="xMidYMid meet"
    aria-hidden
  >
    {children}
  </svg>
)

/**
 * Brand marks painted as an image so inherited CSS fill/stroke/color — from the
 * blue Flow header button, from `currentColor`, from parent opacity — cannot
 * retint them. Inline SVG presentation attributes lose to inherited CSS `fill`.
 */
const brandImage = (size: number, svg: string) => (
  <img
    className="agent-glyph-logo"
    src={`data:image/svg+xml,${encodeURIComponent(svg)}`}
    width={size}
    height={size}
    alt=""
    draggable={false}
  />
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

// Official 2020 Gmail mark (Wikimedia). No white overlay — at 16px that overlay
// antialiases into gray and muddies every brand fill. Painted as an image so
// the Flow header's blue `color` cannot inherit as SVG `fill`.
const MarkGmail = ({ size }: MarkProps) =>
  brandImage(
    size,
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="52 42 88 66"><path fill="#4285F4" d="M58 108h14V74L52 59v43c0 3.32 2.69 6 6 6"/><path fill="#34A853" d="M120 108h14c3.32 0 6-2.69 6-6V59l-20 15"/><path fill="#FBBC04" d="M120 48v26l20-15v-8c0-7.42-8.47-11.65-14.4-7.2"/><path fill="#EA4335" d="M72 74V48l24 18 24-18v26L96 92"/><path fill="#C5221F" d="M52 51v8l20 15V48l-5.6-4.2c-5.94-4.45-14.4-.22-14.4 7.2"/></svg>',
  )

// Official 2020 Google Calendar mark (Wikimedia), the same source as Gmail's.
// Painted as an image for the same reason: the Flow header's blue `color` must
// not inherit into the fills, and the "31" face is what makes the mark read as
// Calendar rather than a generic blue tile.
const MarkCalendar = ({ size }: MarkProps) =>
  brandImage(
    size,
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200"><g transform="translate(3.75 3.75)"><path fill="#FFFFFF" d="M148.882,43.618l-47.368-5.263l-57.895,5.263L38.355,96.25l5.263,52.632l52.632,6.579l52.632-6.579 l5.263-53.947L148.882,43.618z"/><path fill="#1A73E8" d="M65.211,125.276c-3.934-2.658-6.658-6.539-8.145-11.671l9.132-3.763c0.829,3.158,2.276,5.605,4.342,7.342 c2.053,1.737,4.553,2.592,7.474,2.592c2.987,0,5.553-0.908,7.697-2.724s3.224-4.132,3.224-6.934c0-2.868-1.132-5.211-3.395-7.026 s-5.105-2.724-8.5-2.724h-5.276v-9.039H76.5c2.921,0,5.382-0.789,7.382-2.368c2-1.579,3-3.737,3-6.487 c0-2.447-0.895-4.395-2.684-5.855s-4.053-2.197-6.803-2.197c-2.684,0-4.816,0.711-6.395,2.145s-2.724,3.197-3.447,5.276 l-9.039-3.763c1.197-3.395,3.395-6.395,6.618-8.987c3.224-2.592,7.342-3.895,12.342-3.895c3.697,0,7.026,0.711,9.974,2.145 c2.947,1.434,5.263,3.421,6.934,5.947c1.671,2.539,2.5,5.382,2.5,8.539c0,3.224-0.776,5.947-2.329,8.184 c-1.553,2.237-3.461,3.947-5.724,5.145v0.539c2.987,1.25,5.421,3.158,7.342,5.724c1.908,2.566,2.868,5.632,2.868,9.211 s-0.908,6.776-2.724,9.579c-1.816,2.803-4.329,5.013-7.513,6.618c-3.197,1.605-6.789,2.421-10.776,2.421 C73.408,129.263,69.145,127.934,65.211,125.276z"/><path fill="#1A73E8" d="M121.25,79.961l-9.974,7.25l-5.013-7.605l17.987-12.974h6.895v61.197h-9.895L121.25,79.961z"/><path fill="#EA4335" d="M148.882,196.25l47.368-47.368l-23.684-10.526l-23.684,10.526l-10.526,23.684L148.882,196.25z"/><path fill="#34A853" d="M33.092,172.566l10.526,23.684h105.263v-47.368H43.618L33.092,172.566z"/><path fill="#4285F4" d="M12.039-3.75C3.316-3.75-3.75,3.316-3.75,12.039v136.842l23.684,10.526l23.684-10.526V43.618h105.263 l10.526-23.684L148.882-3.75H12.039z"/><path fill="#188038" d="M-3.75,148.882v31.579c0,8.724,7.066,15.789,15.789,15.789h31.579v-47.368H-3.75z"/><path fill="#FBBC04" d="M148.882,43.618v105.263h47.368V43.618l-23.684-10.526L148.882,43.618z"/><path fill="#1967D2" d="M196.25,43.618V12.039c0-8.724-7.066-15.789-15.789-15.789h-31.579v47.368H196.25z"/></g></svg>',
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

/** Stored facts and semantic memory — a small knowledge graph, not a database. */
const MarkMemory = ({ size }: MarkProps) =>
  line(
    size,
    <>
      <circle cx="12" cy="11.6" r="2.1" />
      <circle cx="7.1" cy="16.8" r="1.55" />
      <circle cx="16.9" cy="16.8" r="1.55" />
      <circle cx="12" cy="6.1" r="1.55" />
      <path d="M12 9.4v1" />
      <path d="M10.4 13.1 8.3 15.2" />
      <path d="M13.6 13.1 15.7 15.2" />
    </>,
  )

/** The orchestrator escalating its own reasoning budget. Painted as an image
 *  (like the Cosmic ball) so inherited CSS colour cannot retint the artwork. */
const MarkBrain = ({ size }: MarkProps) => (
  <img
    className="agent-glyph-logo"
    src={brainMark}
    width={size}
    height={size}
    alt=""
    draggable={false}
  />
)

/** Session history: open ring with a solid arrow — thick enough to read at 16px. */
const MarkRecall = ({ size }: MarkProps) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={2.5}
    strokeLinecap="round"
    strokeLinejoin="round"
    preserveAspectRatio="xMidYMid meet"
    aria-hidden
  >
    <path d="M17.55 7.4A7.2 7.2 0 1 1 6.45 7.4" />
    <path fill="currentColor" stroke="none" d="M8.15 7.15 12 2.85l3.85 4.3Z" />
  </svg>
)

const MarkCosmicBall = ({ size }: MarkProps) => (
  <img
    className="agent-glyph-cosmic-ball"
    src={cosmicBallLogo}
    width={size}
    height={size}
    alt=""
    draggable={false}
  />
)

const MarkSchedule = ({ size }: MarkProps) =>
  line(
    size,
    <>
      <circle cx="12" cy="12" r="8.4" />
      <path d="M12 7.4v5l3.2 2.2" />
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
  recall: MarkRecall,
  schedule: MarkSchedule,
  think: MarkCosmicBall,
  brain: MarkBrain,
  agent: MarkCosmicBall,
  cosmic: MarkCosmicBall,
}

/** Logos carry their own colours and must not be tinted by the row. */
const SELF_COLOURED = new Set<GlyphId>(['youtube', 'gmail', 'calendar', 'gdocs', 'gsheets', 'firecrawl', 'think', 'brain', 'agent', 'cosmic'])

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
  recall: 1.12,
  x: 0.94,
  think: 1,
  agent: 1,
  cosmic: 1,
}

export const GlyphMark = ({ glyph, size = 16 }: { glyph: GlyphId; size?: number }) => {
  const Mark = MARKS[glyph] || MarkCosmicBall
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

import { memo, useState, type ReactElement, type ReactNode } from 'react'
import { faviconUrl, type AgentSignal, type GlyphId } from './agentSignals'

/**
 * The marks that lead a flow step, a collapsed section header, and the live
 * footer.
 *
 * Every mark in here is a solid shape, hand-drawn on the same 24x24 grid. That
 * is the whole reason this file exists instead of a line-icon import: at the
 * 12-13px these render at, a 1.9px-stroke wireframe collapses into grey mush --
 * a stroked globe in particular reads as a smudge -- while a filled silhouette
 * stays crisp and reads as a considered mark rather than clip art. It also puts
 * the functional glyphs at the same optical weight as the three real logos
 * (X, YouTube, Gmail), so a column mixing them looks like one set.
 *
 * Colour discipline: a mark that really is a colour (a logo, a Google product)
 * draws in that colour. Everything else is monochrome. The tile behind them is
 * always neutral -- it is an alignment anchor, never a status light.
 */

type MarkProps = { size: number }

const svg = (size: number, children: ReactNode, evenOdd = true) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="currentColor"
    fillRule={evenOdd ? 'evenodd' : 'nonzero'}
    clipRule={evenOdd ? 'evenodd' : 'nonzero'}
    aria-hidden
  >
    {children}
  </svg>
)

/* -- Real logos ---------------------------------------------------------- */

const MarkX = ({ size }: MarkProps) =>
  svg(size, <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231Zm-1.161 17.52h1.833L7.084 4.126H5.117Z" />)

const MarkYouTube = ({ size }: MarkProps) =>
  // evenodd carves the play triangle out of the body, so the mark reads on any
  // tile colour instead of needing a matching fill behind it.
  svg(size, <path d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814ZM9.545 15.568V8.432L15.818 12l-6.273 3.568Z" />)

const MarkGmail = ({ size }: MarkProps) =>
  svg(size, <path d="M24 5.457v13.909c0 .904-.732 1.636-1.636 1.636h-3.819V11.73L12 16.64l-6.545-4.91v9.273H1.636A1.636 1.636 0 0 1 0 19.366V5.457c0-2.023 2.309-3.178 3.927-1.964L5.455 4.64 12 9.548l6.545-4.91 1.528-1.145C21.69 2.28 24 3.434 24 5.457Z" />)

/* -- Functional marks ---------------------------------------------------- */

/** Reading a page. A browser window says "a website" at 12px; a globe does not. */
const MarkWeb = ({ size }: MarkProps) =>
  svg(size, <path d="M4.6 2.8h14.8a3.3 3.3 0 0 1 3.3 3.3v11.8a3.3 3.3 0 0 1-3.3 3.3H4.6a3.3 3.3 0 0 1-3.3-3.3V6.1a3.3 3.3 0 0 1 3.3-3.3Zm.4 2.5a1.15 1.15 0 1 0 0 2.3 1.15 1.15 0 0 0 0-2.3Zm3.5 0a1.15 1.15 0 1 0 0 2.3 1.15 1.15 0 0 0 0-2.3Zm3.5 0a1.15 1.15 0 1 0 0 2.3 1.15 1.15 0 0 0 0-2.3ZM1.3 9.1h21.4v1.7H1.3Zm3.7 4h14v1.8H5Zm0 3.6h9v1.8H5Z" />)

/** Searching. A solid lens with a carved ring, not a hairline circle. */
const MarkSearch = ({ size }: MarkProps) =>
  svg(size, <path d="M10.6 1.6a9 9 0 1 0 5.28 16.29l4.61 4.61a1.6 1.6 0 0 0 2.26-2.26l-4.61-4.61A9 9 0 0 0 10.6 1.6Zm0 3.2a5.8 5.8 0 1 1 0 11.6 5.8 5.8 0 0 1 0-11.6Z" />)

/** The orchestrator's own sandbox: the boxed prompt, as a terminal draws it. */
const MarkSandbox = ({ size }: MarkProps) =>
  svg(size, <path d="M4.6 2.6h14.8a3.4 3.4 0 0 1 3.4 3.4v12a3.4 3.4 0 0 1-3.4 3.4H4.6a3.4 3.4 0 0 1-3.4-3.4V6a3.4 3.4 0 0 1 3.4-3.4ZM7.4 8.2 9 6.6l5 5-5 5-1.6-1.6 3.4-3.4Zm5 7h5v2h-5Z" />)

/** A machine we drive rather than own: the same prompt, unboxed. */
const MarkTerminal = ({ size }: MarkProps) =>
  svg(
    size,
    <>
      <path d="M4.4 5.6 6.4 3.6l8 8-8 8-2-2 6-6Z" />
      <path d="M12.6 17.4h8v2.6h-8Z" />
    </>,
    false,
  )

const MarkDoc = ({ size }: MarkProps) =>
  svg(
    size,
    <>
      <path d="M6.6 1.9h6.5v5.1a1.9 1.9 0 0 0 1.9 1.9h5.1v11.2a2.9 2.9 0 0 1-2.9 2.9H6.6a2.9 2.9 0 0 1-2.9-2.9V4.8a2.9 2.9 0 0 1 2.9-2.9Zm.6 10.5h9.6v1.8H7.2Zm0 3.6h6.4v1.8H7.2Z" />
      <path d="M14.6 2.3 19.7 7.4h-3.7a1.4 1.4 0 0 1-1.4-1.4Z" opacity={0.5} />
    </>,
  )

const MarkTable = ({ size }: MarkProps) =>
  // A solid header band over a 2x2 body. Three carved gutters each way turned
  // this into a checkerboard at 13px, which reads as anything but a sheet.
  svg(size, <path d="M4.6 2.9h14.8a3.3 3.3 0 0 1 3.3 3.3v11.6a3.3 3.3 0 0 1-3.3 3.3H4.6a3.3 3.3 0 0 1-3.3-3.3V6.2a3.3 3.3 0 0 1 3.3-3.3ZM1.3 9.6h21.4v1.6H1.3Zm0 5.6h21.4v1.6H1.3Zm9.9-4.8h1.6v4.8h-1.6Zm0 6.4h1.6v4.5h-1.6Z" />)

const MarkSlides = ({ size }: MarkProps) =>
  svg(size, <path d="M3.4 3.2h17.2a2.9 2.9 0 0 1 2.9 2.9v8.4a2.9 2.9 0 0 1-2.9 2.9H3.4a2.9 2.9 0 0 1-2.9-2.9V6.1a2.9 2.9 0 0 1 2.9-2.9ZM6 7.2h12v2H6Zm0 3.8h7.5v1.9H6ZM11.1 18.3h1.8v2.2h-1.8ZM7.6 20.2h8.8v2.1H7.6Z" />)

const MarkDiagram = ({ size }: MarkProps) =>
  svg(size, <path d="M9.1 1.9h5.8a2 2 0 0 1 2 2v3.2a2 2 0 0 1-2 2H9.1a2 2 0 0 1-2-2V3.9a2 2 0 0 1 2-2Zm2.05 7.2h1.7v2.4h-1.7ZM4.7 11.5h14.6v1.7H4.7Zm0 1.1h1.7v2.9H4.7Zm12.9 0h1.7v2.9h-1.7ZM2.6 15.1h5.9a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H2.6a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2Zm12.9 0h5.9a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2h-5.9a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2Z" />, false)

const MarkMap = ({ size }: MarkProps) =>
  svg(size, <path d="M12 1.4a8.2 8.2 0 0 0-8.2 8.2c0 5.9 6.9 12.4 7.6 13.05a.9.9 0 0 0 1.2 0c.7-.65 7.6-7.15 7.6-13.05A8.2 8.2 0 0 0 12 1.4Zm0 5.1a3.2 3.2 0 1 1 0 6.4 3.2 3.2 0 0 1 0-6.4Z" />)

const MarkImage = ({ size }: MarkProps) =>
  svg(size, <path d="M4.6 3.2h14.8a3.3 3.3 0 0 1 3.3 3.3v11a3.3 3.3 0 0 1-3.3 3.3H4.6a3.3 3.3 0 0 1-3.3-3.3v-11a3.3 3.3 0 0 1 3.3-3.3ZM8.3 6.6a2.1 2.1 0 1 0 0 4.2 2.1 2.1 0 0 0 0-4.2Zm-4.7 11L9.7 10.7l3.5 4 2.7-2.6 4.6 5.5Z" />)

const MarkMemory = ({ size }: MarkProps) =>
  svg(size, <path d="M12 1.6c4.9 0 8.8 1.6 8.8 3.6S16.9 8.8 12 8.8 3.2 7.2 3.2 5.2 7.1 1.6 12 1.6ZM3.2 8c1.9 1.5 5.2 2.3 8.8 2.3s6.9-.8 8.8-2.3v4.3c0 2-3.9 3.6-8.8 3.6S3.2 14.3 3.2 12.3Zm0 7.1c1.9 1.5 5.2 2.3 8.8 2.3s6.9-.8 8.8-2.3v3.6c0 2-3.9 3.6-8.8 3.6s-8.8-1.6-8.8-3.6Z" />, false)

const MarkThink = ({ size }: MarkProps) =>
  svg(size, <path d="M10 5c.37 3.33 1 5.33 2.2 6.53S15.67 12.63 19 13c-3.33.37-5.33 1-6.53 2.2S10.37 17.67 10 21c-.37-3.33-1-5.33-2.2-6.53S4.33 13.37 1 13c3.33-.37 5.33-1 6.53-2.2S9.63 8.33 10 5Zm8.5-3.6c.17 1.55.47 2.48 1.03 3.04S21.02 5.33 22.57 5.5c-1.55.17-2.48.47-3.04 1.03S18.67 8.02 18.5 9.57c-.17-1.55-.47-2.48-1.03-3.04S15.98 5.67 14.43 5.5c1.55-.17 2.48-.47 3.04-1.03S18.33 2.95 18.5 1.4Z" />, false)

const MarkAgent = ({ size }: MarkProps) =>
  svg(size, <path d="M12 1.5 22.2 7.1v9.8L12 22.5 1.8 16.9V7.1Zm0 9.55L3.05 6.13l-.82 1.5 8.92 4.87V22h1.7v-9.5l8.92-4.87-.82-1.5Z" />)

const MarkCosmic = ({ size }: MarkProps) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden>
    <circle cx="12" cy="12" r="3.9" />
    <ellipse
      cx="12"
      cy="12"
      rx="10.2"
      ry="4.7"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      transform="rotate(-28 12 12)"
    />
  </svg>
)

const MarkCalendar = ({ size }: MarkProps) =>
  svg(
    size,
    <>
      <path d="M7 1.2h2.1v4.4H7Zm7.9 0H17v4.4h-2.1Z" />
      <path d="M4.3 3.6h15.4a3.3 3.3 0 0 1 3.3 3.3v13a3.3 3.3 0 0 1-3.3 3.3H4.3A3.3 3.3 0 0 1 1 19.9v-13a3.3 3.3 0 0 1 3.3-3.3ZM1 8.4h22v1.9H1Zm5.2 4.2h2.4V15H6.2Zm4.6 0h2.4V15h-2.4Zm4.6 0h2.4V15h-2.4ZM6.2 16.6h2.4V19H6.2Zm4.6 0h2.4V19h-2.4Z" />
    </>,
  )

/** Only for a site whose favicon will not load: an intranet host, or a login wall. */
const MarkGlobe = ({ size }: MarkProps) =>
  svg(size, <path d="M12 1.8a10.2 10.2 0 1 0 0 20.4 10.2 10.2 0 0 0 0-20.4ZM2.6 11.1h18.8v1.8H2.6ZM12 1.8c2.35 0 4.25 4.57 4.25 10.2S14.35 22.2 12 22.2 7.75 17.63 7.75 12 9.65 1.8 12 1.8Zm0 1.8c-1.17 0-2.45 3.76-2.45 8.4s1.28 8.4 2.45 8.4 2.45-3.76 2.45-8.4S13.17 3.6 12 3.6Z" />)

const MARKS: Record<GlyphId, (props: MarkProps) => ReactElement> = {
  x: MarkX,
  youtube: MarkYouTube,
  gmail: MarkGmail,
  calendar: MarkCalendar,
  gdocs: MarkDoc,
  gsheets: MarkTable,
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

export const GlyphMark = ({ glyph, size = 13 }: { glyph: GlyphId; size?: number }) => {
  const Mark = MARKS[glyph] || MarkCosmic
  return <Mark size={size} />
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
        <MarkGlobe size={Math.round(size * 0.66)} />
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
 * The tile a flow step leads with.
 *
 * A brand keeps its own colour in every state, `active` included. Tinting the
 * X mark with the app accent turned a black-and-white logo into a blue box that
 * no longer read as X at all; liveness is the tile's job, not the mark's, so
 * `active` brightens the tile and adds one expanding ring instead.
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
    style={{ width: size, height: size, ...(signal.brand ? { color: signal.brand } : null) }}
    title={title ?? signal.label}
    aria-hidden
  >
    <GlyphMark glyph={signal.glyph} size={iconSize ?? Math.round(size * 0.6)} />
  </span>
))
AgentGlyph.displayName = 'AgentGlyph'

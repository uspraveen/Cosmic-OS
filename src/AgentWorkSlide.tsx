import { useEffect, useRef, useState } from 'react'

export type AgentWorkPayload = {
  agentId: string
  label?: string
  detail?: string
  query?: string
}

interface AgentWorkSlideProps {
  payload: AgentWorkPayload
}

export default function AgentWorkSlide({ payload }: AgentWorkSlideProps) {
  switch (payload.agentId) {
    case 'web-search':
    case 'firecrawl':
    case 'perplexity':
      return <WebSearchSlide payload={payload} />
    default:
      return <WebSearchSlide payload={payload} />
  }
}

function WebSearchSlide({ payload }: AgentWorkSlideProps) {
  const label = payload.label || 'Searching the web'
  const detail = payload.detail || payload.query || ''

  return (
    <div className="slide slide-agent-work">
      <div className="aw-stack">
        <Globe />
        <div className="aw-text">
          <ShimmerLabel>{label}</ShimmerLabel>
          {detail && <div className="aw-detail">{detail}</div>}
        </div>
      </div>
    </div>
  )
}

/**
 * Centered, slowly-rotating globe.
 *
 * Sphere illusion: meridians are SVG ellipses whose horizontal radius is animated
 * via |cos(phase + offset)| — collectively this produces the look of longitude
 * lines wrapping around a 3D sphere as the phase advances.
 */
function Globe() {
  const [phase, setPhase] = useState(0)
  const rafRef = useRef<number | null>(null)
  const lastRef = useRef<number>(0)

  useEffect(() => {
    const tick = (now: number) => {
      if (!lastRef.current) lastRef.current = now
      const dt = Math.max(0, (now - lastRef.current) / 1000)
      lastRef.current = now
      // ~1 full rotation every ~9s — calm, premium pacing
      setPhase((p) => (p + dt * (Math.PI * 2) / 9) % (Math.PI * 2))
      rafRef.current = requestAnimationFrame(tick)
    }
    rafRef.current = requestAnimationFrame(tick)
    return () => {
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current)
      rafRef.current = null
      lastRef.current = 0
    }
  }, [])

  const SIZE = 64
  const CX = SIZE / 2
  const CY = SIZE / 2
  const R = 24

  const meridianOffsets = [0, 22.5, 45, 67.5, 90, 112.5, 135, 157.5]

  return (
    <div className="aw-globe-wrap">
      <div className="aw-globe-glow" aria-hidden />
      <svg
        className="aw-globe"
        width={SIZE}
        height={SIZE}
        viewBox={`0 0 ${SIZE} ${SIZE}`}
        role="img"
        aria-label="Searching the web"
      >
        <defs>
          <radialGradient id="awSphereFill" cx="34%" cy="28%" r="78%">
            <stop offset="0%" stopColor="rgba(255,255,255,0.22)" />
            <stop offset="42%" stopColor="rgba(180,210,255,0.06)" />
            <stop offset="100%" stopColor="rgba(120,160,220,0.00)" />
          </radialGradient>
          <radialGradient id="awSphereRim" cx="50%" cy="50%" r="50%">
            <stop offset="86%" stopColor="rgba(255,255,255,0.0)" />
            <stop offset="100%" stopColor="rgba(255,255,255,0.18)" />
          </radialGradient>
          <linearGradient id="awHighlight" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor="rgba(255,255,255,0.55)" />
            <stop offset="100%" stopColor="rgba(255,255,255,0.0)" />
          </linearGradient>
          <clipPath id="awSphereClip">
            <circle cx={CX} cy={CY} r={R} />
          </clipPath>
        </defs>

        {/* sphere body */}
        <circle cx={CX} cy={CY} r={R} fill="url(#awSphereFill)" />
        <circle cx={CX} cy={CY} r={R} fill="url(#awSphereRim)" />

        {/* parallels (latitudes) — static */}
        <g
          clipPath="url(#awSphereClip)"
          fill="none"
          stroke="rgba(220,235,255,0.18)"
          strokeWidth="0.55"
        >
          <ellipse cx={CX} cy={CY} rx={R} ry={R * 0.32} />
          <ellipse cx={CX} cy={CY} rx={R} ry={R * 0.62} />
          <ellipse cx={CX} cy={CY} rx={R} ry={R * 0.86} />
        </g>

        {/* equator — slightly stronger */}
        <line
          x1={CX - R}
          y1={CY}
          x2={CX + R}
          y2={CY}
          stroke="rgba(220,235,255,0.28)"
          strokeWidth="0.6"
          clipPath="url(#awSphereClip)"
        />

        {/* meridians (longitudes) — animated rx for rotation feel */}
        <g
          clipPath="url(#awSphereClip)"
          fill="none"
          stroke="rgba(225,240,255,0.42)"
          strokeWidth="0.6"
        >
          {meridianOffsets.map((deg, i) => {
            const angle = phase + (deg * Math.PI) / 180
            const rxRaw = Math.cos(angle)
            const rx = Math.max(0.001, Math.abs(rxRaw) * R)
            const opacity = 0.35 + 0.55 * Math.abs(rxRaw)
            return (
              <ellipse
                key={i}
                cx={CX}
                cy={CY}
                rx={rx}
                ry={R}
                style={{ opacity }}
              />
            )
          })}
        </g>

        {/* outer crisp rim */}
        <circle
          cx={CX}
          cy={CY}
          r={R}
          fill="none"
          stroke="rgba(255,255,255,0.32)"
          strokeWidth="0.7"
        />

        {/* specular highlight */}
        <ellipse
          cx={CX - R * 0.32}
          cy={CY - R * 0.42}
          rx={R * 0.30}
          ry={R * 0.16}
          fill="url(#awHighlight)"
          transform={`rotate(-28 ${CX - R * 0.32} ${CY - R * 0.42})`}
        />
      </svg>
    </div>
  )
}

function ShimmerLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="aw-shimmer" aria-live="polite">
      <span className="aw-shimmer-track">{children}</span>
    </div>
  )
}

import { useEffect, useRef } from 'react'

/* ─────────────────────────────────────────────────────────
   DotBurstCheckmark
   ─────────────────────────────────────────────────────────
   Canvas particle animation:
   1. 84 dots from the dot-matrix progress bar burst outward
      as soft, light dots
   2. They arc and converge toward the mark icon
   3. Particles snap into a checkmark shape with a subtle
      green glow bloom
   4. The formed checkmark shimmers gently

   Inspired by the snow particle system in WeatherAnimation.
   ───────────────────────────────────────────────────────── */

const COLS = 28
const ROWS = 3
const TOTAL = COLS * ROWS

// ── Sample checkmark stroke as discrete target positions ──
function checkmarkTargets(count: number, size: number): { x: number; y: number }[] {
  const cx = size / 2
  const cy = size / 2
  const s = size * 0.35

  const a = { x: cx - s * 0.74, y: cy + s * 0.08 }
  const b = { x: cx - s * 0.08, y: cy + s * 0.68 }
  const c = { x: cx + s * 0.86, y: cy - s * 0.62 }

  const l1 = Math.hypot(b.x - a.x, b.y - a.y)
  const l2 = Math.hypot(c.x - b.x, c.y - b.y)
  const total = l1 + l2
  const n1 = Math.max(1, Math.round((l1 / total) * count))
  const n2 = count - n1

  const pts: { x: number; y: number }[] = []
  for (let i = 0; i < n1; i++) {
    const t = n1 > 1 ? i / (n1 - 1) : 0
    pts.push({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t })
  }
  for (let i = 0; i < n2; i++) {
    const t = n2 > 1 ? i / (n2 - 1) : 0
    pts.push({ x: b.x + (c.x - b.x) * t, y: b.y + (c.y - b.y) * t })
  }
  return pts
}

interface P {
  ox: number; oy: number        // origin (grid position)
  x: number;  y: number          // current
  vx: number; vy: number         // burst velocity
  tx: number; ty: number          // target on checkmark
  sz: number                      // base size
  op: number                      // opacity
  delay: number                   // stagger frames
  settled: boolean
  ph: number                      // shimmer phase
}

export default function DotBurstCheckmark() {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const animRef = useRef(0)
  const doneRef = useRef(false)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const shell = canvas.parentElement
    if (!shell) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    // ── Measure layout ──
    const sr = shell.getBoundingClientRect()
    const W = sr.width
    const H = sr.height
    const dpr = window.devicePixelRatio || 1
    canvas.width = W * dpr
    canvas.height = H * dpr
    ctx.scale(dpr, dpr)

    // Mark icon center
    const markEl = shell.querySelector('.it-mark') as HTMLElement | null
    let mx = 21, my = 33, ms = 42
    if (markEl) {
      const r = markEl.getBoundingClientRect()
      mx = r.left - sr.left + r.width / 2
      my = r.top - sr.top + r.height / 2
      ms = Math.max(r.width, r.height)
    }

    // Dot grid position
    const gridEl = shell.querySelector('.it-dot-progress') as HTMLElement | null
    let gy = H - 22, gw = W
    if (gridEl) {
      const r = gridEl.getBoundingClientRect()
      gy = r.top - sr.top
      gw = r.width
    }

    // ── Checkmark stroke endpoints (canvas coords) ──
    const s = ms * 0.35
    const strokeA = { x: mx - s * 0.74, y: my + s * 0.08 }
    const strokeB = { x: mx - s * 0.08, y: my + s * 0.68 }
    const strokeC = { x: mx + s * 0.86, y: my - s * 0.62 }

    // ── Build particles ──
    const targets = checkmarkTargets(TOTAL, ms)
    const spX = gw / COLS
    const spY = 9
    const gcx = gw / 2
    const gcy = gy + (ROWS * spY) / 2
    const particles: P[] = []

    for (let row = 0; row < ROWS; row++) {
      for (let col = 0; col < COLS; col++) {
        const i = row * COLS + col
        const ox = col * spX + spX / 2
        const oy = gy + row * spY + spY / 2

        // Burst direction: outward from grid center with upward bias
        const dx = ox - gcx
        const dy = oy - gcy
        const force = 4 + Math.random() * 5
        const ang = Math.atan2(dy, dx) + (Math.random() - 0.5) * 1.6
        const vx = Math.cos(ang) * force
        const vy = Math.sin(ang) * force - 3 - Math.random() * 3

        // Target in mark-relative coords → shell coords
        const tx = mx - ms / 2 + targets[i].x
        const ty = my - ms / 2 + targets[i].y

        particles.push({
          ox, oy, x: ox, y: oy, vx, vy, tx, ty,
          sz: 1.3 + Math.random() * 0.9,
          op: 0.5,
          delay: col * 0.4 + Math.random() * 1.5,
          settled: false,
          ph: Math.random() * Math.PI * 2,
        })
      }
    }

    // ── Timing ──
    const BURST = 24         // burst expansion frames
    const CONV_ST = 10        // convergence starts (overlaps burst end)
    const CONV_DUR = 44       // convergence duration
    const MAX_FRAME = 350

    let frame = 0
    doneRef.current = false
    let settledFrame = 0           // frame when all particles first settled
    const MERGE_DUR = 18           // frames to transition dots → clean stroke

    const drawCheckStroke = (alpha: number) => {
      ctx.save()
      ctx.lineCap = 'round'
      ctx.lineJoin = 'round'
      ctx.lineWidth = 2.4

      // Subtle glow behind stroke
      ctx.shadowColor = `rgba(52,199,89,${alpha * 0.5})`
      ctx.shadowBlur = 6

      ctx.strokeStyle = `rgba(52,199,89,${alpha})`
      ctx.beginPath()
      ctx.moveTo(strokeA.x, strokeA.y)
      ctx.lineTo(strokeB.x, strokeB.y)
      ctx.lineTo(strokeC.x, strokeC.y)
      ctx.stroke()

      ctx.restore()
    }

    const tick = () => {
      frame++
      ctx.clearRect(0, 0, W, H)
      let allDone = true

      // ── Merge progress (0 = dots only, 1 = stroke only) ──
      const mergeT = doneRef.current ? Math.min(1, (frame - settledFrame) / MERGE_DUR) : 0
      const dotAlpha = 1 - mergeT      // dots fade out
      const strokeAlpha = mergeT        // stroke fades in

      for (const p of particles) {
        const ef = Math.max(0, frame - p.delay)

        // ── Waiting: pulse at origin ──
        if (ef <= 0) {
          const pul = 0.2 + 0.5 * Math.abs(Math.sin(frame * 0.09 + p.ph))
          ctx.beginPath()
          ctx.arc(p.ox, p.oy, 1.5, 0, Math.PI * 2)
          ctx.fillStyle = `rgba(255,255,255,${pul})`
          ctx.fill()
          allDone = false
          continue
        }

        // ── Phase 1: Burst outward ──
        if (ef <= BURST && !p.settled) {
          const t = ef / BURST
          const ease = 1 - (1 - t) ** 3
          p.x = p.ox + p.vx * ef * (1 - ease * 0.6)
          p.y = p.oy + p.vy * ef * (1 - ease * 0.6)
          allDone = false
        }

        // ── Phase 2: Converge to checkmark ──
        const cf = ef - CONV_ST
        if (cf > 0 && !p.settled) {
          const ct = Math.min(1, cf / CONV_DUR)
          let ec: number
          if (ct < 0.5) {
            ec = 4 * ct * ct * ct
          } else {
            const f = -2 * ct + 2
            ec = 1 - (f * f * f) / 2
          }
          const overshoot = ct > 0.85 ? Math.sin((ct - 0.85) * 20) * 0.02 * (1 - ct) * 6.6 : 0

          const bx = p.ox + p.vx * Math.min(ef, BURST) * 0.4
          const by = p.oy + p.vy * Math.min(ef, BURST) * 0.4
          p.x = bx + (p.tx - bx) * (ec + overshoot)
          p.y = by + (p.ty - by) * (ec + overshoot)

          p.op = 0.35 + 0.4 * ec

          if (ct >= 1) {
            p.settled = true
            p.x = p.tx
            p.y = p.ty
          } else {
            allDone = false
          }
        }

        // ── Skip dot rendering once fully merged to stroke ──
        if (mergeT >= 1) continue

        const convP = cf > 0 ? Math.min(1, cf / CONV_DUR) : 0

        // ── Draw particle (simple soft dot) ──
        const gm = p.settled ? 0.82 : convP * 0.65
        const cr = Math.round(255 - gm * 45)
        const cg = 255
        const cb = Math.round(255 - gm * 30)
        const sz = p.settled ? p.sz * 1.1 : p.sz
        const opMul = dotAlpha * p.op

        // Soft glow
        ctx.beginPath()
        const g1 = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, sz * 2)
        g1.addColorStop(0, `rgba(${cr},${cg},${cb},${opMul * 0.55})`)
        g1.addColorStop(0.5, `rgba(${cr},${cg},${cb},${opMul * 0.15})`)
        g1.addColorStop(1, `rgba(${cr},${cg},${cb},0)`)
        ctx.fillStyle = g1
        ctx.arc(p.x, p.y, sz * 2, 0, Math.PI * 2)
        ctx.fill()

        // Dot core
        ctx.beginPath()
        ctx.arc(p.x, p.y, sz * 0.55, 0, Math.PI * 2)
        ctx.fillStyle = `rgba(255,255,255,${opMul * 0.6})`
        ctx.fill()
      }

      // ── Global convergence bloom ──
      const gc = Math.min(1, Math.max(0, (frame - 25) / 35))
      if (gc > 0 && gc < 1) {
        const ga = Math.sin(gc * Math.PI) * 0.09
        ctx.beginPath()
        const gg = ctx.createRadialGradient(mx, my, 0, mx, my, ms * 1.4)
        gg.addColorStop(0, `rgba(52,199,89,${ga})`)
        gg.addColorStop(1, 'rgba(52,199,89,0)')
        ctx.fillStyle = gg
        ctx.arc(mx, my, ms * 1.4, 0, Math.PI * 2)
        ctx.fill()
      }

      // ── All settled: start merge to clean stroke ──
      if (allDone && !doneRef.current) {
        doneRef.current = true
        settledFrame = frame
      }

      // ── Draw clean checkmark stroke (fades in as dots fade out) ──
      if (strokeAlpha > 0) {
        drawCheckStroke(strokeAlpha)
      }

      if (frame < MAX_FRAME) {
        animRef.current = requestAnimationFrame(tick)
      }
    }

    animRef.current = requestAnimationFrame(tick)
    return () => { if (animRef.current) cancelAnimationFrame(animRef.current) }
  }, [])

  return (
    <canvas
      ref={canvasRef}
      style={{
        position: 'absolute',
        inset: 0,
        width: '100%',
        height: '100%',
        pointerEvents: 'none',
        zIndex: 10,
      }}
    />
  )
}

import './StreamDotMatrix.css'

const COLS = 4
const ROWS = 4

export default function StreamDotMatrix() {
  return (
    <div className="composer-dot-matrix" aria-hidden>
      {Array.from({ length: COLS * ROWS }, (_, i) => {
        const col = i % COLS
        const row = Math.floor(i / COLS)
        // Diagonal wave so the “bright band” sweeps across the grid (several dots hot at once).
        const waveIndex = col + row
        return (
          <span
            key={i}
            className="composer-dot-matrix__cell"
            style={{ animationDelay: `${waveIndex * 0.055}s` }}
          />
        )
      })}
    </div>
  )
}

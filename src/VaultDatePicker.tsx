import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { createPortal } from 'react-dom'

const WEEKDAYS = ['S', 'M', 'T', 'W', 'T', 'F', 'S']

function startOfMonth(year: number, month: number) {
  return new Date(year, month, 1)
}

function toDayKey(date: Date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}

function parseDayKey(value?: string | null): Date | null {
  const day = String(value || '').trim().slice(0, 10)
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return null
  const parsed = new Date(`${day}T00:00:00`)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

interface VaultDatePickerProps {
  value: string
  onChange: (next: string) => void
}

const POPOVER_WIDTH = 256
const POPOVER_GAP = 6
const VIEWPORT_MARGIN = 12

export default function VaultDatePicker({ value, onChange }: VaultDatePickerProps) {
  const selected = parseDayKey(value)
  const todayKey = toDayKey(new Date())
  const [open, setOpen] = useState(false)
  const [cursor, setCursor] = useState(() => selected || new Date())
  const [popoverStyle, setPopoverStyle] = useState<CSSProperties>({})
  const rootRef = useRef<HTMLDivElement | null>(null)
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const popoverRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (selected) setCursor(selected)
  }, [value])

  useLayoutEffect(() => {
    if (!open) return

    const place = () => {
      const trigger = triggerRef.current
      if (!trigger) return
      const rect = trigger.getBoundingClientRect()
      const height = popoverRef.current?.offsetHeight || 280
      let left = rect.right - POPOVER_WIDTH
      left = Math.min(
        Math.max(left, VIEWPORT_MARGIN),
        Math.max(VIEWPORT_MARGIN, window.innerWidth - POPOVER_WIDTH - VIEWPORT_MARGIN),
      )
      let top = rect.bottom + POPOVER_GAP
      if (top + height > window.innerHeight - VIEWPORT_MARGIN && rect.top - POPOVER_GAP - height >= VIEWPORT_MARGIN) {
        top = rect.top - POPOVER_GAP - height
      }
      setPopoverStyle({ top, left, width: POPOVER_WIDTH })
    }

    place()
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    return () => {
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
    }
  }, [open, cursor])

  useEffect(() => {
    if (!open) return
    const onPointer = (event: MouseEvent) => {
      const target = event.target as Node
      if (rootRef.current?.contains(target) || popoverRef.current?.contains(target)) return
      setOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.stopPropagation()
      setOpen(false)
    }
    window.addEventListener('mousedown', onPointer)
    window.addEventListener('keydown', onKey, true)
    return () => {
      window.removeEventListener('mousedown', onPointer)
      window.removeEventListener('keydown', onKey, true)
    }
  }, [open])

  const days = useMemo(() => {
    const first = startOfMonth(cursor.getFullYear(), cursor.getMonth())
    const last = new Date(cursor.getFullYear(), cursor.getMonth() + 1, 0)
    const cells: Array<Date | null> = []
    for (let i = 0; i < first.getDay(); i += 1) cells.push(null)
    for (let day = 1; day <= last.getDate(); day += 1) {
      cells.push(new Date(cursor.getFullYear(), cursor.getMonth(), day))
    }
    return cells
  }, [cursor])

  const monthLabel = cursor.toLocaleDateString([], { month: 'long', year: 'numeric' })
  const display = selected
    ? selected.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' })
    : 'No expiry'

  return (
    <div className="vault-date-picker" ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className={`vault-select-trigger ${open ? 'open' : ''} ${selected ? '' : 'is-empty'}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span>{display}</span>
        <svg viewBox="0 0 24 24" fill="none" aria-hidden>
          <rect x="3.5" y="5" width="17" height="15.5" rx="2.5" stroke="currentColor" strokeWidth="1.4" />
          <path d="M8 3.5v3M16 3.5v3M3.5 10h17" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
        </svg>
      </button>
      {open && typeof document !== 'undefined'
        ? createPortal(
            <div
              ref={popoverRef}
              className="vault-date-popover"
              role="dialog"
              aria-label="Choose expiry date"
              style={{
                ...popoverStyle,
                visibility: popoverStyle.top == null ? 'hidden' : 'visible',
              }}
            >
              <div className="vault-date-nav">
                <button
                  type="button"
                  className="vault-date-nav-btn"
                  aria-label="Previous month"
                  onClick={() => setCursor(new Date(cursor.getFullYear(), cursor.getMonth() - 1, 1))}
                >
                  ‹
                </button>
                <strong>{monthLabel}</strong>
                <button
                  type="button"
                  className="vault-date-nav-btn"
                  aria-label="Next month"
                  onClick={() => setCursor(new Date(cursor.getFullYear(), cursor.getMonth() + 1, 1))}
                >
                  ›
                </button>
              </div>
              <div className="vault-date-weekdays">
                {WEEKDAYS.map((label, index) => (
                  <span key={`${label}-${index}`}>{label}</span>
                ))}
              </div>
              <div className="vault-date-grid">
                {days.map((date, index) => {
                  if (!date) return <span key={`empty-${index}`} className="vault-date-empty" />
                  const key = toDayKey(date)
                  const isSelected = selected ? toDayKey(selected) === key : false
                  return (
                    <button
                      key={key}
                      type="button"
                      className={`vault-date-day ${isSelected ? 'selected' : ''} ${key === todayKey ? 'today' : ''}`}
                      onClick={() => {
                        onChange(key)
                        setOpen(false)
                      }}
                    >
                      {date.getDate()}
                    </button>
                  )
                })}
              </div>
              <div className="vault-date-footer">
                <button
                  type="button"
                  className="vault-date-clear"
                  onClick={() => {
                    onChange('')
                    setOpen(false)
                  }}
                >
                  No expiry
                </button>
              </div>
            </div>,
            document.body,
          )
        : null}
    </div>
  )
}

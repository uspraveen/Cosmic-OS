import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import {
  clampRectToArea,
  fullDisplayRect,
  mapWindowRectToFramePhysical,
  normalizeDragRect,
  resizeRect,
  type ScreenshotDisplayGeometry,
  type ScreenshotFrameSize,
  type ScreenshotPoint,
  type ScreenshotRect,
  type ScreenshotResizeHandle,
} from './screenshotBounds'
import { SCREENSHOT_LAYER_CLASS } from './windowInteractivity'
import './screenshot.css'

export interface ScreenshotOverlayProps {
  /** Object URL for the frozen display frame captured when the shortcut fired. */
  frameUrl: string
  /** Display geometry in DIPs plus where the overlay window sits inside it. */
  geometry: ScreenshotDisplayGeometry
  /** Pixel size of the frozen frame (physical px). */
  frameSize: ScreenshotFrameSize
  /** Pre-selected rect in window CSS px, auto-bound to the active surface. */
  initialRect: ScreenshotRect
  onConfirm: (rect: ScreenshotRect) => void
  onCancel: () => void
}

type ScreenshotDragState =
  | { kind: 'new'; pointerId: number; anchor: ScreenshotPoint }
  | { kind: 'move'; pointerId: number; origin: ScreenshotPoint; startRect: ScreenshotRect }
  | {
      kind: 'resize'
      pointerId: number
      handle: ScreenshotResizeHandle
      origin: ScreenshotPoint
      startRect: ScreenshotRect
    }

const RESIZE_HANDLES: ScreenshotResizeHandle[] = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w']
const MIN_SELECTION_SIDE = 4

const clamp = (value: number, min: number, max: number) => Math.min(Math.max(value, min), max)

export default function ScreenshotOverlay({
  frameUrl,
  geometry,
  frameSize,
  initialRect,
  onConfirm,
  onCancel,
}: ScreenshotOverlayProps) {
  const layerRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<ScreenshotDragState | null>(null)
  const [rect, setRect] = useState<ScreenshotRect>(initialRect)
  const rectRef = useRef(rect)
  rectRef.current = rect

  const imageLeft = geometry.bounds.x - geometry.workArea.x
  const imageTop = geometry.bounds.y - geometry.workArea.y
  const physicalRect = mapWindowRectToFramePhysical(rect, geometry, frameSize)
  // Keep the size pill on screen when the selection touches an edge (full
  // display with a top/left taskbar is the common case).
  const labelTop = rect.y >= 30 ? rect.y - 26 : Math.max(4, rect.y + 6)
  const labelLeft = Math.min(Math.max(4, rect.x + 2), Math.max(4, window.innerWidth - 150))

  const commit = useCallback(() => {
    const current = rectRef.current
    if (current.width < MIN_SELECTION_SIDE || current.height < MIN_SELECTION_SIDE) return
    onConfirm(current)
  }, [onConfirm])

  const cancel = useCallback(() => {
    onCancel()
  }, [onCancel])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        cancel()
        return
      }
      if (event.key === 'Enter') {
        event.preventDefault()
        event.stopPropagation()
        commit()
        return
      }
      if (event.key === 'f' || event.key === 'F') {
        event.preventDefault()
        event.stopPropagation()
        setRect(fullDisplayRect(geometry))
      }
    }
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [cancel, commit, geometry])

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    const target = event.target as HTMLElement
    const handle = target.dataset.screenshotHandle as ScreenshotResizeHandle | undefined
    const insideSelection = Boolean(target.closest('[data-screenshot-selection]'))
    const point = { x: event.clientX, y: event.clientY }
    event.preventDefault()
    try {
      event.currentTarget.setPointerCapture(event.pointerId)
    } catch {
      // Capture is best-effort; drags still track while the pointer stays inside.
    }
    if (handle) {
      dragRef.current = { kind: 'resize', pointerId: event.pointerId, handle, origin: point, startRect: rectRef.current }
    } else if (insideSelection) {
      dragRef.current = { kind: 'move', pointerId: event.pointerId, origin: point, startRect: rectRef.current }
    } else {
      dragRef.current = { kind: 'new', pointerId: event.pointerId, anchor: point }
      setRect({ x: point.x, y: point.y, width: 0, height: 0 })
    }
  }

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    const area = { x: 0, y: 0, width: window.innerWidth, height: window.innerHeight }
    const point = { x: clamp(event.clientX, 0, area.width), y: clamp(event.clientY, 0, area.height) }
    if (drag.kind === 'new') {
      setRect(clampRectToArea(normalizeDragRect(drag.anchor, point), area))
      return
    }
    const dx = point.x - drag.origin.x
    const dy = point.y - drag.origin.y
    if (drag.kind === 'move') {
      setRect(
        clampRectToArea(
          { ...drag.startRect, x: drag.startRect.x + dx, y: drag.startRect.y + dy },
          area,
        ),
      )
      return
    }
    setRect(clampRectToArea(resizeRect(drag.startRect, drag.handle, dx, dy), area))
  }

  const endDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    dragRef.current = null
    try {
      if (event.currentTarget.hasPointerCapture(event.pointerId)) {
        event.currentTarget.releasePointerCapture(event.pointerId)
      }
    } catch {
      // Already released (pointercancel / lost capture) — nothing to do.
    }
  }

  return (
    <div
      ref={layerRef}
      className={SCREENSHOT_LAYER_CLASS}
      role="dialog"
      aria-modal="true"
      aria-label="Screenshot snipper"
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onContextMenu={(event) => event.preventDefault()}
    >
      <img
        className="screenshot-frozen-frame"
        src={frameUrl}
        alt=""
        draggable={false}
        style={{
          left: `${imageLeft}px`,
          top: `${imageTop}px`,
          width: `${geometry.bounds.width}px`,
          height: `${geometry.bounds.height}px`,
        }}
      />

      <div
        data-screenshot-selection="true"
        className="screenshot-selection"
        style={{
          left: `${rect.x}px`,
          top: `${rect.y}px`,
          width: `${rect.width}px`,
          height: `${rect.height}px`,
        }}
        onDoubleClick={(event) => {
          event.preventDefault()
          event.stopPropagation()
          commit()
        }}
      >
        {RESIZE_HANDLES.map((handle) => (
          <span
            key={handle}
            data-screenshot-handle={handle}
            className={`screenshot-handle handle-${handle}`}
          />
        ))}
      </div>

      <div
        className="screenshot-size-label"
        style={{ left: `${labelLeft}px`, top: `${labelTop}px` }}
      >
        {physicalRect.width} × {physicalRect.height}
      </div>

      <div className="screenshot-hints" aria-hidden="true">
        <span className="screenshot-hint-strong">Drag to snip</span>
        <span>F · full display</span>
        <span>Enter · copy</span>
        <span>Esc · cancel</span>
      </div>
    </div>
  )
}

/**
 * Pure geometry + surface-resolution logic for the in-app screenshot snipper.
 *
 * The snipping UI itself lives in `ScreenshotOverlay.tsx`; everything here is
 * DOM-free so it can be unit tested. Two responsibilities:
 *
 *  1. Pick which Cosmic surface the shortcut should pre-select ("auto-bound"):
 *     the settings sheet, the launcher, meeting/spaces, the chat column, or the
 *     island when the rest of the app is hidden.
 *  2. Translate that selection between the three coordinate spaces involved:
 *     window CSS px (what the renderer draws), display DIP bounds/workArea (what
 *     Electron reports), and the captured frame's physical pixels.
 */

export interface ScreenshotRect {
  x: number
  y: number
  width: number
  height: number
}

export interface ScreenshotPoint {
  x: number
  y: number
}

export interface ScreenshotFrameSize {
  width: number
  height: number
}

export interface ScreenshotDisplayGeometry {
  /** Full display rect in DIPs (Electron `Display.bounds`). */
  bounds: ScreenshotRect
  /** Usable area in DIPs — where the overlay window actually sits. */
  workArea: ScreenshotRect
}

export type ScreenshotSurfaceId =
  | 'settings'
  | 'launcher'
  | 'meeting'
  | 'spaces'
  | 'task'
  | 'chat'
  | 'composer'
  | 'island'

export interface ScreenshotSurfaceRects {
  settings: ScreenshotRect | null
  launcher: ScreenshotRect | null
  meeting: ScreenshotRect | null
  spaces: ScreenshotRect | null
  task: ScreenshotRect | null
  composer: ScreenshotRect | null
  response: ScreenshotRect | null
  island: ScreenshotRect | null
}

export interface ScreenshotSurfaceState {
  /** Whether the main search/chat surface is on screen (island-only when false). */
  visible: boolean
  mode: 'chat' | 'task' | 'meeting' | 'spaces'
  launcherTrayOpen: boolean
  composerFocused: boolean
}

export interface ScreenshotAutoBound {
  surface: ScreenshotSurfaceId
  rect: ScreenshotRect
}

const MIN_RECT_SIDE = 4

export function isUsableRect(rect: ScreenshotRect | null | undefined): rect is ScreenshotRect {
  return Boolean(
    rect &&
      Number.isFinite(rect.x) &&
      Number.isFinite(rect.y) &&
      Number.isFinite(rect.width) &&
      Number.isFinite(rect.height) &&
      rect.width >= MIN_RECT_SIDE &&
      rect.height >= MIN_RECT_SIDE,
  )
}

/** Build a top-left rect from a drag anchor and the current pointer position. */
export function normalizeDragRect(anchor: ScreenshotPoint, point: ScreenshotPoint): ScreenshotRect {
  const x = Math.min(anchor.x, point.x)
  const y = Math.min(anchor.y, point.y)
  return {
    x,
    y,
    width: Math.abs(point.x - anchor.x),
    height: Math.abs(point.y - anchor.y),
  }
}

/** Keep a rect inside a containing area, preserving its size where possible. */
export function clampRectToArea(rect: ScreenshotRect, area: ScreenshotRect): ScreenshotRect {
  const width = Math.min(rect.width, area.width)
  const height = Math.min(rect.height, area.height)
  const x = Math.min(Math.max(rect.x, area.x), area.x + area.width - width)
  const y = Math.min(Math.max(rect.y, area.y), area.y + area.height - height)
  return { x, y, width, height }
}

/** Smallest rect covering every usable input, or null when none are usable. */
export function unionRects(rects: Array<ScreenshotRect | null | undefined>): ScreenshotRect | null {
  let left = Number.POSITIVE_INFINITY
  let top = Number.POSITIVE_INFINITY
  let right = Number.NEGATIVE_INFINITY
  let bottom = Number.NEGATIVE_INFINITY
  for (const rect of rects) {
    if (!isUsableRect(rect)) continue
    left = Math.min(left, rect.x)
    top = Math.min(top, rect.y)
    right = Math.max(right, rect.x + rect.width)
    bottom = Math.max(bottom, rect.y + rect.height)
  }
  if (!Number.isFinite(left) || !Number.isFinite(top)) return null
  return { x: left, y: top, width: right - left, height: bottom - top }
}

/**
 * Deterministic "whichever surface is active" pick. The app's own focus wins
 * first (a focused composer bounds to the textbox), then modal/large surfaces,
 * then the chat column as one bound (response + composer read as a single
 * card stack), then the island for the hidden state.
 */
export function pickAutoBoundRect(
  rects: ScreenshotSurfaceRects,
  state: ScreenshotSurfaceState,
): ScreenshotAutoBound | null {
  if (!state.visible) {
    return isUsableRect(rects.island) ? { surface: 'island', rect: rects.island } : null
  }

  if (isUsableRect(rects.settings)) {
    return { surface: 'settings', rect: rects.settings }
  }
  if (state.launcherTrayOpen && isUsableRect(rects.launcher)) {
    return { surface: 'launcher', rect: rects.launcher }
  }
  if (state.mode === 'meeting' && isUsableRect(rects.meeting)) {
    return { surface: 'meeting', rect: rects.meeting }
  }
  if (state.mode === 'spaces' && isUsableRect(rects.spaces)) {
    return { surface: 'spaces', rect: rects.spaces }
  }

  if (state.composerFocused && isUsableRect(rects.composer)) {
    return { surface: 'composer', rect: rects.composer }
  }

  const mainColumn = unionRects([rects.response, rects.composer])
  if (mainColumn) {
    return { surface: state.mode === 'task' ? 'task' : 'chat', rect: mainColumn }
  }
  if (isUsableRect(rects.task)) {
    return { surface: 'task', rect: rects.task }
  }
  if (isUsableRect(rects.island)) {
    return { surface: 'island', rect: rects.island }
  }
  return null
}

/** Full-display selection in window CSS px. May extend past the window. */
export function fullDisplayRect(geometry: ScreenshotDisplayGeometry): ScreenshotRect {
  return {
    x: geometry.bounds.x - geometry.workArea.x,
    y: geometry.bounds.y - geometry.workArea.y,
    width: geometry.bounds.width,
    height: geometry.bounds.height,
  }
}

/**
 * Map a window CSS-px rect to pixels on the captured frame. The frame covers
 * the whole display, while the window only covers the workArea, so the offset
 * between the two is applied before scaling. The effective scale is derived
 * from the captured frame's real size — never assumed from `scaleFactor`.
 */
export function mapWindowRectToFramePhysical(
  rect: ScreenshotRect,
  geometry: ScreenshotDisplayGeometry,
  frameSize: ScreenshotFrameSize,
): ScreenshotRect {
  const scale = geometry.bounds.width > 0 ? frameSize.width / geometry.bounds.width : 1
  const rawX = Math.round((geometry.workArea.x - geometry.bounds.x + rect.x) * scale)
  const rawY = Math.round((geometry.workArea.y - geometry.bounds.y + rect.y) * scale)
  const x = Math.min(Math.max(0, rawX), Math.max(0, frameSize.width - 1))
  const y = Math.min(Math.max(0, rawY), Math.max(0, frameSize.height - 1))
  const width = Math.max(1, Math.min(Math.round(rect.width * scale), frameSize.width - x))
  const height = Math.max(1, Math.min(Math.round(rect.height * scale), frameSize.height - y))
  return { x, y, width, height }
}

export type ScreenshotResizeHandle = 'n' | 's' | 'e' | 'w' | 'nw' | 'ne' | 'sw' | 'se'

/** Resize a rect by dragging one of its eight handles. */
export function resizeRect(
  rect: ScreenshotRect,
  handle: ScreenshotResizeHandle,
  dx: number,
  dy: number,
): ScreenshotRect {
  let left = rect.x
  let top = rect.y
  let right = rect.x + rect.width
  let bottom = rect.y + rect.height

  if (handle.includes('w')) left += dx
  if (handle.includes('e')) right += dx
  if (handle.includes('n')) top += dy
  if (handle.includes('s')) bottom += dy

  return {
    x: Math.min(left, right),
    y: Math.min(top, bottom),
    width: Math.abs(right - left),
    height: Math.abs(bottom - top),
  }
}

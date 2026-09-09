/**
 * Translating desktop input into events the live browser can replay.
 *
 * Two things here are easy to get wrong and silent when they are.
 *
 * **Coordinates.** The live frame is drawn with `object-fit: contain`, so the
 * image is letterboxed inside its element whenever the aspect ratios differ.
 * Measuring against the element box therefore aims every click off by the
 * letterbox — worse near the edges, perfect in the middle, which is exactly
 * the pattern that survives a casual test. Everything below measures against
 * the *content* box and reports a normalized 0..1 position, so neither side
 * has to know the other's pixel size: the frame is downscaled from the page
 * (1024 wide against a 1280 viewport) and either could change.
 *
 * **Volume.** A pointer emits mousemove at display rate. Sending each one
 * would spend an internet round trip per frame of hand movement to deliver
 * positions that were already stale. Moves are coalesced to the last one in a
 * batch; presses, releases, wheels and keys are never dropped, because those
 * are the events with meaning.
 */

/** CDP `Input.dispatch*` payloads, in the shape the agent's whitelist accepts. */
export interface TakeoverInputEvent {
  kind: 'mouse' | 'key' | 'text'
  type?: string
  x?: number
  y?: number
  button?: string
  clickCount?: number
  deltaX?: number
  deltaY?: number
  modifiers?: number
  key?: string
  code?: string
  text?: string
  windowsVirtualKeyCode?: number
}

export interface FrameGeometry {
  /** Bounding box of the <img> element. */
  rect: { left: number; top: number; width: number; height: number }
  /** Intrinsic size of the frame currently painted. */
  naturalWidth: number
  naturalHeight: number
}

/**
 * Where a screen point falls on the frame, as 0..1 of the image content.
 * Returns null for a point in the letterbox, which is not part of the page and
 * must not be clamped onto its edge.
 */
export const mapPointerToFrame = (
  geometry: FrameGeometry,
  clientX: number,
  clientY: number,
): { x: number; y: number } | null => {
  const { rect, naturalWidth, naturalHeight } = geometry
  if (!(rect.width > 0) || !(rect.height > 0) || !(naturalWidth > 0) || !(naturalHeight > 0)) return null
  // object-fit: contain — the image is scaled to the limiting axis and centred.
  const scale = Math.min(rect.width / naturalWidth, rect.height / naturalHeight)
  const renderedWidth = naturalWidth * scale
  const renderedHeight = naturalHeight * scale
  const offsetX = (rect.width - renderedWidth) / 2
  const offsetY = (rect.height - renderedHeight) / 2
  const x = (clientX - rect.left - offsetX) / renderedWidth
  const y = (clientY - rect.top - offsetY) / renderedHeight
  if (x < 0 || x > 1 || y < 0 || y > 1) return null
  return { x, y }
}

const BUTTONS = ['left', 'middle', 'right', 'back', 'forward'] as const

/** CDP packs modifiers into one int: alt 1, ctrl 2, meta 4, shift 8. */
export const modifierMask = (event: {
  altKey?: boolean
  ctrlKey?: boolean
  metaKey?: boolean
  shiftKey?: boolean
}): number =>
  (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) | (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0)

export const mouseInputEvent = (
  type: 'mousePressed' | 'mouseReleased' | 'mouseMoved' | 'mouseWheel',
  position: { x: number; y: number },
  source: {
    button?: number
    detail?: number
    deltaX?: number
    deltaY?: number
    altKey?: boolean
    ctrlKey?: boolean
    metaKey?: boolean
    shiftKey?: boolean
  },
): TakeoverInputEvent => {
  const event: TakeoverInputEvent = {
    kind: 'mouse',
    type,
    x: position.x,
    y: position.y,
    modifiers: modifierMask(source),
  }
  if (type === 'mouseWheel') {
    event.deltaX = source.deltaX || 0
    event.deltaY = source.deltaY || 0
    event.button = 'none'
    event.clickCount = 0
    return event
  }
  if (type === 'mouseMoved') {
    event.button = 'none'
    event.clickCount = 0
    return event
  }
  event.button = BUTTONS[source.button ?? 0] || 'left'
  event.clickCount = Math.min(3, Math.max(1, source.detail || 1))
  return event
}

/** Keys that must reach the page as text as well as a keystroke. */
const isPrintable = (key: string): boolean => key.length === 1

export const keyInputEvent = (
  type: 'keyDown' | 'keyUp',
  source: {
    key: string
    code?: string
    keyCode?: number
    altKey?: boolean
    ctrlKey?: boolean
    metaKey?: boolean
    shiftKey?: boolean
  },
): TakeoverInputEvent => {
  const modifiers = modifierMask(source)
  const event: TakeoverInputEvent = {
    kind: 'key',
    type,
    key: source.key,
    code: source.code,
    modifiers,
    windowsVirtualKeyCode: source.keyCode,
  }
  // Chrome only inserts a character when the event carries text, and only for
  // a keyDown. Sending it while ctrl/meta is held would turn a shortcut into
  // a typed letter.
  if (type === 'keyDown' && isPrintable(source.key) && !source.ctrlKey && !source.metaKey) {
    event.text = source.key
  }
  return event
}

/**
 * Collapse a batch to what is still worth sending.
 *
 * Only trailing intermediate moves are dropped: a move that happens before a
 * press is what positions that press, so it has to survive. Anything that is
 * not a move is kept in order.
 */
export const coalesceInputEvents = (events: TakeoverInputEvent[]): TakeoverInputEvent[] => {
  const out: TakeoverInputEvent[] = []
  for (let index = 0; index < events.length; index += 1) {
    const event = events[index]
    const isMove = event.kind === 'mouse' && event.type === 'mouseMoved'
    if (isMove) {
      const next = events[index + 1]
      const nextIsMove = next && next.kind === 'mouse' && next.type === 'mouseMoved'
      if (nextIsMove) continue // a fresher position for the same gesture follows
    }
    out.push(event)
  }
  return out
}

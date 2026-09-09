import { describe, expect, it } from 'vitest'
import {
  coalesceInputEvents,
  keyInputEvent,
  mapPointerToFrame,
  modifierMask,
  mouseInputEvent,
  type TakeoverInputEvent,
} from './browserTakeoverInput'

// A 1024x640 frame drawn inside a wider box: object-fit: contain leaves
// vertical bars on the left and right, which is where off-by-letterbox
// aiming bugs live.
const letterboxedHorizontally = {
  rect: { left: 0, top: 0, width: 1200, height: 640 },
  naturalWidth: 1024,
  naturalHeight: 640,
}

// The same frame in a taller box: bars top and bottom instead.
const letterboxedVertically = {
  rect: { left: 0, top: 0, width: 1024, height: 800 },
  naturalWidth: 1024,
  naturalHeight: 640,
}

const exact = { rect: { left: 0, top: 0, width: 1024, height: 640 }, naturalWidth: 1024, naturalHeight: 640 }

describe('mapPointerToFrame', () => {
  it('maps the centre to the centre whatever the letterboxing', () => {
    for (const geometry of [exact, letterboxedHorizontally, letterboxedVertically]) {
      const point = mapPointerToFrame(
        geometry,
        geometry.rect.left + geometry.rect.width / 2,
        geometry.rect.top + geometry.rect.height / 2,
      )
      expect(point?.x).toBeCloseTo(0.5, 6)
      expect(point?.y).toBeCloseTo(0.5, 6)
    }
  })

  it('measures from the image content, not the element box', () => {
    // 1200 wide box, 1024 of image: 88px of bar each side. The left edge of
    // the picture is at x=88, and that is 0 — measuring against the element
    // would have called it 0.073 and aimed every click short.
    const point = mapPointerToFrame(letterboxedHorizontally, 88, 0)
    expect(point?.x).toBeCloseTo(0, 6)
    expect(point?.y).toBeCloseTo(0, 6)
  })

  it('returns null inside the letterbox rather than clamping to the edge', () => {
    // A click on the bar is not a click on the page. Clamping would fire it at
    // the leftmost column of the site instead.
    expect(mapPointerToFrame(letterboxedHorizontally, 10, 300)).toBeNull()
    expect(mapPointerToFrame(letterboxedVertically, 500, 10)).toBeNull()
  })

  it('accounts for an element that is not at the origin', () => {
    const geometry = { ...exact, rect: { left: 200, top: 100, width: 1024, height: 640 } }
    const point = mapPointerToFrame(geometry, 200 + 512, 100 + 320)
    expect(point?.x).toBeCloseTo(0.5, 6)
    expect(point?.y).toBeCloseTo(0.5, 6)
  })

  it('maps the far corner to exactly 1', () => {
    const point = mapPointerToFrame(exact, 1024, 640)
    expect(point?.x).toBeCloseTo(1, 6)
    expect(point?.y).toBeCloseTo(1, 6)
  })

  it('returns null before a frame has loaded', () => {
    expect(mapPointerToFrame({ ...exact, naturalWidth: 0, naturalHeight: 0 }, 10, 10)).toBeNull()
    expect(
      mapPointerToFrame({ ...exact, rect: { left: 0, top: 0, width: 0, height: 0 } }, 10, 10),
    ).toBeNull()
  })
})

describe('modifierMask', () => {
  it('packs modifiers the way CDP expects', () => {
    expect(modifierMask({})).toBe(0)
    expect(modifierMask({ altKey: true })).toBe(1)
    expect(modifierMask({ ctrlKey: true })).toBe(2)
    expect(modifierMask({ metaKey: true })).toBe(4)
    expect(modifierMask({ shiftKey: true })).toBe(8)
    expect(modifierMask({ ctrlKey: true, shiftKey: true })).toBe(10)
  })
})

describe('mouseInputEvent', () => {
  it('translates a left click', () => {
    const event = mouseInputEvent('mousePressed', { x: 0.5, y: 0.25 }, { button: 0, detail: 1 })
    expect(event).toMatchObject({ kind: 'mouse', type: 'mousePressed', button: 'left', clickCount: 1 })
  })

  it('translates a right click', () => {
    expect(mouseInputEvent('mousePressed', { x: 0, y: 0 }, { button: 2 }).button).toBe('right')
  })

  it('caps a rapid multi-click', () => {
    expect(mouseInputEvent('mousePressed', { x: 0, y: 0 }, { detail: 9 }).clickCount).toBe(3)
  })

  it('sends a move with no button held', () => {
    const event = mouseInputEvent('mouseMoved', { x: 0.1, y: 0.2 }, { button: 0 })
    expect(event.button).toBe('none')
    expect(event.clickCount).toBe(0)
  })

  it('carries wheel deltas', () => {
    const event = mouseInputEvent('mouseWheel', { x: 0.5, y: 0.5 }, { deltaX: 0, deltaY: 240 })
    expect(event.deltaY).toBe(240)
    expect(event.button).toBe('none')
  })
})

describe('keyInputEvent', () => {
  it('sends text with a printable keydown so the character is inserted', () => {
    const event = keyInputEvent('keyDown', { key: 'a', code: 'KeyA', keyCode: 65 })
    expect(event.text).toBe('a')
    expect(event.windowsVirtualKeyCode).toBe(65)
  })

  it('never sends text on keyup', () => {
    expect(keyInputEvent('keyUp', { key: 'a', code: 'KeyA' }).text).toBeUndefined()
  })

  it('omits text for a named key', () => {
    // "Enter" is not a character; sending it as text would type the word.
    expect(keyInputEvent('keyDown', { key: 'Enter', code: 'Enter' }).text).toBeUndefined()
    expect(keyInputEvent('keyDown', { key: 'Backspace' }).text).toBeUndefined()
  })

  it('omits text while a shortcut modifier is held', () => {
    // Ctrl+A is "select all", not the letter a.
    expect(keyInputEvent('keyDown', { key: 'a', ctrlKey: true }).text).toBeUndefined()
    expect(keyInputEvent('keyDown', { key: 'a', metaKey: true }).text).toBeUndefined()
  })

  it('keeps text for a shifted character', () => {
    // Shift is part of producing the character, unlike ctrl/meta.
    expect(keyInputEvent('keyDown', { key: 'A', shiftKey: true }).text).toBe('A')
  })
})

describe('coalesceInputEvents', () => {
  const move = (x: number): TakeoverInputEvent => ({ kind: 'mouse', type: 'mouseMoved', x, y: 0 })
  const press: TakeoverInputEvent = { kind: 'mouse', type: 'mousePressed', x: 0.5, y: 0.5 }

  it('keeps only the freshest of a run of moves', () => {
    const out = coalesceInputEvents([move(0.1), move(0.2), move(0.3)])
    expect(out).toHaveLength(1)
    expect(out[0].x).toBe(0.3)
  })

  it('keeps the move that positions a press', () => {
    // Dropping this one would fire the click wherever the pointer last was.
    const out = coalesceInputEvents([move(0.1), press, move(0.9)])
    expect(out).toHaveLength(3)
    expect(out[0].x).toBe(0.1)
  })

  it('never drops a press, release, wheel or key', () => {
    const events: TakeoverInputEvent[] = [
      press,
      { kind: 'mouse', type: 'mouseReleased', x: 0.5, y: 0.5 },
      { kind: 'mouse', type: 'mouseWheel', deltaY: 100 },
      { kind: 'key', type: 'keyDown', key: 'a' },
    ]
    expect(coalesceInputEvents(events)).toHaveLength(4)
  })

  it('preserves order', () => {
    const out = coalesceInputEvents([press, move(0.4), { kind: 'key', type: 'keyDown', key: 'b' }])
    expect(out.map((e) => e.type)).toEqual(['mousePressed', 'mouseMoved', 'keyDown'])
  })

  it('handles an empty batch', () => {
    expect(coalesceInputEvents([])).toEqual([])
  })
})

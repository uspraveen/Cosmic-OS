import { describe, expect, it } from 'vitest'
import {
  clampRectToArea,
  fullDisplayRect,
  mapWindowRectToFramePhysical,
  normalizeDragRect,
  pickAutoBoundRect,
  resizeRect,
  unionRects,
  type ScreenshotRect,
  type ScreenshotSurfaceRects,
  type ScreenshotSurfaceState,
} from './screenshotBounds'

const rect = (x: number, y: number, width: number, height: number): ScreenshotRect => ({ x, y, width, height })

const emptyRects = (overrides: Partial<ScreenshotSurfaceRects> = {}): ScreenshotSurfaceRects => ({
  settings: null,
  launcher: null,
  meeting: null,
  spaces: null,
  task: null,
  composer: null,
  response: null,
  island: null,
  ...overrides,
})

const chatState = (overrides: Partial<ScreenshotSurfaceState> = {}): ScreenshotSurfaceState => ({
  visible: true,
  mode: 'chat',
  launcherTrayOpen: false,
  composerFocused: false,
  ...overrides,
})

describe('normalizeDragRect', () => {
  it('builds a forward drag rect', () => {
    expect(normalizeDragRect({ x: 10, y: 20 }, { x: 110, y: 220 })).toEqual(rect(10, 20, 100, 200))
  })

  it('normalizes a backward drag to a top-left rect', () => {
    expect(normalizeDragRect({ x: 110, y: 220 }, { x: 10, y: 20 })).toEqual(rect(10, 20, 100, 200))
  })
})

describe('clampRectToArea', () => {
  it('keeps a rect that already fits', () => {
    expect(clampRectToArea(rect(10, 10, 100, 50), rect(0, 0, 500, 500))).toEqual(rect(10, 10, 100, 50))
  })

  it('pushes an overflow rect back inside', () => {
    expect(clampRectToArea(rect(460, 480, 100, 50), rect(0, 0, 500, 500))).toEqual(rect(400, 450, 100, 50))
  })

  it('shrinks a rect larger than the area', () => {
    expect(clampRectToArea(rect(-20, -20, 900, 900), rect(0, 0, 500, 300))).toEqual(rect(0, 0, 500, 300))
  })
})

describe('unionRects', () => {
  it('returns null when nothing is usable', () => {
    expect(unionRects([null, undefined, rect(0, 0, 0, 50)])).toBeNull()
  })

  it('covers every usable rect', () => {
    expect(unionRects([rect(10, 10, 100, 100), rect(200, 50, 50, 300)])).toEqual(rect(10, 10, 240, 340))
  })
})

describe('resizeRect', () => {
  it('moves the west edge', () => {
    expect(resizeRect(rect(100, 100, 200, 100), 'w', 20, 0)).toEqual(rect(120, 100, 180, 100))
  })

  it('moves the south-east corner', () => {
    expect(resizeRect(rect(100, 100, 200, 100), 'se', 30, 40)).toEqual(rect(100, 100, 230, 140))
  })

  it('normalizes when an edge crosses over', () => {
    expect(resizeRect(rect(100, 100, 20, 20), 'w', 50, 0)).toEqual(rect(120, 100, 30, 20))
  })
})

describe('pickAutoBoundRect', () => {
  it('bounds to the island when the app is hidden', () => {
    expect(pickAutoBoundRect(emptyRects({ island: rect(800, 10, 320, 42) }), chatState({ visible: false })))
      .toEqual({ surface: 'island', rect: rect(800, 10, 320, 42) })
  })

  it('returns null when hidden without an island rect', () => {
    expect(pickAutoBoundRect(emptyRects(), chatState({ visible: false }))).toBeNull()
  })

  it('prefers the settings sheet above everything else', () => {
    const picked = pickAutoBoundRect(
      emptyRects({
        settings: rect(400, 100, 600, 500),
        response: rect(300, 100, 600, 400),
        composer: rect(300, 520, 600, 60),
      }),
      chatState(),
    )
    expect(picked?.surface).toBe('settings')
  })

  it('prefers the launcher tray while it is open', () => {
    const picked = pickAutoBoundRect(
      emptyRects({ launcher: rect(500, 600, 440, 90), composer: rect(500, 600, 440, 90) }),
      chatState({ launcherTrayOpen: true }),
    )
    expect(picked?.surface).toBe('launcher')
  })

  it('bounds to the meeting surface in meeting mode', () => {
    const picked = pickAutoBoundRect(
      emptyRects({ meeting: rect(100, 100, 900, 600) }),
      chatState({ mode: 'meeting' }),
    )
    expect(picked?.surface).toBe('meeting')
  })

  it('bounds to the spaces surface in spaces mode', () => {
    const picked = pickAutoBoundRect(
      emptyRects({ spaces: rect(100, 100, 900, 600) }),
      chatState({ mode: 'spaces' }),
    )
    expect(picked?.surface).toBe('spaces')
  })

  it('bounds the focused composer to the textbox', () => {
    const picked = pickAutoBoundRect(
      emptyRects({ composer: rect(300, 700, 600, 60), response: rect(300, 100, 600, 500) }),
      chatState({ composerFocused: true }),
    )
    expect(picked).toEqual({ surface: 'composer', rect: rect(300, 700, 600, 60) })
  })

  it('unions the chat response and composer into one column', () => {
    const picked = pickAutoBoundRect(
      emptyRects({ response: rect(300, 100, 600, 500), composer: rect(300, 620, 600, 60) }),
      chatState(),
    )
    expect(picked).toEqual({ surface: 'chat', rect: rect(300, 100, 600, 580) })
  })

  it('falls back to the composer when there is no response yet', () => {
    const picked = pickAutoBoundRect(emptyRects({ composer: rect(300, 700, 600, 60) }), chatState())
    expect(picked).toEqual({ surface: 'chat', rect: rect(300, 700, 600, 60) })
  })

  it('falls back to the task rail when only it is on screen', () => {
    const picked = pickAutoBoundRect(emptyRects({ task: rect(1000, 200, 340, 300) }), chatState())
    expect(picked).toEqual({ surface: 'task', rect: rect(1000, 200, 340, 300) })
  })
})

describe('fullDisplayRect', () => {
  it('covers the whole display from the window origin', () => {
    const geometry = {
      bounds: rect(0, 0, 2560, 1440),
      workArea: rect(0, 0, 2560, 1400),
    }
    expect(fullDisplayRect(geometry)).toEqual(rect(0, 0, 2560, 1440))
  })

  it('offsets the selection when the taskbar sits left or top', () => {
    const geometry = {
      bounds: rect(0, 0, 1920, 1080),
      workArea: rect(80, 40, 1840, 1040),
    }
    expect(fullDisplayRect(geometry)).toEqual(rect(-80, -40, 1920, 1080))
  })
})

describe('mapWindowRectToFramePhysical', () => {
  const geometry = { bounds: rect(0, 0, 1920, 1080), workArea: rect(0, 0, 1920, 1040) }

  it('maps at 1x', () => {
    expect(mapWindowRectToFramePhysical(rect(100, 200, 300, 150), geometry, { width: 1920, height: 1080 }))
      .toEqual(rect(100, 200, 300, 150))
  })

  it('maps at 1.5x using the real frame size for scale', () => {
    expect(mapWindowRectToFramePhysical(rect(100, 200, 300, 150), geometry, { width: 2880, height: 1620 }))
      .toEqual(rect(150, 300, 450, 225))
  })

  it('applies the work-area offset before scaling', () => {
    const offsetGeometry = { bounds: rect(0, 0, 1920, 1080), workArea: rect(0, 48, 1920, 1032) }
    expect(mapWindowRectToFramePhysical(rect(10, 10, 100, 100), offsetGeometry, { width: 1920, height: 1080 }))
      .toEqual(rect(10, 58, 100, 100))
  })

  it('clamps a full-display rect with negative origin into the frame', () => {
    const offsetGeometry = { bounds: rect(0, 0, 1920, 1080), workArea: rect(80, 40, 1840, 1040) }
    const mapped = mapWindowRectToFramePhysical(fullDisplayRect(offsetGeometry), offsetGeometry, {
      width: 2880,
      height: 1620,
    })
    expect(mapped).toEqual(rect(0, 0, 2880, 1620))
  })

  it('never produces a zero or negative crop', () => {
    const mapped = mapWindowRectToFramePhysical(rect(1919, 1079, 50, 50), geometry, { width: 1920, height: 1080 })
    expect(mapped.width).toBeGreaterThan(0)
    expect(mapped.height).toBeGreaterThan(0)
  })
})

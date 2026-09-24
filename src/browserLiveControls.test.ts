import { describe, expect, it } from 'vitest'
import { isBrowserRunLive, resolveBrowserLiveControls, type BrowserLiveControlsInput } from './browserLiveControls'

const controls = (overrides: Partial<BrowserLiveControlsInput> = {}) =>
  resolveBrowserLiveControls({
    awaitingInput: false,
    lightboxOpen: false,
    takeoverAvailable: true,
    driving: false,
    ...overrides,
  })

describe('resolveBrowserLiveControls', () => {
  it('keeps the ask panel in the run card while the expanded view is closed', () => {
    expect(controls({ awaitingInput: true }).askPlacement).toBe('inline')
  })

  it('moves the ask panel into the expanded view rather than leaving it behind the portal', () => {
    expect(controls({ awaitingInput: true, lightboxOpen: true }).askPlacement).toBe('docked')
  })

  it('never mounts the ask panel in two places at once', () => {
    // The regression this guards: two panels would be two surfaces racing to
    // answer the same request id.
    for (const lightboxOpen of [false, true]) {
      for (const driving of [false, true]) {
        const placement = controls({ awaitingInput: true, lightboxOpen, driving }).askPlacement
        expect(['inline', 'docked']).toContain(placement)
      }
    }
  })

  it('renders no ask panel when nothing is waiting', () => {
    expect(controls({ lightboxOpen: true }).askPlacement).toBe('none')
    expect(controls({ awaitingInput: false }).showWaitingChip).toBe(false)
  })

  it('keeps take control while an interrupt is pending, with the chip as status', () => {
    // Questions are exactly when hands on the page matter (solve this CAPTCHA,
    // finish this login) — the run is paused by the question, but the wheel
    // must stay reachable.
    const waiting = controls({ awaitingInput: true, lightboxOpen: true })
    expect(waiting.showTakeControl).toBe(true)
    expect(waiting.showWaitingChip).toBe(true)
  })

  it('never drops the takeover button without putting the chip in its place', () => {
    // The original defect: the control simply disappeared from the rail.
    const withdrawn = controls({ awaitingInput: true, lightboxOpen: true, takeoverAvailable: true })
    expect(withdrawn.showTakeControl || withdrawn.showWaitingChip).toBe(true)
  })

  it('offers take control on a live run with nothing waiting', () => {
    const open = controls({ lightboxOpen: true })
    expect(open.showTakeControl).toBe(true)
    expect(open.showWaitingChip).toBe(false)
  })

  it('hides take control on a run it cannot address', () => {
    expect(controls({ lightboxOpen: true, takeoverAvailable: false }).showTakeControl).toBe(false)
  })

  it('swaps take control for the takeover readout once driving', () => {
    const driving = controls({ lightboxOpen: true, driving: true })
    expect(driving.showTakeControl).toBe(false)
    expect(driving.showTakeoverState).toBe(true)
  })

  it('still announces an interrupt that lands while this client holds the wheel', () => {
    // Rare (a parked run is not stepping, so it does not ask), but the card is
    // blocking: it must be visible and answerable even mid-takeover.
    const both = controls({ awaitingInput: true, lightboxOpen: true, driving: true })
    expect(both.askPlacement).toBe('docked')
    expect(both.showWaitingChip).toBe(true)
    expect(both.showTakeoverState).toBe(true)
  })

  it('keeps the chip out of the collapsed card, which has its own status pill', () => {
    expect(controls({ awaitingInput: true, lightboxOpen: false }).showWaitingChip).toBe(false)
  })
})

describe('browser run liveness', () => {
  it('keeps an explicitly running browser available when the assistant stream moves', () => {
    expect(isBrowserRunLive('running', false)).toBe(true)
  })

  it('withdraws takeover on every terminal browser phase even while the assistant writes', () => {
    for (const phase of ['finished', 'failed', 'cancelled'] as const) {
      expect(isBrowserRunLive(phase, true)).toBe(false)
    }
  })

  it('uses assistant streaming only for older progress without a phase', () => {
    expect(isBrowserRunLive(undefined, true)).toBe(true)
    expect(isBrowserRunLive(undefined, false)).toBe(false)
  })
})

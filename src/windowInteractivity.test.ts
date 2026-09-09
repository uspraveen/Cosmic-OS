import { describe, expect, it } from 'vitest'
import { PORTAL_SURFACE_CLASS, hitTestPointerTarget, type PointerHitTarget } from './windowInteractivity'

/** A stand-in for the element under the pointer: `ancestors` is the set of
 *  selectors that `closest` would match from that point. */
const at = (...ancestors: string[]): PointerHitTarget => ({
  closest: (selector: string) => (ancestors.includes(selector) ? {} : null),
})

const visible = { searchVisible: true }
const hidden = { searchVisible: false }

describe('hitTestPointerTarget', () => {
  it('treats empty space as click-through', () => {
    expect(hitTestPointerTarget(at(), visible)).toEqual({ islandHovered: false, interactive: false })
  })

  it('treats a missing element as click-through', () => {
    expect(hitTestPointerTarget(null, visible)).toEqual({ islandHovered: false, interactive: false })
  })

  it('keeps the island interactive and marks it hovered', () => {
    expect(hitTestPointerTarget(at('.island'), visible)).toEqual({ islandHovered: true, interactive: true })
  })

  it('keeps the overlay interactive without counting as island hover', () => {
    expect(hitTestPointerTarget(at('.overlay'), visible)).toEqual({ islandHovered: false, interactive: true })
  })

  it('drops overlay clicks once the surface is hidden', () => {
    expect(hitTestPointerTarget(at('.overlay'), hidden).interactive).toBe(false)
  })

  it('keeps the island interactive even while the search surface is hidden', () => {
    expect(hitTestPointerTarget(at('.island'), hidden).interactive).toBe(true)
  })

  // The regression this module exists for: a lightbox portaled to document.body
  // is not inside `.overlay`, so it used to hit-test as empty space. The window
  // went click-through the moment the pointer entered it, which made the whole
  // app look frozen — its own close button included.
  it('keeps a body-portaled surface interactive', () => {
    expect(hitTestPointerTarget(at(`.${PORTAL_SURFACE_CLASS}`), visible).interactive).toBe(true)
  })

  it('keeps a control inside a portaled surface interactive', () => {
    const closeButton = at(`.${PORTAL_SURFACE_CLASS}`, '.browser-run-lightbox-close')
    expect(hitTestPointerTarget(closeButton, visible).interactive).toBe(true)
  })

  it('does not let a portaled surface count as island hover', () => {
    expect(hitTestPointerTarget(at(`.${PORTAL_SURFACE_CLASS}`), visible).islandHovered).toBe(false)
  })

  it('drops a stale portaled surface once the app is hidden', () => {
    expect(hitTestPointerTarget(at(`.${PORTAL_SURFACE_CLASS}`), hidden).interactive).toBe(false)
  })

  it('keeps the cron notice interactive regardless of surface visibility', () => {
    expect(hitTestPointerTarget(at('.cron-result-shell'), hidden).interactive).toBe(true)
  })
})

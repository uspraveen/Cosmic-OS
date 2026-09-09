/**
 * Which pointer positions make the Cosmic window interactive.
 *
 * The window is a transparent, always-on-top overlay covering the whole work
 * area, so it is click-through by default and only becomes solid where real UI
 * sits under the cursor. Main flips that with `setIgnoreMouseEvents`, driven by
 * a hit test the renderer runs on every mousemove.
 *
 * The hit test walks up from `document.elementFromPoint`, which means a surface
 * rendered through `createPortal(..., document.body)` is invisible to it: the
 * portal escapes `.overlay` on purpose (to leave its stacking context) and so
 * escapes this test too. A portaled surface therefore paints normally, accepts
 * hover, and drops every click straight through to whatever is behind Cosmic —
 * indistinguishable from a frozen app, and unescapable, because the close
 * button it puts on screen is inside the same dead region.
 *
 * `PORTAL_SURFACE_CLASS` is how a portaled surface opts back in. Any element
 * portaled outside `.overlay` must carry it.
 */

/** Marker for surfaces portaled to document.body that still want clicks. */
export const PORTAL_SURFACE_CLASS = 'cosmic-portal-surface'

/** The minimum of `Element` this needs — kept structural so it can be tested
 *  without a DOM. */
export interface PointerHitTarget {
  closest(selector: string): unknown
}

export interface PointerHitResult {
  /** Hovering the island proper (or settings), which keeps the island expanded. */
  islandHovered: boolean
  /** Whether the window should accept clicks at this point. */
  interactive: boolean
}

/**
 * @param target element under the pointer, or null when there is none
 * @param options.searchVisible whether the main search/chat surface is on screen;
 *   while it is hidden its DOM may still exist, and stale surfaces must not
 *   capture clicks meant for the desktop
 */
export const hitTestPointerTarget = (
  target: PointerHitTarget | null,
  options: { searchVisible: boolean },
): PointerHitResult => {
  if (!target) return { islandHovered: false, interactive: false }
  const island = Boolean(target.closest('.island'))
  const settings = Boolean(target.closest('.settings-overlay'))
  // The overlay must stay interactive for clicks, but must not count as island
  // hover — otherwise Cosmic UI keeps the full home slide open.
  const islandHovered = island || settings
  const overlay = options.searchVisible && Boolean(target.closest('.overlay'))
  const portalSurface = options.searchVisible && Boolean(target.closest(`.${PORTAL_SURFACE_CLASS}`))
  const cronNotice = Boolean(target.closest('.cron-result-shell'))
  return {
    islandHovered,
    interactive: islandHovered || overlay || portalSurface || cronNotice,
  }
}

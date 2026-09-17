/**
 * Which controls the expanded browser view shows, and where the interrupt card
 * is rendered.
 *
 * The expanded view is portaled to document.body and covers the whole window,
 * so the inline run card — and the ask panel that lives inside it — is painted
 * behind it. An interrupt arriving while the view was open was therefore
 * invisible: the page simply stopped moving. Worse, "Take control" vanished at
 * the same moment (a pending interrupt supersedes takeover, so the run is not
 * ours to pause), leaving a live browser, one fewer button, and no explanation.
 *
 * The rules are few but have to hold together, which is why they are resolved
 * here instead of inline:
 *
 *  - the ask panel renders in exactly ONE place. Two copies would be two
 *    surfaces racing to answer the same request id.
 *  - the waiting chip appears exactly when a pending interrupt is what took
 *    the takeover button away, so the bar never just loses a control.
 *  - takeover suppression and the chip that explains it are one decision, not
 *    two that can drift apart.
 */

/** Where the interrupt card is mounted for the current view state. */
export type BrowserAskPlacement = 'none' | 'inline' | 'docked'

export interface BrowserLiveControlsInput {
  /** An interrupt is on screen and still unanswered. */
  awaitingInput: boolean
  /** The expanded (lightbox) view is actually painted, not merely requested. */
  lightboxOpen: boolean
  /** Takeover is possible at all: a live run with an addressable task id.
   * Whether a pending interrupt then withdraws it is decided here. */
  takeoverAvailable: boolean
  /** This client has asked for the wheel, or already holds it. */
  driving: boolean
}

export interface BrowserLiveControls {
  /** 'docked' = inside the expanded view, below the frame. 'inline' = in the
   * run card, where it has always been. Never both. */
  askPlacement: BrowserAskPlacement
  showTakeControl: boolean
  /** The "You have control" / "Pausing…" readout and its Give back button. */
  showTakeoverState: boolean
  /** The amber "waiting on you" chip in the expanded view's control rail. */
  showWaitingChip: boolean
}

export const resolveBrowserLiveControls = ({
  awaitingInput,
  lightboxOpen,
  takeoverAvailable,
  driving,
}: BrowserLiveControlsInput): BrowserLiveControls => ({
  askPlacement: !awaitingInput ? 'none' : lightboxOpen ? 'docked' : 'inline',
  // An unanswered interrupt has already stopped the run; asking it to pause
  // again is meaningless, so the button goes. The chip below says why.
  showTakeControl: takeoverAvailable && !awaitingInput && !driving,
  showTakeoverState: driving,
  // Only the expanded view has a control rail to put it in — the run card
  // shows the same state in its status pill and in the panel itself.
  showWaitingChip: awaitingInput && lightboxOpen,
})

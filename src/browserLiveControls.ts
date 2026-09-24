/**
 * Which controls the expanded browser view shows, and where the interrupt card
 * is rendered.
 *
 * The expanded view is portaled to document.body and covers the whole window,
 * so the inline run card — and the ask panel that lives inside it — is painted
 * behind it. An interrupt arriving while the view was open was therefore
 * invisible: the page simply stopped moving.
 *
 * The rules are few but have to hold together, which is why they are resolved
 * here instead of inline:
 *
 *  - the ask panel renders in exactly ONE place. Two copies would be two
 *    surfaces racing to answer the same request id.
 *  - the waiting chip is the status readout for a pending interrupt — but it
 *    no longer stands in for a withdrawn button: Take control stays available
 *    while a question is pending, because questions are exactly when hands on
 *    the page matter (solve this CAPTCHA, finish this login). The run is
 *    waiting for an answer; takeover queues the handoff at its next safe
 *    step. The button and waiting chip are resolved together here.
 */

/** Where the interrupt card is mounted for the current view state. */
export type BrowserAskPlacement = 'none' | 'inline' | 'docked'

/** The browser agent owns this lifecycle. Older stored runs have no phase, so
 * they retain the assistant-stream fallback until a terminal marker arrives. */
export const isBrowserRunLive = (
  phase: 'running' | 'finished' | 'failed' | 'cancelled' | undefined,
  assistantStreaming: boolean,
): boolean => phase === 'running' || (phase === undefined && assistantStreaming)

export interface BrowserLiveControlsInput {
  /** An interrupt is on screen and still unanswered. */
  awaitingInput: boolean
  /** The expanded (lightbox) view is actually painted, not merely requested. */
  lightboxOpen: boolean
  /** Takeover is possible: an active run with an addressable task id.
   * A pending interrupt does not withdraw the button. */
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
  // A pending interrupt pauses the run, but it does not take the wheel away:
  // exactly when a question needs hands on the page (solve this CAPTCHA,
  // finish this login), Take control must stay. Arming it queues the pause at
  // the next step boundary; answering the card lets the run park into it.
  showTakeControl: takeoverAvailable && !driving,
  showTakeoverState: driving,
  // Only the expanded view has a control rail to put it in — the run card
  // shows the same state in its status pill and in the panel itself.
  showWaitingChip: awaitingInput && lightboxOpen,
})

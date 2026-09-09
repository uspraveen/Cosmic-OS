/**
 * The browser agent reports one live state per step — the step it is on right
 * now — and nothing about where it has been. The card wants the last few
 * finished steps, so the trail is accumulated on this side: whenever a new
 * reading arrives, the description that *was* current becomes history.
 *
 * This lives outside App.tsx because the accumulation has to survive being
 * replayed. The background-task path upserts a reading and then immediately
 * patches with the same one, and a history refresh can re-deliver a reading
 * already folded in — so merging must be idempotent, which is exactly the
 * kind of thing worth pinning down in a test.
 */

export interface BrowserRunTrailEntry {
  step: number | null
  text: string
}

/** The parts of a browser_progress reading this merge actually reads. */
export interface BrowserRunProgressLike {
  step?: number | null
  description?: string
  screenshot?: unknown
  trail?: BrowserRunTrailEntry[]
}

/** Enough history for the card (which shows three) plus room to re-render
 * after a reading arrives out of order. */
export const BROWSER_TRAIL_LIMIT = 6

const cleanText = (value: unknown): string => (typeof value === 'string' ? value.trim() : '')

const cleanStep = (value: unknown): number | null => (typeof value === 'number' ? value : null)

/** Normalize a trail that rode in on a raw payload — live state is folded back
 * into event payloads on the task-mirror paths, so an accumulated trail has to
 * survive that round trip. */
export const normalizeBrowserTrail = (value: unknown): BrowserRunTrailEntry[] | undefined => {
  if (!Array.isArray(value)) {
    return undefined
  }
  const entries = value
    .map((item) => {
      const entry = (item && typeof item === 'object' ? item : {}) as Record<string, unknown>
      return { step: cleanStep(entry.step), text: cleanText(entry.text) }
    })
    .filter((entry) => Boolean(entry.text))
    .slice(-BROWSER_TRAIL_LIMIT)
  return entries.length > 0 ? entries : undefined
}

/**
 * Fold a fresh reading into the one already on screen, growing the trail by
 * the step that just finished.
 *
 * A reading that carries its own trail is trusted as-is (it came back from a
 * mirror that already did this), and a step that ships no screenshot keeps the
 * last one rather than blanking the viewport mid-run.
 */
export const mergeBrowserRunProgress = <T extends BrowserRunProgressLike>(
  previous: T | undefined,
  incoming: T | undefined,
): T | undefined => {
  if (!incoming) {
    return previous
  }
  if (!previous) {
    return incoming
  }
  const previousText = cleanText(previous.description)
  const previousStep = cleanStep(previous.step)
  const movedOn = previousText !== cleanText(incoming.description) || previousStep !== cleanStep(incoming.step)
  let trail = incoming.trail ?? previous.trail ?? []
  if (previousText && movedOn && !incoming.trail) {
    const last = trail[trail.length - 1]
    if (!last || last.text !== previousText || last.step !== previousStep) {
      trail = [...trail, { step: previousStep, text: previousText }].slice(-BROWSER_TRAIL_LIMIT)
    }
  }
  return {
    ...incoming,
    trail: trail.length > 0 ? trail : undefined,
    screenshot: incoming.screenshot ?? previous.screenshot ?? null,
  } as T
}

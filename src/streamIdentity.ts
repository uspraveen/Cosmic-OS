// Stream identity: decides which chat bubble a given gateway event belongs to.
//
// The desktop client can have several logically independent event streams
// interleaved on the same WebSocket at once: the user's own foreground
// request, an autonomous single-shot delivery (heartbeat digest, Gmail
// surface decision, cron result), or a promoted background task. Each such
// stream is identified by a `request_id` and/or `task_id`. Exactly one of
// them is treated as "the" active foreground stream at a time (the one
// driving the streaming indicator, composer-disabled state, and the stop
// button).
//
// Autonomous deliveries frequently arrive as a single `response.complete`
// (or `task.failed` / `task.cancelled` / `error`) event that was never
// preceded by a `route_result` / `task.created` event for that same id, so
// it has no prior binding to a chat message. Without an explicit identity
// check, naively falling back to "whatever the last assistant bubble is" or
// "clear the global streaming flag" corrupts an unrelated, still-in-flight
// stream (e.g. an incoming email reply gets spliced into a long-running
// document-editing task's answer, or its completion prematurely re-enables
// the composer for the still-streaming task).

export interface StreamEventIdentity {
  request_id?: unknown
  task_id?: unknown
}

const normalizeId = (value: unknown): string => (typeof value === 'string' ? value.trim() : '')

export const extractEventStreamIds = (event: StreamEventIdentity | null | undefined) => ({
  requestId: normalizeId(event?.request_id),
  taskId: normalizeId(event?.task_id),
})

/**
 * True when `event` belongs to the stream currently tracked as active
 * (matching by request id or task id), or when there is no meaningful
 * identity information to disambiguate with (either side is unset), in
 * which case we conservatively assume it's the same stream (legacy/no-op
 * safety net rather than silently dropping state updates).
 */
export const isEventForActiveStream = (
  event: StreamEventIdentity | null | undefined,
  activeRequestId: string | null | undefined,
  activeTaskId: string | null | undefined,
): boolean => {
  const { requestId: eventRequestId, taskId: eventTaskId } = extractEventStreamIds(event)
  const normalizedActiveRequestId = normalizeId(activeRequestId)
  const normalizedActiveTaskId = normalizeId(activeTaskId)

  if (!normalizedActiveRequestId && !normalizedActiveTaskId) {
    return true
  }
  if (!eventRequestId && !eventTaskId) {
    return true
  }
  return (
    (Boolean(eventRequestId) && eventRequestId === normalizedActiveRequestId) ||
    (Boolean(eventTaskId) && eventTaskId === normalizedActiveTaskId)
  )
}

/**
 * True when it's safe to bind `event` to the most recently appended
 * assistant message rather than minting a brand-new bubble for it. Only
 * events with no identity of their own (legacy continuation) or events that
 * belong to the currently active stream should ever take this path -
 * anything else (an unrelated autonomous delivery) must get its own message.
 */
export const shouldBindToLastAssistantMessage = (
  event: StreamEventIdentity | null | undefined,
  activeRequestId: string | null | undefined,
  activeTaskId: string | null | undefined,
): boolean => {
  const { requestId: eventRequestId, taskId: eventTaskId } = extractEventStreamIds(event)
  const hasOwnIdentity = Boolean(eventRequestId || eventTaskId)
  return !hasOwnIdentity || isEventForActiveStream(event, activeRequestId, activeTaskId)
}

/**
 * True when a `task.created` event may bind its task id into the singular
 * active-stream slot. This is stricter than "the task slot is empty":
 * while a foreground request is streaming, its task slot is usually still
 * empty (the request id arrives first, the orchestrator task id later), so
 * an unrelated autonomous run (heartbeat digest, cron, Gmail surface
 * decision) racing in with its own `task.created` would otherwise claim the
 * task slot and produce a chimera identity — half the user's stream, half
 * the autonomous one. The autonomous run's terminal event would then match
 * the active stream by task id, clearing the user's streaming state
 * mid-answer and pointing the stop button at the wrong stream.
 */
export const canAdoptTaskForActiveStream = (
  event: StreamEventIdentity | null | undefined,
  activeRequestId: string | null | undefined,
  activeTaskId: string | null | undefined,
): boolean => {
  const { requestId: eventRequestId, taskId: eventTaskId } = extractEventStreamIds(event)
  if (!eventTaskId) {
    return false
  }
  const normalizedActiveRequestId = normalizeId(activeRequestId)
  if (normalizedActiveRequestId && eventRequestId === normalizedActiveRequestId) {
    // The active stream's own task (including a newer task id spawned by the
    // same request) may always (re)bind.
    return true
  }
  const normalizedActiveTaskId = normalizeId(activeTaskId)
  if (normalizedActiveTaskId) {
    return normalizedActiveTaskId === eventTaskId
  }
  if (normalizedActiveRequestId) {
    // A different (or identity-less) request while a stream is in flight:
    // never adopt its task into the active slot.
    return false
  }
  return true
}

/**
 * True when claiming the singular "active stream" slot for `incomingId`
 * would not clobber a different stream that's already in flight. Use before
 * overwriting an active-request/active-task ref from an event that starts a
 * new stream (`route_result`, `task.created`).
 */
export const canClaimActiveStreamSlot = (
  incomingId: string | null | undefined,
  currentActiveId: string | null | undefined,
): boolean => {
  const normalizedIncomingId = normalizeId(incomingId)
  const normalizedCurrentActiveId = normalizeId(currentActiveId)
  return !normalizedCurrentActiveId || normalizedCurrentActiveId === normalizedIncomingId
}

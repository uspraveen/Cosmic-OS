// Messages the user sends while Cosmic is working.
//
// They go into the transcript straight away as ordinary user bubbles. Until
// the agent takes one in, the bubble carries a pending marker (rendered as a
// subtle spinner, never as a "queued"/"waiting" label). Then one of two
// things happens:
//
// * The running task reads it at its next step (`user_message.delivered`).
//   The bubble moves into that reply as a steer anchor, at the point in the
//   reply where the model read it (the gateway stamps the stream offset), so
//   what follows it is what the model wrote after reading it. The anchor is
//   persisted with the reply, so a reload shows the same thing.
// * It could not be read in time and runs as its own turn afterwards
//   (`user_message.dispatched`, or a plain `route_result`). The pending
//   marker just goes away: it is now a normal query with its own answer.
//
// Everything here is pure so the transitions are unit-tested.

export type MidRunPendingState = 'sending' | 'waiting' | 'failed'

export interface MidRunPending {
  state: MidRunPendingState
  /** Set while a cancel (withdraw) is in flight. */
  withdrawing?: boolean
  error?: string | null
  /** Has the gateway acknowledged holding it? (drives reconcile on resume) */
  acked?: boolean
}

export interface SteerAnchor {
  requestId: string
  content: string
  /** Visible stream length of the reply when the model read the message. */
  offset: number
  deliveredAt?: string | null
}

export interface MidRunAwareMessage {
  id: string
  role: string
  content: string
  requestId?: string | null
  midRun?: MidRunPending
  steerAnchors?: SteerAnchor[]
}

const cleanId = (value: unknown): string => (typeof value === 'string' ? value.trim() : '')

export const isMidRunPending = (message: MidRunAwareMessage | undefined | null): boolean =>
  Boolean(message && message.role === 'user' && message.midRun)

export const findPendingBubble = <M extends MidRunAwareMessage>(messages: M[], requestId: unknown): M | undefined => {
  const id = cleanId(requestId)
  if (!id) return undefined
  return messages.find((message) => message.role === 'user' && message.midRun && message.requestId === id)
}

/** Apply `patch` to the pending bubble for `requestId` (no-op if none). */
export const patchPendingBubble = <M extends MidRunAwareMessage>(
  messages: M[],
  requestId: unknown,
  patch: Partial<MidRunPending>,
): M[] => {
  const id = cleanId(requestId)
  let changed = false
  const next = messages.map((message) => {
    if (!id || message.role !== 'user' || !message.midRun || message.requestId !== id) return message
    changed = true
    return { ...message, midRun: { ...message.midRun, ...patch } }
  })
  return changed ? next : messages
}

/** The message is a normal turn now (or never needed holding): drop the marker. */
export const settlePendingBubble = <M extends MidRunAwareMessage>(messages: M[], requestId: unknown): M[] => {
  const id = cleanId(requestId)
  let changed = false
  const next = messages.map((message) => {
    if (!id || message.role !== 'user' || !message.midRun || message.requestId !== id) return message
    changed = true
    const settled = { ...message }
    delete settled.midRun
    return settled
  })
  return changed ? next : messages
}

export const removePendingBubble = <M extends MidRunAwareMessage>(messages: M[], requestId: unknown): M[] => {
  const id = cleanId(requestId)
  if (!id) return messages
  const next = messages.filter((message) => !(message.role === 'user' && message.midRun && message.requestId === id))
  return next.length === messages.length ? messages : next
}

export const normalizeSteerAnchors = (value: unknown): SteerAnchor[] | undefined => {
  if (!Array.isArray(value)) return undefined
  const anchors: SteerAnchor[] = []
  const seen = new Set<string>()
  for (const raw of value) {
    if (!raw || typeof raw !== 'object') continue
    const item = raw as Record<string, unknown>
    const requestId = cleanId(item.request_id ?? item.requestId)
    const offsetRaw = item.stream_offset ?? item.offset
    const offset = typeof offsetRaw === 'number' && Number.isFinite(offsetRaw) ? Math.max(0, Math.floor(offsetRaw)) : null
    if (!requestId || offset === null || seen.has(requestId)) continue
    seen.add(requestId)
    const deliveredAt = item.delivered_at ?? item.deliveredAt
    anchors.push({
      requestId,
      content: typeof item.content === 'string' ? item.content : '',
      offset,
      deliveredAt: typeof deliveredAt === 'string' ? deliveredAt : null,
    })
  }
  return anchors.length > 0 ? anchors : undefined
}

/** Union by request id, keeping the first-seen anchor (offsets never move). */
export const mergeSteerAnchors = (
  current: SteerAnchor[] | undefined,
  incoming: SteerAnchor[] | undefined,
): SteerAnchor[] | undefined => {
  if (!incoming || incoming.length === 0) return current
  if (!current || current.length === 0) return incoming
  const known = new Set(current.map((anchor) => anchor.requestId))
  const added = incoming.filter((anchor) => !known.has(anchor.requestId))
  return added.length > 0 ? [...current, ...added] : current
}

/**
 * `user_message.delivered`: the running reply read the message. Move it out
 * of the tail of the transcript and into that reply at the stamped offset.
 * If the reply is not on screen (another window, a race with a reload) the
 * bubble simply stops being pending, where it is.
 */
export const applySteerDelivery = <M extends MidRunAwareMessage>(
  messages: M[],
  event: { request_id?: unknown; into_request_id?: unknown; content?: unknown; stream_offset?: unknown; delivered_at?: unknown },
): M[] => {
  const requestId = cleanId(event.request_id)
  const intoRequestId = cleanId(event.into_request_id)
  if (!requestId) return messages
  let targetIndex = -1
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]
    if (message.role === 'assistant' && intoRequestId && message.requestId === intoRequestId) {
      targetIndex = index
      break
    }
  }
  if (targetIndex < 0) {
    return settlePendingBubble(messages, requestId)
  }
  const bubble = messages.find((message) => message.role === 'user' && message.requestId === requestId)
  const content = typeof event.content === 'string' && event.content.trim()
    ? event.content
    : bubble?.content || ''
  const offsetRaw = event.stream_offset
  const target = messages[targetIndex]
  const offset = typeof offsetRaw === 'number' && Number.isFinite(offsetRaw)
    ? Math.max(0, Math.floor(offsetRaw))
    : String(target.content || '').length
  const anchor: SteerAnchor = {
    requestId,
    content,
    offset,
    deliveredAt: typeof event.delivered_at === 'string' ? event.delivered_at : null,
  }
  const steerAnchors = mergeSteerAnchors(target.steerAnchors, [anchor])
  return messages
    .map((message, index) => (index === targetIndex ? { ...message, steerAnchors } : message))
    // The bubble now lives inside the reply; drop the standalone copy.
    .filter((message) => !(message.role === 'user' && message.requestId === requestId))
}

/**
 * A steered message is persisted twice on purpose: as a user row (for the
 * model's context and other clients) and as an anchor on the reply that read
 * it (for placement). Show it once, inline in the reply.
 */
export const dropSteeredUserDuplicates = <M extends MidRunAwareMessage>(messages: M[]): M[] => {
  const anchored = new Set<string>()
  for (const message of messages) {
    if (message.role !== 'assistant') continue
    for (const anchor of message.steerAnchors || []) anchored.add(anchor.requestId)
  }
  if (anchored.size === 0) return messages
  const next = messages.filter((message) => !(
    message.role === 'user' && message.requestId && anchored.has(message.requestId)
  ))
  return next.length === messages.length ? messages : next
}

export interface HeldServerItem {
  requestId: string
  content: string
}

export const normalizeHeldServerItems = (value: unknown): HeldServerItem[] => {
  if (!Array.isArray(value)) return []
  const items: HeldServerItem[] = []
  for (const raw of value) {
    if (!raw || typeof raw !== 'object') continue
    const item = raw as Record<string, unknown>
    const requestId = cleanId(item.request_id)
    if (!requestId) continue
    items.push({ requestId, content: typeof item.content === 'string' ? item.content : '' })
  }
  return items
}

/**
 * Reconcile pending bubbles with the gateway's held queue after a reconnect.
 * Held items keep (or get back) a pending bubble at the tail. Acknowledged
 * bubbles the gateway no longer holds were read or ran while we were away;
 * the restored history shows them, so the pending copy goes. Bubbles the
 * gateway never acknowledged (still sending, or failed) are kept.
 */
export const reconcilePendingBubbles = <M extends MidRunAwareMessage>(
  messages: M[],
  serverItems: unknown,
  makeBubble: (item: HeldServerItem) => M,
): M[] => {
  const held = normalizeHeldServerItems(serverItems)
  const heldIds = new Set(held.map((item) => item.requestId))
  let next = messages.filter((message) => !(
    message.role === 'user' && message.midRun && message.midRun.acked && message.requestId && !heldIds.has(message.requestId)
  ))
  const present = new Set(next.filter((message) => message.role === 'user').map((message) => message.requestId))
  const missing = held.filter((item) => !present.has(item.requestId))
  next = next.map((message) => (
    message.role === 'user' && message.midRun && message.requestId && heldIds.has(message.requestId)
      ? { ...message, midRun: { ...message.midRun, state: 'waiting' as const, acked: true } }
      : message
  ))
  if (missing.length > 0) next = [...next, ...missing.map(makeBubble)]
  return next
}

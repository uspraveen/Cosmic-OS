// Mid-run messages: the composer is never locked while Cosmic works.
//
// A message sent while a foreground run is streaming does not start a new
// turn. The gateway holds it (gateway/mid_run_queue.py) and either:
//   - steers it into the running task at the next safe step boundary (after
//     the current tool call / model turn), so the model reads it before it
//     picks its next step (Enter, the default - like Codex "steer" mode), or
//   - queues it to run as its own turn when the current one ends (Tab).
// A steer that cannot land (direct-model answer, run already wrapping up,
// attachments) quietly becomes a queued follow-up, so nothing is dropped.
//
// The gateway is the source of truth. This module keeps the composer's
// "pending" tray in sync with it: local optimistic items for messages still
// in flight to the VM, reconciled against the server's queue snapshots and
// its delivered / dispatched / withdrawn events.

export type MidRunMode = 'steer' | 'queue'

export type MidRunItemState =
  | 'sending'        // handed to the desktop bridge, VM has not acked yet
  | 'pending_steer'  // VM holds it; will be steered into the run when it can
  | 'steering'       // inside the running task's inbox: read at the next step
  | 'queued'         // runs as its own turn after the current one
  | 'starting'       // the previous run ended; this is being started as a turn
  | 'failed'         // never reached the VM

export interface MidRunQueueItem {
  requestId: string
  content: string
  mode: MidRunMode
  state: MidRunItemState
  attachmentCount: number
  createdAt: string
  /** True once the gateway has acknowledged the item at least once. */
  serverSeen: boolean
  /** A cancel/edit is in flight; the item may still turn out delivered. */
  withdrawing: boolean
  error?: string | null
}

const SERVER_STATES: ReadonlySet<string> = new Set(['pending_steer', 'steering', 'queued'])

const normalizeServerItem = (value: unknown): MidRunQueueItem | null => {
  if (!value || typeof value !== 'object') return null
  const raw = value as Record<string, unknown>
  const requestId = typeof raw.request_id === 'string' ? raw.request_id.trim() : ''
  if (!requestId) return null
  const state = typeof raw.state === 'string' && SERVER_STATES.has(raw.state) ? raw.state as MidRunItemState : 'queued'
  return {
    requestId,
    content: typeof raw.content === 'string' ? raw.content : '',
    mode: raw.mode === 'queue' ? 'queue' : 'steer',
    state,
    attachmentCount: Number.isFinite(Number(raw.attachment_count)) ? Number(raw.attachment_count) : 0,
    createdAt: typeof raw.created_at === 'string' ? raw.created_at : new Date().toISOString(),
    serverSeen: true,
    withdrawing: raw.withdrawing === true,
    error: null,
  }
}

export const addLocalMidRunItem = (
  items: MidRunQueueItem[],
  item: { requestId: string; content: string; mode: MidRunMode; attachmentCount?: number; createdAt?: string },
): MidRunQueueItem[] => {
  if (items.some((existing) => existing.requestId === item.requestId)) return items
  return [...items, {
    requestId: item.requestId,
    content: item.content,
    mode: item.mode,
    state: 'sending',
    attachmentCount: item.attachmentCount ?? 0,
    createdAt: item.createdAt ?? new Date().toISOString(),
    serverSeen: false,
    withdrawing: false,
    error: null,
  }]
}

/** Upsert from a `user_message.queued` ack (carries the item). */
export const applyMidRunAck = (items: MidRunQueueItem[], event: unknown): MidRunQueueItem[] => {
  const ack = event && typeof event === 'object' ? (event as { item?: unknown }) : null
  const incoming = normalizeServerItem(ack?.item ?? event)
  if (!incoming) return items
  const index = items.findIndex((item) => item.requestId === incoming.requestId)
  if (index < 0) return [...items, incoming]
  const next = items.slice()
  next[index] = { ...items[index], ...incoming, content: incoming.content || items[index].content }
  return next
}

/**
 * Reconcile against the gateway's authoritative queue (`user_message.queue`
 * or the resume payload). Server items win and keep server order. Local
 * items the server has never acknowledged (still sending, or failed) are kept
 * after them, as are items whose turn is starting (until route_result); other
 * items the server acknowledged but no longer lists have left the queue
 * (delivered or withdrawn) and are dropped. A resume passes
 * keepStarting=false: the restored history already shows those turns.
 */
export const applyMidRunSnapshot = (
  items: MidRunQueueItem[],
  serverItems: unknown,
  { keepStarting = true }: { keepStarting?: boolean } = {},
): MidRunQueueItem[] => {
  if (!Array.isArray(serverItems)) return items
  const server = serverItems.map(normalizeServerItem).filter((item): item is MidRunQueueItem => Boolean(item))
  const serverIds = new Set(server.map((item) => item.requestId))
  const localById = new Map(items.map((item) => [item.requestId, item]))
  const merged = server.map((item) => {
    const local = localById.get(item.requestId)
    return local ? { ...local, ...item, content: item.content || local.content } : item
  })
  // 'starting' items left the server queue on purpose (their turn began);
  // they leave the tray on route_result (or error), not on a snapshot.
  const unacked = items.filter((item) => !serverIds.has(item.requestId) && (
    !item.serverSeen || (keepStarting && item.state === 'starting')
  ))
  return [...merged, ...unacked]
}

export const removeMidRunItem = (items: MidRunQueueItem[], requestId: string): MidRunQueueItem[] => (
  items.some((item) => item.requestId === requestId) ? items.filter((item) => item.requestId !== requestId) : items
)

export const patchMidRunItem = (
  items: MidRunQueueItem[],
  requestId: string,
  patch: Partial<MidRunQueueItem>,
): MidRunQueueItem[] => {
  const index = items.findIndex((item) => item.requestId === requestId)
  if (index < 0) return items
  const next = items.slice()
  next[index] = { ...items[index], ...patch }
  return next
}

export const findMidRunItem = (items: MidRunQueueItem[], requestId: unknown): MidRunQueueItem | null => {
  const id = typeof requestId === 'string' ? requestId.trim() : ''
  if (!id) return null
  return items.find((item) => item.requestId === id) ?? null
}

export const describeMidRunItem = (item: MidRunQueueItem): { label: string; hint: string } => {
  if (item.state === 'failed') {
    return { label: 'Not sent', hint: item.error || 'Could not reach your VM.' }
  }
  if (item.withdrawing) {
    return { label: 'Removing…', hint: 'Taking it back before Cosmic reads it.' }
  }
  if (item.state === 'sending') {
    return { label: 'Sending…', hint: item.mode === 'queue' ? 'Will run after the current task.' : 'Cosmic will read this at its next step.' }
  }
  if (item.state === 'starting') {
    return { label: 'Starting', hint: 'Cosmic finished the last task and is starting on this now.' }
  }
  if (item.state === 'steering') {
    return { label: 'Next step', hint: 'Cosmic reads this as soon as the current step finishes.' }
  }
  if (item.state === 'pending_steer') {
    return { label: 'Waiting', hint: 'Cosmic reads this at its next step, or right after this answer.' }
  }
  return { label: 'Queued', hint: 'Runs as its own message when Cosmic finishes.' }
}

/** Enter steers, Tab queues - only while a run is busy and there is text. */
export const midRunModeForKey = (
  key: string,
  { busy, hasContent, shiftKey }: { busy: boolean; hasContent: boolean; shiftKey?: boolean },
): MidRunMode | null => {
  if (!busy || !hasContent) return null
  if (key === 'Enter' && !shiftKey) return 'steer'
  if (key === 'Tab' && !shiftKey) return 'queue'
  return null
}

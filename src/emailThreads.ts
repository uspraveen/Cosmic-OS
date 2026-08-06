// Email thread grouping for the transcript.
//
// An inbound email reply does not live in the day's session. The gateway gives
// every email thread its own `email-thread:<mailbox>:<thread-id>` session so
// the orchestrator answers with thread-scoped context, and the desktop only
// ever loaded one session — so a thread showed its opening message and nothing
// else. The gateway now sends those messages along, tagged with their thread.
//
// Rendering them inline would be wrong. A thread's messages are minutes or
// hours apart and other channels (mobile, WhatsApp, heartbeats) land in
// between, so a flat chronological splice tears one conversation into pieces
// scattered down the transcript. Instead each thread collapses into a single
// unit holding its messages in order.
//
// The unit sits at the position of the thread's LATEST message, the way a
// mailbox bumps a conversation on reply. Anchoring at the first message
// instead would hoist new replies upward into an already-scrolled-past card,
// which is the one outcome worse than the original bug: a new email that the
// user never sees. Everything that is not part of a thread keeps its exact
// position, so no other channel is displaced.

export interface ThreadableMessage {
  id: string
  emailThreadId?: string | null
  emailThreadSubject?: string | null
}

export interface EmailThreadUnit<T> {
  kind: 'email-thread'
  threadId: string
  subject: string
  /** Thread messages in their original chronological order. */
  messages: T[]
  /** The thread's latest message — the unit renders at its position. */
  anchorId: string
}

export type TranscriptUnit<T> = { kind: 'message'; message: T } | EmailThreadUnit<T>

const threadIdOf = (message: ThreadableMessage): string =>
  typeof message.emailThreadId === 'string' ? message.emailThreadId.trim() : ''

/**
 * Collapse each email thread into one unit anchored at its latest message.
 * Stable and idempotent; non-thread messages keep their position.
 */
export const groupEmailThreads = <T extends ThreadableMessage>(
  messages: T[],
): TranscriptUnit<T>[] => {
  const threadMessages = new Map<string, T[]>()
  for (const message of messages) {
    const threadId = threadIdOf(message)
    if (!threadId) continue
    const existing = threadMessages.get(threadId)
    if (existing) existing.push(message)
    else threadMessages.set(threadId, [message])
  }

  // The anchor is the last occurrence, so the unit lands where the newest
  // message would have been.
  const anchorIdByThread = new Map<string, string>()
  for (const [threadId, grouped] of threadMessages) {
    anchorIdByThread.set(threadId, grouped[grouped.length - 1].id)
  }

  const units: TranscriptUnit<T>[] = []
  for (const message of messages) {
    const threadId = threadIdOf(message)
    if (!threadId) {
      units.push({ kind: 'message', message })
      continue
    }
    if (anchorIdByThread.get(threadId) !== message.id) {
      // Absorbed into its thread's unit, which renders at the anchor.
      continue
    }
    const grouped = threadMessages.get(threadId) ?? [message]
    units.push({
      kind: 'email-thread',
      threadId,
      subject: threadSubject(grouped),
      messages: grouped,
      anchorId: message.id,
    })
  }
  return units
}

/**
 * Label for a thread card. Prefers a stored subject, stripping the reply
 * prefixes that accumulate as a thread goes back and forth.
 */
export const threadSubject = <T extends ThreadableMessage>(messages: T[]): string => {
  for (const message of messages) {
    const subject =
      typeof message.emailThreadSubject === 'string' ? message.emailThreadSubject.trim() : ''
    if (subject) return stripReplyPrefixes(subject)
  }
  return 'Email thread'
}

/**
 * Drop the `Email from: … / Email subject: …` header the gateway prepends to
 * an inbound email's content.
 *
 * That prefix exists so the orchestrator knows who wrote what; on screen it is
 * pure duplication, since the card header already carries the subject and the
 * sender is rendered as its own label. Used for messages stored before the
 * body was kept separately — anything that does not match the exact shape the
 * gateway emits is left completely alone.
 */
export const stripEmailEnvelope = (content: string): string => {
  const lines = content.split('\n')
  let index = 0
  if (/^Email from:\s/.test(lines[index] ?? '')) index += 1
  if (!/^Email subject:\s/.test(lines[index] ?? '')) return content
  index += 1
  // Bounded on purpose: an envelope with no body would otherwise run off the
  // end of the array forever, since a missing line reads as blank.
  while (index < lines.length && lines[index].trim() === '') index += 1
  const body = lines.slice(index).join('\n').trim()
  // An email with no body at all is better shown as-is than as an empty pill.
  return body || content
}

export const stripReplyPrefixes = (subject: string): string => {
  let result = subject.trim()
  // Threads accumulate prefixes ("Re: Fwd: Re: ..."); strip them all.
  for (;;) {
    const stripped = result.replace(/^\s*(re|fwd|fw)\s*(\[\d+\])?\s*:\s*/i, '')
    if (stripped === result) break
    result = stripped
  }
  return result.trim() || subject.trim()
}

/**
 * Message reactions: the gateway's quick emoji receipt on the user's own
 * message. A `message.reaction` websocket event carries the emoji plus the
 * identifiers needed to find the bubble — `request_id` matches the optimistic
 * desktop message, `message_id` matches one loaded from history. Parsing and
 * the message-list fold live here so the reducer stays pure and testable.
 */

export interface ReactionEventInput {
  type?: unknown
  emoji?: unknown
  request_id?: unknown
  message_id?: unknown
  session_id?: unknown
}

export interface ReactionMessageLike {
  id: string
  role: string
  requestId?: string | null
  reaction?: string | null
}

export interface ParsedReactionEvent {
  emoji: string
  requestId: string
  messageId: string
  sessionId: string
}

const asTrimmedString = (value: unknown): string =>
  typeof value === 'string' ? value.trim() : ''

/**
 * Only well-formed events for the session currently on screen survive.
 * Everything else (wrong type, missing emoji, stale session) is ignored —
 * a dropped badge is the correct failure mode.
 */
export const parseReactionEvent = (
  event: ReactionEventInput | null | undefined,
  activeSessionId: string | null | undefined,
): ParsedReactionEvent | null => {
  if (!event || asTrimmedString(event.type) !== 'message.reaction') return null
  const emoji = asTrimmedString(event.emoji)
  if (!emoji) return null
  const sessionId = asTrimmedString(event.session_id)
  if (sessionId && activeSessionId && sessionId !== activeSessionId) return null
  return {
    emoji,
    requestId: asTrimmedString(event.request_id),
    messageId: asTrimmedString(event.message_id),
    sessionId,
  }
}

/** Stamp the emoji on the matching user message, if it is in the list. */
export const applyReactionToMessages = <T extends ReactionMessageLike>(
  messages: T[],
  parsed: ParsedReactionEvent | null,
): T[] => {
  if (!parsed || (!parsed.requestId && !parsed.messageId)) return messages
  let changed = false
  const next = messages.map((message) => {
    if (message.role !== 'user') return message
    const matches =
      (parsed.requestId && message.requestId === parsed.requestId) ||
      (parsed.messageId && message.id === parsed.messageId)
    if (!matches || message.reaction === parsed.emoji) return message
    changed = true
    return { ...message, reaction: parsed.emoji }
  })
  return changed ? next : messages
}

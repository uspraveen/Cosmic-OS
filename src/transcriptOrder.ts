// Transcript ordering: keep every assistant reply attached to the user query
// it answers.
//
// Session history is stored in completion order, not conversational order. An
// autonomous delivery (heartbeat digest, cron result, surfaced email) that
// finishes while the user's own answer is still streaming gets persisted
// BETWEEN the user's query and that query's eventual answer. Rendering the
// raw order therefore shows the heartbeat "directly under the query label"
// and pushes the real answer below it — and the cross-channel renderer, which
// pairs a user message with the message immediately after it, pairs the wrong
// one.
//
// This regroups a flat message list into units: each user message anchors a
// unit and collects every assistant message that answers it (same request
// id, in original relative order); any other message — autonomous deliveries,
// legacy messages without a request id, divider pseudo-messages — anchors its
// own unit. Units keep the position of their anchor, so an interleaved
// heartbeat lands after the full query/answer exchange instead of splitting
// it. The operation is stable and idempotent.

export interface OrderableTranscriptMessage {
  role: string
  requestId?: string | null
}

export const groupRepliesWithTheirQuery = <T extends OrderableTranscriptMessage>(
  messages: T[],
): T[] => {
  const units: T[][] = []
  const unitIndexByRequestId = new Map<string, number>()

  for (const message of messages) {
    const requestId = typeof message.requestId === 'string' ? message.requestId.trim() : ''

    if (message.role === 'user') {
      units.push([message])
      if (requestId && !unitIndexByRequestId.has(requestId)) {
        unitIndexByRequestId.set(requestId, units.length - 1)
      }
      continue
    }

    if (message.role === 'assistant' && requestId && unitIndexByRequestId.has(requestId)) {
      units[unitIndexByRequestId.get(requestId)!].push(message)
      continue
    }

    units.push([message])
  }

  return units.flat()
}

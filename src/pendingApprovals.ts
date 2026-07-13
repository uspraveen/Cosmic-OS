// Pending approval detection: identifies response blocks (sandbox permission
// requests, Gmail/agent-email draft approvals, calendar responses) that are
// still waiting on an explicit user decision.
//
// These cards live inline inside a specific assistant message and never get
// removed from that message once rendered - but the *stream* around them
// (isStreaming, the task rail, "task completed" banners) finishes as soon as
// the model stops talking, which can happen in the very same turn where it
// asks for permission. That leaves the user with no durable signal that
// something still needs their input once they scroll away or start a new
// exchange, which reads as "the response finished before I could respond."
// This module powers a persistent, message-independent indicator for that
// case.

export interface ApprovalLikeBlock {
  id: string
  type: string
  status?: string | null
  canRespond?: boolean
}

const APPROVAL_BLOCK_TYPES = new Set([
  'sandbox_permission_request',
  'gmail_draft_approval',
  'agent_email_draft_approval',
  'calendar_event',
])

// Terminal statuses mean the user (or the system) already resolved this
// block; anything else (including missing/undefined status, which defaults
// to "pending" for a freshly created block) is still awaiting a decision.
const RESOLVED_STATUSES = new Set([
  'approved',
  'rejected',
  'declined',
  'denied',
  'completed',
  'executed',
  'expired',
  'cancelled',
  'canceled',
  'ignored',
  'sent',
  'failed',
])

export const isBlockAwaitingApproval = (block: ApprovalLikeBlock | null | undefined): boolean => {
  if (!block || !APPROVAL_BLOCK_TYPES.has(block.type)) {
    return false
  }
  if (block.canRespond === false) {
    return false
  }
  const status = String(block.status || '').trim().toLowerCase()
  if (!status) {
    return true
  }
  if (status === 'running') {
    // Approved and executing - no longer needs a decision, but isn't
    // resolved-terminal either. Treat as "not awaiting approval" since
    // there's nothing left for the user to click.
    return false
  }
  return !RESOLVED_STATUSES.has(status)
}

export interface MessageWithResponseBlocks {
  id: string
  role: string
  responseBlocks?: ApprovalLikeBlock[]
}

export interface PendingApprovalSummary {
  messageId: string
  blockIds: string[]
}

export const findPendingApprovals = <M extends MessageWithResponseBlocks>(
  messages: M[],
): PendingApprovalSummary[] => {
  const summaries: PendingApprovalSummary[] = []
  for (const message of messages) {
    if (message.role !== 'assistant' || !Array.isArray(message.responseBlocks)) {
      continue
    }
    const blockIds = message.responseBlocks
      .filter((block) => isBlockAwaitingApproval(block))
      .map((block) => block.id)
    if (blockIds.length > 0) {
      summaries.push({ messageId: message.id, blockIds })
    }
  }
  return summaries
}

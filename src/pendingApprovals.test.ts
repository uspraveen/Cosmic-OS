import { describe, expect, it } from 'vitest'
import { findPendingApprovals, isBlockAwaitingApproval } from './pendingApprovals'

describe('isBlockAwaitingApproval', () => {
  it('treats a freshly created sandbox permission block (no status yet) as pending', () => {
    expect(isBlockAwaitingApproval({ id: 'a', type: 'sandbox_permission_request' })).toBe(true)
  })

  it('treats an explicit pending status as pending', () => {
    expect(isBlockAwaitingApproval({ id: 'a', type: 'gmail_draft_approval', status: 'pending' })).toBe(true)
  })

  it('does not flag a resolved (approved) block', () => {
    expect(isBlockAwaitingApproval({ id: 'a', type: 'sandbox_permission_request', status: 'approved' })).toBe(false)
  })

  it('does not flag a running (already approved, executing) sandbox block', () => {
    expect(isBlockAwaitingApproval({ id: 'a', type: 'sandbox_permission_request', status: 'running' })).toBe(false)
  })

  it('does not flag a completed sandbox block', () => {
    expect(isBlockAwaitingApproval({ id: 'a', type: 'sandbox_permission_request', status: 'completed' })).toBe(false)
  })

  it('does not flag a calendar event the user cannot respond to', () => {
    expect(
      isBlockAwaitingApproval({ id: 'a', type: 'calendar_event', status: 'pending', canRespond: false }),
    ).toBe(false)
  })

  it('ignores unrelated block types like markdown', () => {
    expect(isBlockAwaitingApproval({ id: 'a', type: 'markdown' })).toBe(false)
  })
})

describe('findPendingApprovals', () => {
  it('finds a pending sandbox permission block on an assistant message even after streaming ends', () => {
    const messages = [
      { id: 'm1', role: 'user' as const },
      {
        id: 'm2',
        role: 'assistant' as const,
        responseBlocks: [
          { id: 'block1', type: 'sandbox_permission_request', status: 'pending' },
        ],
      },
    ]
    expect(findPendingApprovals(messages)).toEqual([{ messageId: 'm2', blockIds: ['block1'] }])
  })

  it('returns nothing once the block is resolved', () => {
    const messages = [
      {
        id: 'm2',
        role: 'assistant' as const,
        responseBlocks: [
          { id: 'block1', type: 'sandbox_permission_request', status: 'completed' },
        ],
      },
    ]
    expect(findPendingApprovals(messages)).toEqual([])
  })

  it('aggregates multiple pending blocks across messages', () => {
    const messages = [
      {
        id: 'm1',
        role: 'assistant' as const,
        responseBlocks: [{ id: 'block1', type: 'gmail_draft_approval', status: 'pending' }],
      },
      { id: 'm2', role: 'user' as const },
      {
        id: 'm3',
        role: 'assistant' as const,
        responseBlocks: [
          { id: 'block2', type: 'sandbox_permission_request', status: 'pending' },
          { id: 'block3', type: 'calendar_event', status: 'pending' },
        ],
      },
    ]
    expect(findPendingApprovals(messages)).toEqual([
      { messageId: 'm1', blockIds: ['block1'] },
      { messageId: 'm3', blockIds: ['block2', 'block3'] },
    ])
  })
})

import { describe, expect, it } from 'vitest'

import {
  applyReactionToMessages,
  parseReactionEvent,
} from './messageReactions'

interface TestMessage {
  id: string
  role: string
  content: string
  requestId?: string | null
  reaction?: string | null
}

const baseMessage: TestMessage = {
  id: 'user_1',
  role: 'user',
  content: 'How about we post this',
  requestId: 'req_abc',
}

describe('parseReactionEvent', () => {
  it('accepts a well-formed event for the active session', () => {
    expect(
      parseReactionEvent(
        {
          type: 'message.reaction',
          emoji: '👀',
          request_id: 'req_abc',
          message_id: 'msg_1',
          session_id: 'sess_1',
        },
        'sess_1',
      ),
    ).toEqual({
      emoji: '👀',
      requestId: 'req_abc',
      messageId: 'msg_1',
      sessionId: 'sess_1',
    })
  })

  it('ignores other event types and missing emoji', () => {
    expect(parseReactionEvent({ type: 'crosschannel.message' }, 'sess_1')).toBeNull()
    expect(
      parseReactionEvent({ type: 'message.reaction', emoji: '  ' }, 'sess_1'),
    ).toBeNull()
    expect(parseReactionEvent(null, 'sess_1')).toBeNull()
  })

  it('ignores events from a stale session', () => {
    expect(
      parseReactionEvent(
        { type: 'message.reaction', emoji: '👍', session_id: 'sess_old' },
        'sess_now',
      ),
    ).toBeNull()
  })

  it('accepts events without a session id', () => {
    expect(
      parseReactionEvent({ type: 'message.reaction', emoji: '🚀' }, 'sess_1'),
    ).toEqual({ emoji: '🚀', requestId: '', messageId: '', sessionId: '' })
  })
})

describe('applyReactionToMessages', () => {
  it('stamps the emoji on the matching request id', () => {
    const next = applyReactionToMessages([baseMessage], {
      emoji: '👀',
      requestId: 'req_abc',
      messageId: '',
      sessionId: 'sess_1',
    })
    expect(next[0].reaction).toBe('👀')
  })

  it('stamps by message id when history loaded the transcript', () => {
    const historyMessage: TestMessage = { id: 'msg_9', role: 'user', content: 'x' }
    const next = applyReactionToMessages([historyMessage], {
      emoji: '🎉',
      requestId: '',
      messageId: 'msg_9',
      sessionId: '',
    })
    expect(next[0].reaction).toBe('🎉')
  })

  it('never touches assistant messages or non-matches', () => {
    const assistant: TestMessage = { id: 'a_1', role: 'assistant', content: 'y' }
    const other: TestMessage = { id: 'u_2', role: 'user', content: 'z', requestId: 'req_other' }
    const next = applyReactionToMessages([assistant, other], {
      emoji: '👍',
      requestId: 'req_abc',
      messageId: '',
      sessionId: '',
    })
    expect(next[0].reaction).toBeUndefined()
    expect(next[1].reaction).toBeUndefined()
  })

  it('returns the same array when nothing matches', () => {
    const messages = [baseMessage]
    expect(
      applyReactionToMessages(messages, {
        emoji: '👍',
        requestId: 'req_missing',
        messageId: '',
        sessionId: '',
      }),
    ).toBe(messages)
  })

  it('overwrites an earlier reaction with the newest one', () => {
    const next = applyReactionToMessages(
      [{ ...baseMessage, reaction: '👍' }],
      { emoji: '🔥', requestId: 'req_abc', messageId: '', sessionId: '' },
    )
    expect(next[0].reaction).toBe('🔥')
  })
})

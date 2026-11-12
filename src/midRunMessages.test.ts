import { describe, expect, it } from 'vitest'
import { buildAlphaStreamSegments } from './alphaStreamLayout'
import {
  applySteerDelivery,
  dropSteeredUserDuplicates,
  mergeSteerAnchors,
  normalizeSteerAnchors,
  patchPendingBubble,
  reconcilePendingBubbles,
  removePendingBubble,
  settlePendingBubble,
  type MidRunAwareMessage,
} from './midRunMessages'

const user = (requestId: string, content: string, extra: Partial<MidRunAwareMessage> = {}): MidRunAwareMessage => ({
  id: `u_${requestId}`, role: 'user', content, requestId, ...extra,
})
const assistant = (requestId: string, content: string, extra: Partial<MidRunAwareMessage> = {}): MidRunAwareMessage => ({
  id: `a_${requestId}`, role: 'assistant', content, requestId, ...extra,
})
const pending = (requestId: string, content: string) => user(requestId, content, { midRun: { state: 'sending' } })

describe('mid-run messages in the transcript', () => {
  it('a pending bubble goes from sending to waiting without a label, then fails quietly', () => {
    let messages = [user('a', 'find flights'), assistant('a', 'Checking'), pending('b', 'nonstop only')]
    messages = patchPendingBubble(messages, 'b', { state: 'waiting', acked: true })
    expect(messages[2].midRun).toEqual({ state: 'waiting', acked: true })
    messages = patchPendingBubble(messages, 'b', { state: 'failed', error: 'offline' })
    expect(messages[2].midRun?.state).toBe('failed')
    // Unknown ids are a no-op (same array back).
    expect(patchPendingBubble(messages, 'zzz', { state: 'waiting' })).toBe(messages)
  })

  it('a read steer moves into the reply at the stamped offset, exactly once', () => {
    const before = [user('a', 'find flights'), assistant('a', 'Checking flights. '), pending('b', 'nonstop only')]
    const event = { request_id: 'b', into_request_id: 'a', content: 'nonstop only', stream_offset: 18, delivered_at: '2026-11-11T20:00:00Z' }
    const after = applySteerDelivery(before, event)
    expect(after.map((message) => message.id)).toEqual(['u_a', 'a_a'])
    expect(after[1].steerAnchors).toEqual([
      { requestId: 'b', content: 'nonstop only', offset: 18, deliveredAt: '2026-11-11T20:00:00Z' },
    ])
    // A replayed delivery does not duplicate the anchor.
    expect(applySteerDelivery(after, event)[1].steerAnchors).toHaveLength(1)
  })

  it('a delivery for a reply that is not on screen just settles the bubble in place', () => {
    const after = applySteerDelivery([pending('b', 'nonstop only')], { request_id: 'b', into_request_id: 'gone' })
    expect(after).toEqual([user('b', 'nonstop only')])
  })

  it('a message that runs as its own turn becomes a normal bubble', () => {
    const messages = [user('a', 'q'), assistant('a', 'done'), pending('b', 'next')]
    expect(settlePendingBubble(messages, 'b')[2]).toEqual(user('b', 'next'))
  })

  it('cancel removes only the pending bubble', () => {
    const messages = [user('a', 'q'), pending('b', 'next')]
    expect(removePendingBubble(messages, 'b')).toEqual([user('a', 'q')])
    expect(removePendingBubble(messages, 'a')).toBe(messages)
  })

  it('history shows a steered message once: inline in the reply that read it', () => {
    const anchors = normalizeSteerAnchors([{ request_id: 'b', content: 'nonstop only', stream_offset: 18 }])
    const history = [
      user('a', 'find flights'),
      user('b', 'nonstop only'),
      assistant('a', 'Checking flights. Found two.', { steerAnchors: anchors }),
    ]
    expect(dropSteeredUserDuplicates(history).map((message) => message.id)).toEqual(['u_a', 'a_a'])
    // Without an anchor (older data) the message stays a normal bubble.
    expect(dropSteeredUserDuplicates([user('b', 'x'), assistant('a', 'y')])).toHaveLength(2)
  })

  it('normalizes and merges anchors from the wire', () => {
    expect(normalizeSteerAnchors(null)).toBeUndefined()
    expect(normalizeSteerAnchors([{ request_id: ' b ', stream_offset: 3.7 }, { request_id: 'b', stream_offset: 9 }, { stream_offset: 1 }]))
      .toEqual([{ requestId: 'b', content: '', offset: 3, deliveredAt: null }])
    const first = [{ requestId: 'b', content: 'x', offset: 3 }]
    expect(mergeSteerAnchors(first, [{ requestId: 'b', content: 'x', offset: 99 }, { requestId: 'c', content: 'y', offset: 5 }]))
      .toEqual([...first, { requestId: 'c', content: 'y', offset: 5 }])
  })

  it('reconnect: held messages keep a pending bubble, ones read while away are dropped', () => {
    const messages = [
      user('a', 'q'),
      user('b', 'read while away', { midRun: { state: 'waiting', acked: true } }),
      user('c', 'still held', { midRun: { state: 'waiting', acked: true } }),
      user('d', 'never acked', { midRun: { state: 'sending' } }),
    ]
    const next = reconcilePendingBubbles(
      messages,
      [{ request_id: 'c', content: 'still held' }, { request_id: 'e', content: 'from another window' }],
      (item) => user(item.requestId, item.content, { midRun: { state: 'waiting', acked: true } }),
    )
    expect(next.map((message) => message.requestId)).toEqual(['a', 'c', 'd', 'e'])
    expect(next.find((message) => message.requestId === 'e')?.midRun?.state).toBe('waiting')
  })
})

describe('steer segments in the reply', () => {
  it('splits the reply where the model read the message', () => {
    const { segments, hasAnchors, hasSteers } = buildAlphaStreamSegments({
      content: 'Checking flights. Found two nonstop options.',
      steerAnchors: [{ id: 'b', offset: 18 }],
    })
    expect(hasAnchors).toBe(false)
    expect(hasSteers).toBe(true)
    expect(segments).toEqual([
      { kind: 'content', content: 'Checking flights. ' },
      { kind: 'user_steer', anchorId: 'b' },
      { kind: 'content', content: 'Found two nonstop options.' },
    ])
  })

  it('a steer read at the live end of the reply sits at the end; later text follows it', () => {
    const live = buildAlphaStreamSegments({ content: 'Checking flights. ', steerAnchors: [{ id: 'b', offset: 18 }] })
    expect(live.segments.map((segment) => segment.kind)).toEqual(['content', 'user_steer'])
  })

  it('orders a steer between tool steps at the same offset by time', () => {
    const { segments } = buildAlphaStreamSegments({
      content: 'Done.',
      activityAnchors: [
        { id: 'step1', offset: 0, createdAt: '2026-11-11T20:00:01Z' },
        { id: 'step2', offset: 0, createdAt: '2026-11-11T20:00:05Z' },
      ],
      steerAnchors: [{ id: 'b', offset: 0, deliveredAt: '2026-11-11T20:00:03Z' }],
    })
    expect(segments.map((segment) => (segment.kind === 'activity' || segment.kind === 'user_steer') ? segment.anchorId : segment.kind))
      .toEqual(['step1', 'b', 'step2', 'content'])
  })

  it('leaves replies without steers exactly as before', () => {
    const plain = buildAlphaStreamSegments({ content: 'Hello' })
    expect(plain).toEqual({ hasAnchors: false, hasSteers: false, segments: [{ kind: 'content', content: 'Hello', blocks: undefined }] })
  })
})

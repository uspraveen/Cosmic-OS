import { describe, expect, it } from 'vitest'
import { groupRepliesWithTheirQuery } from './transcriptOrder'

const user = (id: string, requestId?: string | null) => ({ id, role: 'user', requestId: requestId ?? null })
const assistant = (id: string, requestId?: string | null) => ({ id, role: 'assistant', requestId: requestId ?? null })

const ids = (messages: { id: string }[]) => messages.map((message) => message.id)

describe('groupRepliesWithTheirQuery', () => {
  it('keeps an already-ordered transcript unchanged', () => {
    const messages = [
      user('u1', 'req_1'),
      assistant('a1', 'req_1'),
      user('u2', 'req_2'),
      assistant('a2', 'req_2'),
    ]
    expect(ids(groupRepliesWithTheirQuery(messages))).toEqual(['u1', 'a1', 'u2', 'a2'])
  })

  it('moves a heartbeat that completed mid-stream out of the query/answer pair', () => {
    // Persisted history order: the heartbeat finished while the user's own
    // answer was still streaming, so it was stored between the query and the
    // answer. Rendering must not show it "directly under the query label".
    const messages = [
      user('u1', 'req_user'),
      assistant('hb', 'req_heartbeat_42'),
      assistant('a1', 'req_user'),
    ]
    expect(ids(groupRepliesWithTheirQuery(messages))).toEqual(['u1', 'a1', 'hb'])
  })

  it('keeps autonomous deliveries in their own position between exchanges', () => {
    const messages = [
      user('u1', 'req_1'),
      assistant('a1', 'req_1'),
      assistant('hb', 'req_heartbeat_1'),
      user('u2', 'req_2'),
      assistant('a2', 'req_2'),
    ]
    expect(ids(groupRepliesWithTheirQuery(messages))).toEqual(['u1', 'a1', 'hb', 'u2', 'a2'])
  })

  it('collects multiple assistant replies for the same request behind their query', () => {
    const messages = [
      user('u1', 'req_1'),
      assistant('a1_interrupted', 'req_1'),
      assistant('cron', 'req_cron_9'),
      assistant('a1_final', 'req_1'),
    ]
    expect(ids(groupRepliesWithTheirQuery(messages))).toEqual(['u1', 'a1_interrupted', 'a1_final', 'cron'])
  })

  it('does not split an exchange when an autonomous delivery interleaves multiple turns', () => {
    const messages = [
      user('u1', 'req_1'),
      assistant('a1', 'req_1'),
      user('u2', 'req_2'),
      assistant('hb', 'req_heartbeat_7'),
      assistant('a2', 'req_2'),
    ]
    expect(ids(groupRepliesWithTheirQuery(messages))).toEqual(['u1', 'a1', 'u2', 'a2', 'hb'])
  })

  it('leaves messages without request ids (dividers, legacy) in place', () => {
    const messages = [
      assistant('rollover_divider', null),
      user('u1', null),
      assistant('a1', null),
    ]
    expect(ids(groupRepliesWithTheirQuery(messages))).toEqual(['rollover_divider', 'u1', 'a1'])
  })

  it('is idempotent', () => {
    const messages = [
      user('u1', 'req_user'),
      assistant('hb', 'req_heartbeat_42'),
      assistant('a1', 'req_user'),
    ]
    const once = groupRepliesWithTheirQuery(messages)
    expect(groupRepliesWithTheirQuery(once)).toEqual(once)
  })
})

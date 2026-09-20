import { describe, expect, it } from 'vitest'
import { MAX_TURN_GROUP_SIZE, groupAssistantTurns, type GroupableMessage } from './assistantTurnGroups'

const user = (): GroupableMessage => ({ role: 'user' })
const assistant = (awaitingReply = false): GroupableMessage => ({ role: 'assistant', awaitingReply })
const standalone = (): GroupableMessage => ({ role: 'assistant', standalone: true })

/** [groupId, isStart, isTail] per assistant index, for compact assertions. */
const shape = (messages: GroupableMessage[]) => {
  const slots = groupAssistantTurns(messages)
  return [...slots.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([index, slot]) => [index, slot.groupId, slot.isStart, slot.isTail])
}

describe('groupAssistantTurns', () => {
  it('leaves an ordinary exchange as one group per answer', () => {
    expect(shape([user(), assistant(), user(), assistant()])).toEqual([
      [1, 1, true, true],
      [3, 3, true, true],
    ])
  })

  it('groups a card-driven resume, which has no user message between', () => {
    // Vault approval: the gateway's continuation query is a system message and
    // is never persisted, so the two assistant messages sit side by side.
    expect(shape([user(), assistant(true), assistant()])).toEqual([
      [1, 1, true, false],
      [2, 1, false, true],
    ])
  })

  it('groups a prose question the user answered', () => {
    expect(shape([user(), assistant(true), user(), assistant()])).toEqual([
      [1, 1, true, false],
      [3, 1, false, true],
    ])
  })

  it('groups the whole YC shape: one task, three pauses, four messages', () => {
    const slots = groupAssistantTurns([
      user(), // "fill the YC application"
      assistant(true), // vault card
      assistant(true), // browser run, then asks about team + DOB
      user(), // answers
      assistant(true), // three drafts, asks which
      user(), // "give me the options"
      assistant(), // done
    ])
    expect(slots.get(1)?.members).toEqual([1, 2, 4, 6])
    expect(slots.get(1)?.isStart).toBe(true)
    expect(slots.get(6)?.isTail).toBe(true)
    expect([...new Set([1, 2, 4, 6].map((i) => slots.get(i)?.groupId))]).toEqual([1])
  })

  it('starts a new group when the previous turn was not awaiting a reply', () => {
    // A response that merely ends with a question sets no flag. That is the
    // distinction, and the model makes it.
    expect(shape([user(), assistant(false), user(), assistant()])).toEqual([
      [1, 1, true, true],
      [3, 3, true, true],
    ])
  })

  it('exposes exactly one start and one tail per group', () => {
    const slots = groupAssistantTurns([user(), assistant(true), assistant(true), assistant()])
    const members = [1, 2, 3]
    expect(members.filter((i) => slots.get(i)?.isStart)).toEqual([1])
    expect(members.filter((i) => slots.get(i)?.isTail)).toEqual([3])
  })

  it('never lets a heartbeat join or extend a task', () => {
    const slots = groupAssistantTurns([user(), assistant(true), standalone(), assistant()])
    expect(slots.get(2)).toBeUndefined() // proactive message: its own chrome
    expect(slots.get(3)?.groupId).toBe(3) // and it does not bridge the two
    expect(slots.get(1)?.isTail).toBe(true)
  })

  it('caps a runaway chain so real task boundaries stay visible', () => {
    const messages = [user(), ...Array.from({ length: MAX_TURN_GROUP_SIZE + 3 }, () => assistant(true))]
    const slots = groupAssistantTurns(messages)
    expect(slots.get(1)?.members).toHaveLength(MAX_TURN_GROUP_SIZE)
    expect(slots.get(1 + MAX_TURN_GROUP_SIZE)?.isStart).toBe(true)
  })

  it('covers every assistant message exactly once', () => {
    const messages = [user(), assistant(true), assistant(), standalone(), user(), assistant(true), assistant()]
    const slots = groupAssistantTurns(messages)
    const assistantIndexes = messages.flatMap((m, i) => (m.role === 'assistant' ? [i] : []))
    for (const index of assistantIndexes) {
      if (messages[index].standalone) continue
      expect(slots.get(index), `index ${index}`).toBeDefined()
    }
    expect(slots.size).toBe(assistantIndexes.length - 1)
  })

  it('handles an empty transcript and a lone user message', () => {
    expect(groupAssistantTurns([]).size).toBe(0)
    expect(groupAssistantTurns([user()]).size).toBe(0)
  })
})

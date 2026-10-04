/** Adjacent automatic continuations may share chrome. Every user message
 * starts a new visible turn, including answers to an approval or question. */

export interface GroupableMessage {
  role: 'user' | 'assistant'
  /** The turn ended blocked on the user. Set by the model's own tag, or by the
   * orchestrator when the turn ended on a card that parks the work. */
  awaitingReply?: boolean
  /** Proactive Cosmic messages (heartbeat, cron) and anything that arrived
   * from another channel are their own event, never a continuation of the
   * task above them. */
  standalone?: boolean
}

export interface TurnGroupSlot {
  /** Stable id for the group: the index of its first assistant message. */
  groupId: number
  /** Carries the group's Flow and Thinking. */
  isStart: boolean
  /** Carries the group's Sources and its single Copy action. */
  isTail: boolean
  /** Indexes of every assistant message in this group, in order. */
  members: number[]
}

/** A runaway group would hide real task boundaries behind one set of chrome,
 * so a long chain of pauses eventually starts a fresh group. Well above any
 * real exchange (the YC task was four). */
export const MAX_TURN_GROUP_SIZE = 12

export const groupAssistantTurns = (
  messages: readonly GroupableMessage[],
): Map<number, TurnGroupSlot> => {
  const slots = new Map<number, TurnGroupSlot>()
  const groups: number[][] = []
  // The open group, and whether anything since its last assistant message
  // disqualifies the next one from joining it.
  let openGroup: number[] | null = null
  let openAwaiting = false

  messages.forEach((message, index) => {
    if (message.role !== 'assistant') {
      openGroup = null
      openAwaiting = false
      return
    }
    if (message.standalone) {
      // Closes the group without joining it: a heartbeat landing mid-task is
      // not part of the task, and the message after it is not a continuation
      // of something a heartbeat interrupted.
      openGroup = null
      openAwaiting = false
      return
    }
    if (openGroup && openAwaiting && openGroup.length < MAX_TURN_GROUP_SIZE) {
      openGroup.push(index)
    } else {
      openGroup = [index]
      groups.push(openGroup)
    }
    openAwaiting = Boolean(message.awaitingReply)
  })

  for (const members of groups) {
    const groupId = members[0]
    members.forEach((index, position) => {
      slots.set(index, {
        groupId,
        isStart: position === 0,
        isTail: position === members.length - 1,
        members,
      })
    })
  }
  return slots
}

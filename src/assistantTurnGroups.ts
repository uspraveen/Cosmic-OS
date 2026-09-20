/**
 * One task, one set of chrome — even when it paused to ask you something.
 *
 * A task that needs an answer mid-flight ends its turn and resumes in a new
 * assistant message. The transcript is a flat list, so each of those messages
 * drew its own Flow, its own Thinking, its own Sources and its own Copy
 * button: the YC application read as four separate tasks when it was one that
 * stopped to ask three questions.
 *
 * The signal for "this is the same task continuing" already exists end to end.
 * `<awaiting_reply/>` means the model is genuinely blocked (its own prompt
 * says: not for rhetorical questions, not for open-ended suggestions), and the
 * orchestrator now also sets it whenever a turn ends on a card that parks the
 * work — vault, sandbox, browser credentials, slide choice. So:
 *
 *   an assistant message continues the previous one when that one was
 *   awaiting a reply, and nothing but the user's answers sits between them.
 *
 * The two ways a turn resumes both fall out of that one rule. A card-driven
 * resume has *nothing* in between — the gateway's continuation query is a
 * system message that is never persisted to the transcript — so the two
 * assistant messages are adjacent. A prose question has the user's typed
 * answer in between. Neither needs a special case.
 *
 * A response that simply ends with a question the user may or may not pick up
 * sets no flag, so it starts a new group: that is the distinction, and it is
 * the model's to make.
 *
 * Grouping is presentational only. Nothing here changes what is stored, what
 * is sent, or the order messages render in.
 */

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
      // A user message between two assistant messages is the answer the first
      // one was waiting for — it keeps the group open. Anything else cannot
      // appear here (the transcript holds only these two roles).
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

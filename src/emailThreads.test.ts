import { describe, expect, it } from 'vitest'
import { groupEmailThreads, stripReplyPrefixes, type TranscriptUnit } from './emailThreads'

interface Msg {
  id: string
  emailThreadId?: string | null
  emailThreadSubject?: string | null
}

const msg = (id: string): Msg => ({ id })
const mail = (id: string, threadId: string, subject?: string): Msg => ({
  id,
  emailThreadId: threadId,
  emailThreadSubject: subject ?? null,
})

const shape = (units: TranscriptUnit<Msg>[]) =>
  units.map((unit) =>
    unit.kind === 'message' ? unit.message.id : `[${unit.threadId}: ${unit.messages.map((m) => m.id).join(',')}]`,
  )

describe('groupEmailThreads', () => {
  it('leaves a transcript with no email threads completely untouched', () => {
    const messages = [msg('u1'), msg('a1'), msg('u2')]
    expect(shape(groupEmailThreads(messages))).toEqual(['u1', 'a1', 'u2'])
  })

  it('keeps a thread whole when other channels land in the middle', () => {
    // The real failure: Cosmic emails, the user replies 35 minutes later, and
    // Cosmic answers. Mobile traffic arrives in between. A flat splice would
    // scatter the thread; the whole conversation must stay in one unit.
    const messages = [
      mail('email_open', 'thr_spc', 'Vinai just replied'),
      msg('mobile_q'),
      msg('mobile_a'),
      mail('email_reply', 'thr_spc', 'Re: Vinai just replied'),
      mail('email_answer', 'thr_spc', 'Re: Vinai just replied'),
    ]
    expect(shape(groupEmailThreads(messages))).toEqual([
      'mobile_q',
      'mobile_a',
      '[thr_spc: email_open,email_reply,email_answer]',
    ])
  })

  it('anchors the thread at its newest message, never its oldest', () => {
    // Anchoring at the first message would push a brand-new reply up into a
    // card the user already scrolled past.
    const messages = [mail('old', 'thr_1'), msg('later_other_channel'), mail('new', 'thr_1')]
    const units = groupEmailThreads(messages)
    expect(shape(units)).toEqual(['later_other_channel', '[thr_1: old,new]'])
    const thread = units[1]
    expect(thread.kind === 'email-thread' && thread.anchorId).toBe('new')
  })

  it('keeps separate threads separate and independently positioned', () => {
    const messages = [
      mail('a1', 'thr_a'),
      mail('b1', 'thr_b'),
      mail('a2', 'thr_a'),
      msg('desktop'),
      mail('b2', 'thr_b'),
    ]
    expect(shape(groupEmailThreads(messages))).toEqual([
      '[thr_a: a1,a2]',
      'desktop',
      '[thr_b: b1,b2]',
    ])
  })

  it('preserves message order inside a thread', () => {
    const messages = [mail('m1', 't'), mail('m2', 't'), mail('m3', 't')]
    const [unit] = groupEmailThreads(messages)
    expect(unit.kind === 'email-thread' && unit.messages.map((m) => m.id)).toEqual([
      'm1',
      'm2',
      'm3',
    ])
  })

  it('is idempotent over the messages it carries', () => {
    const messages = [mail('m1', 't'), msg('x'), mail('m2', 't')]
    const once = groupEmailThreads(messages)
    const twice = groupEmailThreads(messages)
    expect(shape(twice)).toEqual(shape(once))
  })

  it('treats a single-message thread as a normal one-message unit', () => {
    const units = groupEmailThreads([mail('only', 'thr_solo', 'Hello')])
    expect(units).toHaveLength(1)
    expect(units[0].kind === 'email-thread' && units[0].messages).toHaveLength(1)
  })

  it('ignores blank thread ids rather than lumping them together', () => {
    const messages: Msg[] = [
      { id: 'a', emailThreadId: '' },
      { id: 'b', emailThreadId: '   ' },
      { id: 'c', emailThreadId: null },
    ]
    expect(shape(groupEmailThreads(messages))).toEqual(['a', 'b', 'c'])
  })

  it('labels the card with the subject, without the reply prefixes', () => {
    const units = groupEmailThreads([
      mail('m1', 't', 'Re: Re: Vinai just replied about SPC'),
      mail('m2', 't', 'Re: Vinai just replied about SPC'),
    ])
    expect(units[0].kind === 'email-thread' && units[0].subject).toBe(
      'Vinai just replied about SPC',
    )
  })

  it('falls back to a generic label when no message carries a subject', () => {
    const units = groupEmailThreads([mail('m1', 't')])
    expect(units[0].kind === 'email-thread' && units[0].subject).toBe('Email thread')
  })
})

describe('stripReplyPrefixes', () => {
  it('strips stacked reply and forward prefixes', () => {
    expect(stripReplyPrefixes('Re: Fwd: Re: Lease renewal')).toBe('Lease renewal')
    expect(stripReplyPrefixes('RE: FW: Quarterly numbers')).toBe('Quarterly numbers')
    expect(stripReplyPrefixes('Re[2]: Counted prefix')).toBe('Counted prefix')
  })

  it('leaves an ordinary subject alone', () => {
    expect(stripReplyPrefixes('Reminder: DMV appointment')).toBe('Reminder: DMV appointment')
    expect(stripReplyPrefixes('Research summary')).toBe('Research summary')
  })

  it('keeps the original when a subject is nothing but prefixes', () => {
    expect(stripReplyPrefixes('Re:')).toBe('Re:')
  })
})

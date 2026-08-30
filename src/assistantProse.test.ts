import { describe, expect, it } from 'vitest'
import { scrubAssistantProse } from './assistantProse'

describe('scrubAssistantProse', () => {
  it('strips a trailing status checkmark after a sentence', () => {
    expect(scrubAssistantProse('Draft sent. ✅')).toBe('Draft sent.')
    expect(scrubAssistantProse('Draft sent. ✅\nReady when you are.')).toBe(
      'Draft sent.\nReady when you are.',
    )
  })

  it('strips decorative emoji used as bullets or emphasis', () => {
    expect(scrubAssistantProse('✨ Inbox is clear')).toBe('Inbox is clear')
    expect(scrubAssistantProse('- Visa follow-up ✅')).toBe('- Visa follow-up')
    expect(scrubAssistantProse('Launch the site 🚀 tomorrow')).toBe('Launch the site tomorrow')
  })

  it('leaves real words and punctuation intact', () => {
    expect(scrubAssistantProse("I'll take the draft first.")).toBe("I'll take the draft first.")
  })

  it('leaves fenced code unchanged', () => {
    expect(scrubAssistantProse('Intro\n```\nstatus = "✅"\n```\nOut')).toBe(
      'Intro\n```\nstatus = "✅"\n```\nOut',
    )
  })
})

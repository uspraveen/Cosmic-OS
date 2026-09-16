import { describe, expect, it } from 'vitest'
import {
  normalizeInterruptCommit,
  normalizeInterruptOptions,
  presentBrowserInterrupt,
} from './browserInterrupt'

const ESSAY = (
  "I've reached a sign-in page (https://thewatersatchenal.petscreening.com/welcome/check_email?"
  + 'token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa) '
  + 'that needs your password. Please enter your credentials and complete sign-in, then reply '
  + "'done' so I can continue. The account username is uspraveenraj@gmail.com. It asked to create "
  + 'a password, generate a strong password and remember to report it back so it can be saved in '
  + 'the password vault. The resident has NO pets and NO assistance animals.'
)

describe('presentBrowserInterrupt', () => {
  it('does not use the raw essay as the title', () => {
    const presented = presentBrowserInterrupt(ESSAY, 'password', 'https://thewatersatchenal.petscreening.com/welcome')
    expect(presented.title).toBe('Sign in')
    expect(presented.title.length).toBeLessThan(80)
    expect(presented.site).toBe('thewatersatchenal.petscreening.com')
    expect(presented.username).toBe('uspraveenraj@gmail.com')
    expect(presented.summary).toBeNull()
    expect(presented.leftover).toBeNull()
    expect(JSON.stringify(presented)).not.toMatch(/eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9/)
    expect(presented.primaryAction).toBe('Continue')
  })

  it('falls back to a URL inside the question when the page url is missing', () => {
    const presented = presentBrowserInterrupt(
      'Open https://accounts.google.com/signin and enter the code.',
      'verification_code',
    )
    expect(presented.site).toBe('accounts.google.com')
    expect(presented.title).toBe('Verification code')
  })

  it('keeps a short generic question as the title', () => {
    const presented = presentBrowserInterrupt('Which plan should I pick?', 'generic')
    expect(presented.title).toBe('Which plan should I pick?')
    expect(presented.leftover).toBeNull()
  })
})

describe('normalizeInterruptOptions', () => {
  it('trims, dedupes, and drops junk values', () => {
    const options = normalizeInterruptOptions([
      '  13500 Chenal Pkwy, Apt 2309  ',
      '13500 chenal pkwy, apt 2309',
      42,
      '',
      '   ',
      'No pets',
    ])
    expect(options).toEqual(['13500 Chenal Pkwy, Apt 2309', 'No pets'])
  })

  it('ignores non-arrays and caps the list at six', () => {
    expect(normalizeInterruptOptions(null)).toEqual([])
    expect(normalizeInterruptOptions('not-a-list')).toEqual([])
    expect(normalizeInterruptOptions(['1', '2', '3', '4', '5', '6', '7'])).toHaveLength(6)
  })

  it('drops options long enough to be an essay', () => {
    expect(normalizeInterruptOptions(['x'.repeat(300)])).toEqual([])
  })
})

describe('normalizeInterruptCommit', () => {
  it('keeps the target, fields, and the irreversible flag', () => {
    const commit = normalizeInterruptCommit({
      target: 'Delete account',
      url: 'https://example.com/settings',
      irreversible: true,
      action_class: 'delete',
      control: { matched: ['delete'] },
      fields: [
        { label: 'Account', value: 'uspraveenraj@gmail.com' },
        { label: 'Reason', value: 'moving out' },
      ],
    })
    expect(commit).not.toBeNull()
    expect(commit?.target).toBe('Delete account')
    expect(commit?.irreversible).toBe(true)
    expect(commit?.actionClass).toBe('delete')
    expect(commit?.fields).toEqual([
      { label: 'Account', value: 'uspraveenraj@gmail.com' },
      { label: 'Reason', value: 'moving out' },
    ])
  })

  it('falls back to the control name and defaults the class', () => {
    const commit = normalizeInterruptCommit({ control: { name: 'Submit application' } })
    expect(commit?.target).toBe('Submit application')
    expect(commit?.actionClass).toBe('submit')
    expect(commit?.fields).toEqual([])
  })

  it('returns null for junk and caps field lists', () => {
    expect(normalizeInterruptCommit(null)).toBeNull()
    expect(normalizeInterruptCommit('nope')).toBeNull()
    const many = Array.from({ length: 40 }, (_, index) => ({
      label: `Field ${index}`,
      value: `Value ${index}`,
    }))
    expect(normalizeInterruptCommit({ target: 'x', fields: many })?.fields).toHaveLength(20)
  })
})

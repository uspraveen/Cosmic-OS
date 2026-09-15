import { describe, expect, it } from 'vitest'
import { presentBrowserInterrupt } from './browserInterrupt'

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

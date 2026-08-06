import { describe, expect, it } from 'vitest'
import {
  describeLoginReason,
  extractLoginUrl,
  loginSessionLines,
  loginStartOutcome,
  stripAnsi,
  type AgentLoginStatus,
} from './agentLogin'

const ESC = String.fromCharCode(27)

describe('loginStartOutcome', () => {
  it('reports failure when the VM says the CLI is missing', () => {
    // Verbatim from the live gateway while the Login button appeared to work.
    const status: AgentLoginStatus = {
      status: 'relogin_required',
      login_required_reason: 'cursor_cli_missing',
      cli: { available: false, authenticated: false, reason: 'cursor_cli_missing' },
      login_session: null,
    }
    const outcome = loginStartOutcome(status, 'Cursor')
    expect(outcome.ok).toBe(false)
    expect(outcome.message).toContain('not installed on the VM')
  })

  it('reports success only once the CLI is actually waiting on the browser', () => {
    const outcome = loginStartOutcome({ status: 'login_pending' }, 'Cursor')
    expect(outcome.ok).toBe(true)
    expect(outcome.message).toContain('browser')
  })

  it('treats an already-signed-in CLI as a success, and says which', () => {
    const outcome = loginStartOutcome({ status: 'authenticated' }, 'Cursor')
    expect(outcome.ok).toBe(true)
    expect(outcome.message).toContain('already signed in')
  })

  it('never claims success for an empty or malformed response', () => {
    expect(loginStartOutcome(null, 'Cursor').ok).toBe(false)
    expect(loginStartOutcome(undefined, 'Cursor').ok).toBe(false)
    expect(loginStartOutcome({}, 'Cursor').ok).toBe(false)
  })

  it('passes through an unrecognised reason rather than swallowing it', () => {
    const outcome = loginStartOutcome(
      { status: 'relogin_required', login_required_reason: 'some_new_reason' },
      'Codex',
    )
    expect(outcome.ok).toBe(false)
    expect(outcome.message).toContain('some_new_reason')
  })

  it('names the provider it is talking about', () => {
    expect(loginStartOutcome({ status: 'login_pending' }, 'Codex').message).toContain('Codex')
  })
})

describe('describeLoginReason', () => {
  it('falls back to the cli availability flag when no reason is given', () => {
    expect(describeLoginReason({ cli: { available: false } })).toContain('not available on the VM')
  })

  it('is empty when nothing is wrong', () => {
    expect(describeLoginReason({ status: 'authenticated' })).toBe('')
  })
})

describe('extractLoginUrl', () => {
  it('finds the sign-in URL in decorated CLI output', () => {
    const lines = [
      `${ESC}[36mTo sign in, open this page in your browser:${ESC}[0m`,
      `${ESC}[4mhttps://cursor.com/loginDeepControl?challenge=abc123&uuid=def${ESC}[0m`,
      'Waiting for approval...',
    ]
    expect(extractLoginUrl(lines)).toBe(
      'https://cursor.com/loginDeepControl?challenge=abc123&uuid=def',
    )
  })

  it('trims the box-drawing and punctuation a TUI puts after a URL', () => {
    expect(extractLoginUrl(['  https://cursor.com/login?x=1 |'])).toBe('https://cursor.com/login?x=1')
    expect(extractLoginUrl(['Visit https://cursor.com/login.'])).toBe('https://cursor.com/login')
  })

  it('returns empty when the CLI has not printed a URL yet', () => {
    expect(extractLoginUrl(['Starting login...', 'Waiting'])).toBe('')
    expect(extractLoginUrl([])).toBe('')
  })

  it('takes the first URL, which is the one the CLI is waiting on', () => {
    const lines = ['https://cursor.com/login?a=1', 'Docs: https://docs.cursor.com']
    expect(extractLoginUrl(lines)).toBe('https://cursor.com/login?a=1')
  })
})

describe('stripAnsi', () => {
  it('removes colour codes without touching the text', () => {
    expect(stripAnsi(`${ESC}[32mLogged in${ESC}[0m as a@b.c`)).toBe('Logged in as a@b.c')
  })

  it('leaves ordinary bracketed text alone', () => {
    expect(stripAnsi('Run [cursor-agent status] to check')).toBe('Run [cursor-agent status] to check')
  })

  it('is reusable - the global regexes must not carry lastIndex between calls', () => {
    const line = `${ESC}[31mred${ESC}[0m`
    expect(stripAnsi(line)).toBe('red')
    expect(stripAnsi(line)).toBe('red')
    expect(stripAnsi(line)).toBe('red')
  })
})

describe('loginSessionLines', () => {
  it('merges both streams and tolerates a missing session', () => {
    expect(loginSessionLines({ stdout: ['a'], stderr: ['b'] })).toEqual(['a', 'b'])
    expect(loginSessionLines(null)).toEqual([])
    expect(loginSessionLines(undefined)).toEqual([])
  })
})

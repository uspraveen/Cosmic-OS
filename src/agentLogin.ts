// Reading a CLI-agent login status honestly.
//
// The Cursor and Codex panels used to set a green "login started on the VM"
// banner the moment the request resolved, without looking at what came back.
// On a VM where the gateway could not find `cursor-agent`, the endpoint
// answered `{status: relogin_required, login_required_reason: cursor_cli_missing}`
// and the panel still said the login had started. Clicking Login did nothing,
// forever, and the panel insisted it had worked.
//
// The gateway names the fault precisely in `login_required_reason`. Everything
// here is about not throwing that away.

export interface AgentLoginSession {
  session_id?: string
  state?: string
  stdout?: string[]
  stderr?: string[]
}

export interface AgentLoginStatus {
  status?: string
  login_required_reason?: string
  cli?: { available?: boolean; authenticated?: boolean; reason?: string }
  login_session?: AgentLoginSession | null
}

/** Reasons that mean "this will never work until someone fixes the VM". */
const REASON_MESSAGES: Record<string, string> = {
  cursor_cli_missing:
    'The Cursor CLI is not installed on the VM, so there is nothing to sign in to. Re-run the VM bootstrap to install cursor-agent.',
  codex_cli_missing:
    'The Codex CLI is not installed on the VM, so there is nothing to sign in to. Re-run the VM bootstrap to install codex.',
  cursor_oauth_login_failed: 'The Cursor sign-in on the VM exited before it completed.',
  chatgpt_login_failed: 'The ChatGPT sign-in on the VM exited before it completed.',
  api_key_relogin_required: 'The saved API key was rejected. Save a new key or switch to browser sign-in.',
  auth_not_configured: 'No sign-in method is configured for this agent yet.',
}

export const describeLoginReason = (status: AgentLoginStatus | null | undefined): string => {
  const reason = String(status?.login_required_reason || '').trim()
  if (reason && REASON_MESSAGES[reason]) return REASON_MESSAGES[reason]
  if (status?.cli?.available === false) {
    return 'The agent CLI is not available on the VM.'
  }
  return reason ? `The VM reported: ${reason}` : ''
}

/**
 * Did clicking Login actually start something?
 *
 * `login_pending` is the normal answer: the CLI is running and waiting for
 * browser approval. `authenticated` means the CLI turned out to be signed in
 * already, which is a success worth reporting as such. Anything else is a
 * refusal, and the panel has to say so rather than congratulate the user.
 */
export const loginStartOutcome = (
  status: AgentLoginStatus | null | undefined,
  label: string,
): { ok: boolean; message: string } => {
  const state = String(status?.status || '').trim()
  if (state === 'login_pending') {
    return { ok: true, message: `${label} sign-in started on the VM. Finish it in your browser.` }
  }
  if (state === 'authenticated') {
    return { ok: true, message: `${label} is already signed in on the VM.` }
  }
  const detail = describeLoginReason(status)
  return {
    ok: false,
    message: detail
      ? `${label} sign-in could not start. ${detail}`
      : `${label} sign-in could not start on the VM.`,
  }
}

// Built with fromCharCode so this file never has to hold a raw ESC byte to be
// correct - a control character in source survives no round trip reliably.
const ESC = String.fromCharCode(27)
const BEL = String.fromCharCode(7)
const ANSI_CSI = new RegExp(ESC + '\\[[0-9;?]*[A-Za-z]', 'g')
const ANSI_OSC = new RegExp(ESC + '\\][^' + BEL + ']*' + BEL, 'g')
// The CSI form whose ESC byte was already lost in transport. The Codex panel
// needed this, so it is not hypothetical. Narrow on purpose so ordinary
// bracketed text survives.
const ANSI_BARE_COLOR = /\[[0-9;]*m/g

/** Strip ANSI decoration from a line of CLI output. */
export const stripAnsi = (value: string): string =>
  value.replace(ANSI_CSI, '').replace(ANSI_OSC, '').replace(ANSI_BARE_COLOR, '')

/**
 * The sign-in URL a CLI printed, if it has printed one yet.
 *
 * CLI login output is decorated: ANSI colour, box-drawing, a trailing spinner.
 * Take the first http(s) URL and trim the punctuation and box glyphs that tend
 * to ride along at the end of a line.
 */
export const extractLoginUrl = (lines: readonly string[]): string => {
  for (const line of lines) {
    const match = stripAnsi(String(line || '')).match(/https?:\/\/[^\s"'<>]+/)
    if (!match) continue
    const url = match[0].replace(/[),.;:\]}|│┃╎┆'"]+$/, '')
    if (url.length > 'https://a'.length) return url
  }
  return ''
}

export const loginSessionLines = (session: AgentLoginSession | null | undefined): string[] => [
  ...(session?.stdout || []),
  ...(session?.stderr || []),
]

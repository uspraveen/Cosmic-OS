import {
  GOOGLE_TOOL_DEFINITIONS,
  accountNeedsReconnect,
  normalizeIntegrationAccount,
  scopesMatch,
  type IntegrationAccountRecord,
  type IntegrationsSnapshot,
} from './integrations'
import type { SettingsView } from './Settings'

export const AUTH_ATTENTION_PREFS_KEY = 'cosmic.authAttentionPrefs.v1'
export const AUTH_ATTENTION_REMINDER_INTERVAL_MS = 2 * 60 * 60 * 1000
export const AUTH_ATTENTION_SNOOZE_MS = 2 * 60 * 60 * 1000

export type AuthAttentionProvider = 'google' | 'codex' | 'cursor' | 'opencode' | 'zcode'

export interface AuthAttentionItem {
  key: string
  provider: AuthAttentionProvider
  accountId: string
  accountLabel: string
  email: string
  title: string
  message: string
  detail: string
  settingsView: SettingsView
}

export interface AgentGatewayStatus {
  status?: string | null
  login_required_reason?: string | null
  auth_mode?: string | null
  has_api_key?: boolean
  cli?: {
    available?: boolean
    authenticated?: boolean
    reason?: string | null
  } | null
}

export interface AuthAttentionPrefs {
  snoozedUntilByKey: Record<string, number>
  neverNotifyByKey: Record<string, boolean>
  lastNotifiedAtByKey: Record<string, number>
}

export const EMPTY_AUTH_ATTENTION_PREFS: AuthAttentionPrefs = {
  snoozedUntilByKey: {},
  neverNotifyByKey: {},
  lastNotifiedAtByKey: {},
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? value as Record<string, unknown> : {}
}

export function loadAuthAttentionPrefs(): AuthAttentionPrefs {
  if (typeof window === 'undefined') return EMPTY_AUTH_ATTENTION_PREFS
  try {
    const raw = window.localStorage.getItem(AUTH_ATTENTION_PREFS_KEY)
    if (!raw) return EMPTY_AUTH_ATTENTION_PREFS
    const parsed = asRecord(JSON.parse(raw))
    return {
      snoozedUntilByKey: asRecord(parsed.snoozedUntilByKey) as Record<string, number>,
      neverNotifyByKey: asRecord(parsed.neverNotifyByKey) as Record<string, boolean>,
      lastNotifiedAtByKey: asRecord(parsed.lastNotifiedAtByKey) as Record<string, number>,
    }
  } catch {
    return EMPTY_AUTH_ATTENTION_PREFS
  }
}

export function saveAuthAttentionPrefs(prefs: AuthAttentionPrefs) {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(AUTH_ATTENTION_PREFS_KEY, JSON.stringify(prefs))
  } catch {
    // localStorage can be unavailable in constrained test/webview contexts.
  }
}

function accountTitle(account: IntegrationAccountRecord) {
  return account.account_label || account.display_name || account.email || 'Google account'
}

function selectedToolLabels(account: IntegrationAccountRecord) {
  const selected = new Set(account.selected_tools)
  return GOOGLE_TOOL_DEFINITIONS
    .filter((tool) => selected.has(tool.id))
    .map((tool) => tool.label)
}

function getReconnectDetail(account: IntegrationAccountRecord) {
  const authError = String(account.metadata?.last_auth_error || '').trim()
  if (authError) return authError
  if (account.status === 'revoked') return 'This account was disconnected from Cosmic.'
  if (account.status === 'reauth_required') return 'Google access needs to be refreshed.'
  if (account.status === 'degraded') return 'One or more Google-backed tools cannot authenticate.'
  if (account.status !== 'connected') return 'This Google account is not connected.'
  if (!account.metadata?.has_refresh_token) return 'Cosmic is missing the refresh token for this account.'
  if (account.metadata?.scope_match === false || !scopesMatch(account)) {
    return 'Reconnect to grant the selected app permissions.'
  }
  return 'Reconnect this account to restore Google-backed tools.'
}

export function getGoogleAuthAttentionItems(snapshot: IntegrationsSnapshot | null | undefined): AuthAttentionItem[] {
  const googleProvider = snapshot?.providers?.find((provider) => provider.provider === 'google')
  if (!googleProvider) return []

  return (googleProvider.accounts ?? [])
    .map((account) => normalizeIntegrationAccount(account))
    .filter((account) => !account.account_id.startsWith('draft-'))
    .filter((account) => account.selected_tools.length > 0)
    .filter((account) => accountNeedsReconnect(account))
    .map((account) => {
      const title = accountTitle(account)
      const labels = selectedToolLabels(account)
      const toolText = labels.length > 0 ? labels.join(', ') : 'Google tools'
      return {
        key: `google:${account.account_id}`,
        provider: 'google' as const,
        accountId: account.account_id,
        accountLabel: title,
        email: account.email,
        title: `${title} needs reconnect`,
        message: `Reconnect Google to restore ${toolText}.`,
        detail: getReconnectDetail(account),
        settingsView: 'integrations-google',
      }
    })
}

const AGENT_ATTENTION_STATUSES = new Set(['relogin_required', 'login_required'])

function agentStatusNeedsAttention(status: AgentGatewayStatus | null | undefined): boolean {
  const normalized = String(status?.status || '').trim().toLowerCase()
  return AGENT_ATTENTION_STATUSES.has(normalized)
}

function agentAttentionDetail(
  providerLabel: string,
  status: AgentGatewayStatus | null | undefined,
): string {
  const reason = String(status?.login_required_reason || '').trim()
  if (reason === 'codex_cli_missing' || reason === 'cursor_cli_missing' || reason === 'opencode_cli_missing' || reason === 'zcode_cli_missing') {
    return `${providerLabel} CLI is missing on the VM. Open Cosmic Agents settings to reinstall or sign in.`
  }
  if (reason === 'api_key_relogin_required') {
    return 'The saved OpenAI API key is no longer valid. Update it in Cosmic Agents settings.'
  }
  if (reason === 'chatgpt_login_required') {
    return 'ChatGPT sign-in is required for Alpha Codex on the VM.'
  }
  if (reason === 'cursor_oauth_login_required') {
    return 'Cursor OAuth sign-in is required for the Alpha runner on the VM.'
  }
  if (reason === 'zen_api_key_required') {
    return 'Paste an OpenCode Zen API key in Cosmic Agents settings so the default runner can execute.'
  }
  const cliReason = String(status?.cli?.reason || '').trim()
  if (cliReason) return cliReason
  if (status?.status === 'relogin_required') {
    return `${providerLabel} needs to be re-authenticated on the VM before Alpha can run.`
  }
  return `${providerLabel} needs sign-in before Alpha can use it.`
}

export function getCodexAuthAttentionItems(status: AgentGatewayStatus | null | undefined): AuthAttentionItem[] {
  if (!agentStatusNeedsAttention(status)) {
    return []
  }
  const authMode = String(status?.auth_mode || '').trim().toLowerCase()
  const detail = agentAttentionDetail('Alpha Codex', status)
  return [{
    key: 'codex:alpha',
    provider: 'codex',
    accountId: 'codex',
    accountLabel: 'Alpha Codex',
    email: '',
    title: 'Alpha Codex needs re-authentication',
    message: authMode === 'api_key'
      ? 'Update the Codex API key or switch back to ChatGPT sign-in.'
      : 'Sign Codex back in on the VM so Alpha can run again.',
    detail,
    settingsView: 'agents-codex',
  }]
}

export function getCursorAuthAttentionItems(status: AgentGatewayStatus | null | undefined): AuthAttentionItem[] {
  if (!agentStatusNeedsAttention(status)) {
    return []
  }
  return [{
    key: 'cursor:alpha',
    provider: 'cursor',
    accountId: 'cursor',
    accountLabel: 'Alpha Cursor',
    email: '',
    title: 'Alpha Cursor needs re-authentication',
    message: 'Sign Cursor back in on the VM so Alpha can run again.',
    detail: agentAttentionDetail('Alpha Cursor', status),
    settingsView: 'agents-cursor',
  }]
}

export function getOpenCodeAuthAttentionItems(status: AgentGatewayStatus | null | undefined): AuthAttentionItem[] {
  if (!agentStatusNeedsAttention(status)) {
    return []
  }
  const reason = String(status?.login_required_reason || '').trim().toLowerCase()
  const missingCli = reason === 'opencode_cli_missing'
  return [{
    key: 'opencode:alpha',
    provider: 'opencode',
    accountId: 'opencode',
    accountLabel: 'Alpha OpenCode',
    email: '',
    title: missingCli ? 'OpenCode CLI is missing on the VM' : 'Alpha OpenCode needs setup',
    message: missingCli
      ? 'Re-run VM bootstrap so OpenCode installs, or pick another Alpha runner.'
      : 'Paste an OpenCode Zen API key so the default Alpha runner can execute.',
    detail: agentAttentionDetail('Alpha OpenCode', status),
    settingsView: 'agents-opencode',
  }]
}

export function getZcodeAuthAttentionItems(status: AgentGatewayStatus | null | undefined): AuthAttentionItem[] {
  if (!agentStatusNeedsAttention(status)) {
    return []
  }
  const reason = String(status?.login_required_reason || '').trim().toLowerCase()
  const missingCli = reason === 'zcode_cli_missing'
  return [{
    key: 'zcode:alpha',
    provider: 'zcode',
    accountId: 'zcode',
    accountLabel: 'Alpha ZCode',
    email: '',
    title: missingCli ? 'ZCode CLI is missing on the VM' : 'Alpha ZCode needs sign-in',
    message: missingCli
      ? 'Re-run VM bootstrap (setup-zcode-cli) so ZCode installs, or pick another Alpha runner.'
      : 'Connect your Z.ai account so Alpha can run GLM-5.3 through ZCode.',
    detail: agentAttentionDetail('Alpha ZCode', status),
    settingsView: 'agents-zcode',
  }]
}

export function mergeAuthAttentionItems(...groups: AuthAttentionItem[][]): AuthAttentionItem[] {
  const merged: AuthAttentionItem[] = []
  const seen = new Set<string>()
  for (const group of groups) {
    for (const item of group) {
      if (seen.has(item.key)) {
        continue
      }
      seen.add(item.key)
      merged.push(item)
    }
  }
  return merged
}

export function pruneAuthAttentionPrefs(prefs: AuthAttentionPrefs, activeKeys: Set<string>): AuthAttentionPrefs {
  const pruneNumberMap = (source: Record<string, number>) =>
    Object.fromEntries(Object.entries(source).filter(([key]) => activeKeys.has(key)))
  const pruneBooleanMap = (source: Record<string, boolean>) =>
    Object.fromEntries(Object.entries(source).filter(([key]) => activeKeys.has(key)))

  return {
    snoozedUntilByKey: pruneNumberMap(prefs.snoozedUntilByKey),
    neverNotifyByKey: pruneBooleanMap(prefs.neverNotifyByKey),
    lastNotifiedAtByKey: pruneNumberMap(prefs.lastNotifiedAtByKey),
  }
}

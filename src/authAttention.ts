import {
  GOOGLE_TOOL_DEFINITIONS,
  accountNeedsReconnect,
  normalizeIntegrationAccount,
  scopesMatch,
  type IntegrationAccountRecord,
  type IntegrationsSnapshot,
} from './integrations'

export const AUTH_ATTENTION_PREFS_KEY = 'cosmic.authAttentionPrefs.v1'
export const AUTH_ATTENTION_REMINDER_INTERVAL_MS = 60 * 60 * 1000
export const AUTH_ATTENTION_SNOOZE_MS = 6 * 60 * 60 * 1000

export interface AuthAttentionItem {
  key: string
  provider: 'google'
  accountId: string
  accountLabel: string
  email: string
  title: string
  message: string
  detail: string
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
      }
    })
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

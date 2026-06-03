import { useEffect, useMemo, useState } from 'react'
import {
  GOOGLE_TOOL_DEFINITIONS,
  accountNeedsReconnect,
  createGoogleAccountDraft,
  formatRelativeConnection,
  normalizeIntegrationAccount,
  statusLabel,
  toggleGoogleAccountTool,
  type IntegrationAccountRecord,
  type IntegrationEvent,
  type IntegrationsSnapshot,
} from './integrations'

interface GoogleIntegrationsSettingsProps {
  active: boolean
}

const EMPTY_SNAPSHOT: IntegrationsSnapshot = { providers: [] }
const GOOGLE_PROVIDER_PAYLOAD = {
  provider: 'google',
  provider_display_name: 'Google',
} as const

function getAccountTitle(account: IntegrationAccountRecord) {
  return account.account_label || account.display_name || account.email || 'Google account'
}

function getAccountSubtitle(account: IntegrationAccountRecord) {
  if (account.email) return account.email
  if (account.display_name) return account.display_name
  if (account.status === 'connected') return 'Google account connected.'
  return 'Connect to add this account to Cosmic.'
}

function getSelectedToolLabels(account: IntegrationAccountRecord) {
  return GOOGLE_TOOL_DEFINITIONS
    .filter((tool) => account.selected_tools.includes(tool.id))
    .map((tool) => tool.label)
}

export default function GoogleIntegrationsSettings({ active }: GoogleIntegrationsSettingsProps) {
  const [accounts, setAccounts] = useState<IntegrationAccountRecord[]>([])
  const [loading, setLoading] = useState(false)
  const [connectingAccountId, setConnectingAccountId] = useState<string | null>(null)
  const [disconnectingAccountId, setDisconnectingAccountId] = useState<string | null>(null)
  const [deletingAccountId, setDeletingAccountId] = useState<string | null>(null)
  const [banner, setBanner] = useState('')

  useEffect(() => {
    if (!banner) return
    const timer = window.setTimeout(() => setBanner(''), 2800)
    return () => window.clearTimeout(timer)
  }, [banner])

  useEffect(() => {
    if (!active) return
    setLoading(true)

    const offIntegrations = window.cosmic?.onIntegrationsUpdate((snapshot: IntegrationsSnapshot = EMPTY_SNAPSHOT) => {
      const googleProvider = snapshot.providers?.find((provider) => provider.provider === 'google')
      const nextAccounts = (googleProvider?.accounts ?? []).map((account) => normalizeIntegrationAccount(account))
      setAccounts(nextAccounts)
      setLoading(false)

      if (deletingAccountId && !nextAccounts.some((account) => account.account_id === deletingAccountId)) {
        setDeletingAccountId(null)
      }
    })

    const offEvents = window.cosmic?.onIntegrationEvent((event: IntegrationEvent) => {
      if (event.provider !== 'google') return

      if (event.type === 'auth_started') {
        setConnectingAccountId(event.account_id)
      } else if (event.type === 'auth_success' || event.type === 'auth_error') {
        setConnectingAccountId(null)
      } else if (event.type === 'disconnect_started') {
        setDisconnectingAccountId(event.account_id)
      } else if (event.type === 'disconnect_success' || event.type === 'disconnect_error') {
        setDisconnectingAccountId(null)
      } else if (event.type === 'delete_started') {
        setDeletingAccountId(event.account_id)
      } else if (event.type === 'delete_success' || event.type === 'delete_error') {
        setDeletingAccountId(null)
        if (event.type === 'delete_success') {
          const title = event.account_label || event.display_name || event.email || 'Google account'
          window.dispatchEvent(
            new CustomEvent('cosmic:island-notification', {
              detail: {
                type: 'success',
                provider: 'Google',
                title: `${title} removed`,
                message: `${title} was removed from Cosmic.`,
              },
            }),
          )
        }
      }

      if (event.message) {
        setBanner(event.message)
      }
    })

    window.cosmic?.getIntegrations()
    return () => {
      offIntegrations?.()
      offEvents?.()
    }
  }, [active, deletingAccountId])

  const connectedCount = useMemo(
    () => accounts.filter((account) => account.status === 'connected' && account.metadata.has_refresh_token).length,
    [accounts],
  )

  const authBusy = Boolean(connectingAccountId)

  const toGooglePayload = (account: IntegrationAccountRecord) => ({
    ...account,
    ...GOOGLE_PROVIDER_PAYLOAD,
  })

  const updateAccount = (
    accountId: string,
    updater: (account: IntegrationAccountRecord) => IntegrationAccountRecord,
  ): IntegrationAccountRecord | null => {
    const currentAccount = accounts.find((account) => account.account_id === accountId)
    if (!currentAccount) return null

    const nextAccount = normalizeIntegrationAccount(updater(currentAccount))
    setAccounts((prev) => prev.map((account) => (account.account_id === accountId ? nextAccount : account)))
    return nextAccount
  }

  const persistAccount = (account: IntegrationAccountRecord) => {
    window.cosmic?.saveIntegrationAccount(toGooglePayload(account))
  }

  const handleConnectNewAccount = () => {
    if (authBusy) return
    const draft = createGoogleAccountDraft(accounts)
    setAccounts((prev) => [draft, ...prev])
    setConnectingAccountId(draft.account_id)
    window.cosmic?.connectGoogleAccount(toGooglePayload(draft))
  }

  const handleConnectAccount = (account: IntegrationAccountRecord) => {
    if (account.selected_tools.length === 0) {
      setBanner('Select at least one Google app before connecting.')
      return
    }

    setConnectingAccountId(account.account_id)
    window.cosmic?.connectGoogleAccount(toGooglePayload(account))
  }

  const handleDisconnectAccount = (account: IntegrationAccountRecord) => {
    setDisconnectingAccountId(account.account_id)
    window.cosmic?.disconnectGoogleAccount(account.account_id)
  }

  const handleDeleteAccount = (account: IntegrationAccountRecord) => {
    if (account.account_id.startsWith('draft-')) {
      setAccounts((prev) => prev.filter((item) => item.account_id !== account.account_id))
      setBanner('Draft removed.')
      return
    }

    if (account.status === 'connected' && account.metadata.has_refresh_token) {
      setBanner('Disconnect this account before removing it.')
      return
    }

    setDeletingAccountId(account.account_id)
    window.cosmic?.deleteIntegrationAccount(account.account_id)
  }

  const handleSetPrimary = (accountId: string) => {
    const nextAccounts = accounts.map((account) =>
      normalizeIntegrationAccount({
        ...account,
        is_primary: account.account_id === accountId,
      }),
    )
    setAccounts(nextAccounts)
    const primaryAccount = nextAccounts.find((account) => account.account_id === accountId)
    if (primaryAccount && !primaryAccount.account_id.startsWith('draft-')) {
      persistAccount(primaryAccount)
      setBanner('Primary account updated.')
    }
  }

  const handleLabelBlur = (account: IntegrationAccountRecord) => {
    if (account.account_id.startsWith('draft-')) return
    persistAccount(account)
  }

  const handleToggleTool = (account: IntegrationAccountRecord, toolId: string) => {
    const nextAccount = updateAccount(account.account_id, (current) => toggleGoogleAccountTool(current, toolId))
    if (nextAccount && !nextAccount.account_id.startsWith('draft-')) {
      persistAccount(nextAccount)
    }
  }

  return (
    <div className="setting-subpage cosmic-google-page">
      <div className="cosmic-google-hero">
        <div className="cosmic-google-hero-inner">
          <div className="cosmic-google-hero-icon" aria-hidden="true">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4"/>
              <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>
              <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/>
              <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>
            </svg>
          </div>
          <div className="cosmic-google-hero-text">
            <h3>Google</h3>
            <p className="cosmic-google-hero-stat">
              {connectedCount === 0
                ? 'No accounts connected'
                : connectedCount === 1
                  ? '1 account'
                  : `${connectedCount} accounts`}
            </p>
            <p className="cosmic-google-hero-desc">Manage accounts and choose which apps Cosmic can use for each.</p>
          </div>
        </div>
        <button type="button" className="cosmic-google-cta" onClick={handleConnectNewAccount} disabled={authBusy}>
          {authBusy ? 'Connecting…' : 'Add account'}
        </button>
      </div>

      {banner && <div className="cosmic-google-banner" role="status">{banner}</div>}

      {loading ? (
        <div className="cosmic-google-empty">
          <div className="cosmic-google-spinner" aria-hidden="true" />
          <span>Loading accounts…</span>
        </div>
      ) : accounts.length === 0 ? (
        <div className="cosmic-google-empty cosmic-google-empty-invite">
          <div className="cosmic-google-empty-icon" aria-hidden="true">
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><line x1="19" y1="8" x2="19" y2="14"/><line x1="22" y1="11" x2="16" y2="11"/></svg>
          </div>
          <p className="cosmic-google-empty-title">No Google accounts yet</p>
          <p className="cosmic-google-empty-desc">Add an account and choose Calendar, Gmail, Docs, Sheets, or Drive. Cosmic stores provider credentials on your private backend.</p>
        </div>
      ) : (
        <div className="cosmic-google-accounts">
          <p className="cosmic-google-section-label">Accounts</p>
          {accounts.map((account, index) => {
            const selectedTools = getSelectedToolLabels(account)
            const needsReconnect = accountNeedsReconnect(account)
            const canRemove = !(account.status === 'connected' && account.metadata.has_refresh_token)
            const showConnectAction = account.status !== 'connected' || needsReconnect
            const connectionNote = needsReconnect
              ? 'Reconnect to refresh access for the apps turned on below.'
              : selectedTools.length > 0
                ? `${selectedTools.join(', ')} enabled. ${formatRelativeConnection(account.metadata.last_connected_at as number | undefined)}`
                : formatRelativeConnection(account.metadata.last_connected_at as number | undefined)

            return (
              <div
                key={account.account_id}
                className="cosmic-google-card"
                style={{ animationDelay: `${index * 0.04}s` }}
              >
                <div className="cosmic-google-card-header">
                  <div className="cosmic-google-card-profile">
                    <div
                      className={`cosmic-google-avatar ${account.status === 'connected' ? 'connected' : ''}`}
                      aria-hidden="true"
                    >
                      {getAccountTitle(account).charAt(0).toUpperCase() || 'G'}
                    </div>
                    <div className="cosmic-google-card-info">
                      <strong>{getAccountTitle(account)}</strong>
                      <span>{getAccountSubtitle(account)}</span>
                    </div>
                  </div>
                  <div className="cosmic-google-card-badges">
                    {account.is_primary && <span className="cosmic-google-badge primary">Primary</span>}
                    <span className={`cosmic-google-badge status-${account.status}`}>{statusLabel(account.status)}</span>
                  </div>
                </div>

                <div className="cosmic-google-card-body">
                  <div className="cosmic-google-field">
                    <label>Nickname</label>
                    <input
                      className="cosmic-google-input"
                      value={account.account_label}
                      onChange={(e) =>
                        updateAccount(account.account_id, (current) => ({
                          ...current,
                          account_label: e.target.value,
                        }))
                      }
                      onBlur={() => handleLabelBlur(account)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') e.currentTarget.blur()
                      }}
                      placeholder="e.g. Work"
                    />
                  </div>

                  <div className="cosmic-google-apps">
                    <div className="cosmic-google-apps-head">
                      <span className="cosmic-google-apps-label">Apps & tools</span>
                      <span className="cosmic-google-apps-count">{selectedTools.length} of {GOOGLE_TOOL_DEFINITIONS.length}</span>
                    </div>
                    <div className="cosmic-google-app-list" role="group" aria-label="Google apps for this account">
                      {GOOGLE_TOOL_DEFINITIONS.map((tool) => {
                        const selected = account.selected_tools.includes(tool.id)
                        return (
                          <button
                            key={tool.id}
                            type="button"
                            className={`cosmic-google-app-row ${selected ? 'on' : ''}`}
                            onClick={() => handleToggleTool(account, tool.id)}
                          >
                            <span className="cosmic-google-app-title">{tool.label}</span>
                            <span className="cosmic-google-app-sub">{tool.platformLabel}</span>
                            <span className={`cosmic-google-toggle ${selected ? 'on' : ''}`}>
                              <span className="cosmic-google-toggle-thumb" />
                            </span>
                          </button>
                        )
                      })}
                    </div>
                    <p className="cosmic-google-apps-hint">Adding more apps may require reconnecting.</p>
                  </div>
                </div>

                <div className="cosmic-google-card-footer">
                  <p className="cosmic-google-card-note">{connectionNote}</p>
                  <div className="cosmic-google-card-actions">
                    {!account.is_primary && accounts.length > 1 && (
                      <button type="button" className="cosmic-google-action ghost" onClick={() => handleSetPrimary(account.account_id)}>
                        Make primary
                      </button>
                    )}
                    {account.status === 'connected' && (
                      <button
                        type="button"
                        className="cosmic-google-action secondary"
                        onClick={() => handleDisconnectAccount(account)}
                        disabled={disconnectingAccountId === account.account_id}
                      >
                        {disconnectingAccountId === account.account_id ? 'Disconnecting…' : 'Disconnect'}
                      </button>
                    )}
                    {showConnectAction && (
                      <button
                        type="button"
                        className="cosmic-google-action primary"
                        onClick={() => handleConnectAccount(account)}
                        disabled={connectingAccountId === account.account_id || authBusy}
                      >
                        {connectingAccountId === account.account_id ? 'Connecting…' : needsReconnect ? 'Reconnect' : 'Connect'}
                      </button>
                    )}
                    <button
                      type="button"
                      className="cosmic-google-action danger"
                      onClick={() => handleDeleteAccount(account)}
                      disabled={!canRemove || deletingAccountId === account.account_id}
                    >
                      {deletingAccountId === account.account_id ? 'Removing…' : 'Remove'}
                    </button>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

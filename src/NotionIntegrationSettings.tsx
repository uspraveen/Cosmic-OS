import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, ChevronDown, LogIn, RefreshCw, Trash2 } from 'lucide-react'
import { NotionMark } from './brandIcons'

interface NotionIntegrationSettingsProps {
  active: boolean
}

interface NotionAccount {
  account_id: string
  display_name?: string
  email?: string
  account_label?: string
  account_display_label?: string
  status?: string
  avatar_url?: string
  last_auth_error?: string
  _metadata?: Record<string, unknown>
}

interface NotionHealthAccount {
  account_id: string
  workspace?: string
  status: string
  needs_reconnect: boolean
  error: string
}

const accountName = (account: NotionAccount): string =>
  String(
    account.account_display_label ||
      account.display_name ||
      account.account_label ||
      account.email ||
      'Notion workspace',
  ).trim()

const isConnected = (account: NotionAccount): boolean =>
  String(account.status || '').trim() === 'active'

type AccountState = 'healthy' | 'reconnect' | 'unreachable'

const accountState = (account: NotionAccount, health: NotionHealthAccount | undefined): AccountState => {
  if (health?.status === 'reauth_required' || !isConnected(account)) return 'reconnect'
  if (health?.status === 'provider_error') return 'unreachable'
  if (health && health.status === 'healthy') return 'healthy'
  return isConnected(account) ? 'healthy' : 'reconnect'
}

const STATE_LABELS: Record<AccountState, string> = {
  healthy: 'Connected',
  reconnect: 'Reconnect needed',
  unreachable: 'Unreachable',
}

export default function NotionIntegrationSettings({ active }: NotionIntegrationSettingsProps) {
  const [accounts, setAccounts] = useState<NotionAccount[]>([])
  const [health, setHealth] = useState<NotionHealthAccount[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [disconnectingId, setDisconnectingId] = useState('')
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [banner, setBanner] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (!banner) return
    const timer = window.setTimeout(() => setBanner(''), 3000)
    return () => window.clearTimeout(timer)
  }, [banner])

  const refresh = useCallback(async () => {
    setLoading(true)
    setError('')
    // One failure must not blank the panel: allSettled keeps whatever the
    // gateway answered for, and only the failed slice degrades gracefully.
    const [accountsResult, healthResult] = await Promise.allSettled([
      window.cosmic?.getNotionAccounts(),
      window.cosmic?.getNotionAuthHealth(),
    ])
    if (accountsResult.status === 'fulfilled') {
      const payload = accountsResult.value
      setAccounts(Array.isArray(payload?.accounts) ? (payload.accounts as NotionAccount[]) : [])
    } else {
      setError('Unable to load connected Notion workspaces.')
    }
    if (healthResult.status === 'fulfilled') {
      const payload = healthResult.value
      setHealth(Array.isArray(payload?.accounts) ? (payload.accounts as NotionHealthAccount[]) : [])
    } else {
      // Older gateway without the probe route — fall back to the stored
      // account status rather than nagging the user.
      setHealth(null)
    }
    setLoading(false)
  }, [])

  useEffect(() => {
    if (!active) return
    void refresh()
  }, [active, refresh])

  useEffect(() => {
    if (accounts.length === 0) return
    setExpanded((current) => {
      if (current.size > 0) return current
      return new Set(accounts.map((account) => account.account_id))
    })
  }, [accounts])

  const toggleExpanded = (accountId: string) => {
    setExpanded((current) => {
      const next = new Set(current)
      if (next.has(accountId)) {
        next.delete(accountId)
      } else {
        next.add(accountId)
      }
      return next
    })
  }

  const connect = async () => {
    setConnecting(true)
    setError('')
    try {
      const result = await window.cosmic?.connectNotionAccount({})
      // The main process reports the real outcome; never assume success from
      // the call resolving.
      if (result?.success) {
        setBanner('Notion connected.')
        await refresh()
      } else if (result?.error === 'cancelled') {
        // The user cancelled from the island; it already reported that, so the
        // panel reopens quietly instead of flagging an error.
      } else {
        setError(result?.message || 'Notion sign-in did not complete.')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to start Notion sign-in.')
    } finally {
      setConnecting(false)
    }
  }

  const disconnect = async (account: NotionAccount) => {
    setDisconnectingId(account.account_id)
    setError('')
    try {
      await window.cosmic?.disconnectNotionAccount(account.account_id)
      setBanner(`${accountName(account)} disconnected.`)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to disconnect this workspace.')
    } finally {
      setDisconnectingId('')
    }
  }

  const healthByAccount = useMemo(() => {
    const map = new Map<string, NotionHealthAccount>()
    for (const item of health || []) map.set(item.account_id, item)
    return map
  }, [health])

  // The pill states what a live check just proved, not what the database
  // hopes is true. When the probe is unavailable (older gateway), fall back
  // to the stored account status.
  const pill = useMemo(() => {
    if (accounts.length === 0)
      return { className: 'warn', label: 'Setup', title: 'Connect a Notion workspace to begin.' }
    if (!health) {
      return accounts.some(isConnected)
        ? {
            className: 'ready',
            label: 'Ready',
            title: 'Connected (live check unavailable).',
          }
        : { className: 'warn', label: 'Setup', title: 'Connect a Notion workspace to begin.' }
    }
    // A single degraded account must not hide behind a green pill: reconnect
    // beats healthy, provider outage beats both.
    if (health.some((item) => item.status === 'reauth_required')) {
      return {
        className: 'warn',
        label: 'Reconnect',
        title: 'Notion rejected the credential. Reconnect the workspace.',
      }
    }
    if (health.some((item) => item.status !== 'healthy')) {
      return {
        className: 'warn',
        label: 'Unreachable',
        title: 'The gateway could not reach Notion. Check the connection.',
      }
    }
    return {
      className: 'ready',
      label: 'Ready',
      title: 'Verified live: token resolved and confirmed with Notion.',
    }
  }, [accounts, health])

  return (
    <div className="cosmic-agents-detail-page">
      <div className="cosmic-agents-detail-hero">
        <div className="cosmic-agents-detail-hero-top">
          <div className="cosmic-agents-detail-hero-icon" aria-hidden="true">
            <NotionMark size={28} />
          </div>
          <div className="cosmic-agents-detail-hero-text">
            <h3>Notion</h3>
            <p>
              {loading
                ? 'Checking connected workspaces'
                : accounts.length === 0
                  ? 'No Notion workspace connected'
                  : `${accounts.length} workspace${accounts.length === 1 ? '' : 's'} connected`}
            </p>
            <span>Lets Cosmic search, read, and write the pages you share — drafts, docs, and databases.</span>
          </div>
        </div>
        <div className={`cosmic-agents-detail-status-pill ${pill.className}`} title={pill.title}>
          {pill.label}
        </div>
      </div>

      {banner ? (
        <div className="cosmic-agents-detail-banner success" role="status">
          <span className="cosmic-agents-detail-banner-icon">✓</span>
          {banner}
        </div>
      ) : null}
      {error ? (
        <div className="cosmic-agents-detail-banner error" role="alert">
          <span className="cosmic-agents-detail-banner-icon">!</span>
          {error}
        </div>
      ) : null}

      <div className="cosmic-agents-detail-section">
        <div className="cosmic-agents-detail-section-head">
          <div>
            <span className="cosmic-agents-detail-kicker">Accounts</span>
            <h4>Connected Notion workspaces</h4>
          </div>
          <div className="cosmic-agents-detail-actions">
            <button
              type="button"
              className="cosmic-agents-detail-btn ghost icon"
              onClick={refresh}
              disabled={loading || connecting}
              title="Refresh workspaces and re-check access live"
            >
              <RefreshCw size={15} className={loading ? 'spinning' : undefined} />
            </button>
            <button
              type="button"
              className="cosmic-agents-detail-btn"
              onClick={connect}
              disabled={connecting}
            >
              <LogIn size={15} />
              {connecting ? 'Connecting…' : accounts.length ? 'Add workspace' : 'Connect'}
            </button>
          </div>
        </div>

        {accounts.length === 0 ? (
          <p className="cosmic-agents-detail-section-copy">
            Connecting opens Notion in your browser. You pick the workspace and exactly which pages
            Cosmic may see — sharing a parent page includes everything under it, so scope it
            deliberately. Nothing outside that selection is reachable.
          </p>
        ) : (
          <div className="cosmic-agents-detail-acct-list">
            {accounts.map((account) => {
              const isOpen = expanded.has(account.account_id)
              const state = accountState(account, healthByAccount.get(account.account_id))
              const disconnecting = disconnectingId === account.account_id
              return (
                <div
                  key={account.account_id}
                  className={`cosmic-agents-detail-acct ${isOpen ? 'open' : ''} ${state === 'reconnect' ? 'degraded' : ''}`}
                >
                  <div className="cosmic-agents-detail-acct-headrow">
                    <button
                      type="button"
                      className="cosmic-agents-detail-acct-head"
                      onClick={() => toggleExpanded(account.account_id)}
                      aria-expanded={isOpen}
                    >
                      <span className="cosmic-agents-detail-acct-avatar" aria-hidden="true">
                        {account.avatar_url ? (
                          <img src={account.avatar_url} alt="" />
                        ) : (
                          <NotionMark size={20} />
                        )}
                      </span>
                      <span className="cosmic-agents-detail-acct-id">
                        <span className="cosmic-agents-detail-acct-name">
                          {accountName(account)}
                        </span>
                        <span className="cosmic-agents-detail-acct-sub">
                          {account.email || 'Notion connection'}
                        </span>
                      </span>
                      <span
                        className={`cosmic-agents-detail-chip ${
                          state === 'healthy' ? 'accent' : state === 'unreachable' ? '' : 'warn'
                        }`}
                        title={healthByAccount.get(account.account_id)?.error || STATE_LABELS[state]}
                      >
                        {STATE_LABELS[state]}
                      </span>
                      <ChevronDown size={16} className="cosmic-agents-detail-acct-caret" />
                    </button>
                    <button
                      type="button"
                      className="cosmic-agents-detail-acct-trash"
                      onClick={() => disconnect(account)}
                      disabled={disconnecting}
                      title="Disconnect"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>

                  <div className={`cosmic-agents-detail-acct-body ${isOpen ? 'open' : ''}`}>
                    <div className="cosmic-agents-detail-acct-body-clip">
                      <div className="cosmic-agents-detail-acct-panel">
                        {state === 'reconnect' ? (
                          <div className="cosmic-agents-detail-acct-alert">
                            <AlertTriangle size={14} />
                            <span>
                              {healthByAccount.get(account.account_id)?.error ||
                                'Notion rejected this connection.'}{' '}
                              Reconnect to restore access.
                            </span>
                            <button
                              type="button"
                              className="cosmic-agents-detail-btn sm"
                              onClick={connect}
                              disabled={connecting}
                            >
                              <LogIn size={13} />
                              Reconnect
                            </button>
                          </div>
                        ) : null}

                        <div className="cosmic-agents-detail-grant-row">
                          <span className="cosmic-agents-detail-chip accent">Read content</span>
                          <span className="cosmic-agents-detail-chip accent">Update content</span>
                          <span className="cosmic-agents-detail-chip accent">Insert content</span>
                          <small className="cosmic-agents-detail-chip-note">
                            granted at connect, scoped to the pages you shared
                          </small>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>

      <div className="cosmic-agents-detail-footnote">
        <NotionMark size={14} />
        <span>
          Access is exactly the pages you shared at connect — change the selection any time from
          Notion → Settings → Connections, or by sharing more pages. Removing Cosmic there cuts
          access at the source; reconnect here after any change.
        </span>
      </div>
    </div>
  )
}

import { useCallback, useEffect, useState } from 'react'
import { CheckCircle2, Github, LogIn, RefreshCw, Trash2 } from 'lucide-react'

interface GitHubIntegrationSettingsProps {
  active: boolean
}

interface GitHubAccount {
  account_id: string
  display_name?: string
  email?: string
  account_label?: string
  account_display_label?: string
  status?: string
  avatar_url?: string
  has_refresh_token?: boolean
  last_auth_error?: string
  _metadata?: Record<string, unknown>
  github_installation_id?: string
}

const accountName = (account: GitHubAccount): string =>
  String(
    account.account_display_label ||
      account.display_name ||
      account.account_label ||
      account.email ||
      'GitHub account',
  ).trim()

/**
 * A connected account is not the same as a usable one. GitHub App tokens are
 * scoped to the repositories chosen at install time, so "connected" with zero
 * repositories selected looks identical here but can do nothing. The copy says
 * so rather than implying access it may not have.
 */
const isConnected = (account: GitHubAccount): boolean =>
  String(account.status || '').trim() === 'active'

export default function GitHubIntegrationSettings({ active }: GitHubIntegrationSettingsProps) {
  const [accounts, setAccounts] = useState<GitHubAccount[]>([])
  const [loading, setLoading] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [disconnectingId, setDisconnectingId] = useState('')
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
    try {
      const payload = await window.cosmic?.getGitHubAccounts()
      const next = Array.isArray(payload?.accounts) ? payload.accounts : []
      setAccounts(next as GitHubAccount[])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to load GitHub accounts.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!active) return
    void refresh()
  }, [active, refresh])

  const connect = async () => {
    setConnecting(true)
    setError('')
    try {
      const result = await window.cosmic?.connectGitHubAccount({})
      // The main process reports the real outcome; never assume success from
      // the call resolving. A dead connect that shows a green banner is the
      // exact failure mode this codebase has been bitten by before.
      if (result?.success) {
        setBanner('GitHub connected.')
        await refresh()
      } else {
        setError(result?.message || 'GitHub sign-in did not complete.')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to start GitHub sign-in.')
    } finally {
      setConnecting(false)
    }
  }

  const disconnect = async (account: GitHubAccount) => {
    setDisconnectingId(account.account_id)
    setError('')
    try {
      await window.cosmic?.disconnectGitHubAccount(account.account_id)
      setBanner(`${accountName(account)} disconnected.`)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to disconnect this account.')
    } finally {
      setDisconnectingId('')
    }
  }

  return (
    <div className="cosmic-agents-detail-page">
      <div className="cosmic-agents-detail-hero">
        <div className="cosmic-agents-detail-hero-top">
          <div className="cosmic-agents-detail-hero-icon" aria-hidden="true">
            <Github size={28} />
          </div>
          <div className="cosmic-agents-detail-hero-text">
            <h3>GitHub</h3>
            <p>
              {loading
                ? 'Checking connected accounts'
                : accounts.length === 0
                  ? 'No GitHub account connected'
                  : `${accounts.length} account${accounts.length === 1 ? '' : 's'} connected`}
            </p>
            <span>Lets Alpha work in the repositories you choose — clone, commit, push, open PRs.</span>
          </div>
        </div>
        <div
          className={`cosmic-agents-detail-status-pill ${
            accounts.some(isConnected) ? 'ready' : 'warn'
          }`}
        >
          {accounts.some(isConnected) ? 'Ready' : 'Setup'}
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
            <h4>Connected GitHub accounts</h4>
          </div>
          <div className="cosmic-agents-detail-actions">
            <button
              type="button"
              className="cosmic-agents-detail-btn ghost icon"
              onClick={refresh}
              disabled={loading || connecting}
              title="Refresh"
            >
              <RefreshCw size={15} />
            </button>
            <button
              type="button"
              className="cosmic-agents-detail-btn"
              onClick={connect}
              disabled={connecting}
            >
              <LogIn size={15} />
              {connecting ? 'Connecting…' : accounts.length ? 'Add account' : 'Connect'}
            </button>
          </div>
        </div>

        {accounts.length === 0 ? (
          <p className="cosmic-agents-detail-section-copy">
            Connecting opens GitHub in your browser. You choose which repositories Cosmic may
            access — that selection is the boundary Alpha works inside, so pick deliberately.
          </p>
        ) : (
          <div className="cosmic-agents-detail-mode-list">
            {accounts.map((account) => (
              <div key={account.account_id} className="cosmic-agents-detail-mode">
                <div className="cosmic-agents-detail-mode-left">
                  {isConnected(account) ? (
                    <CheckCircle2 size={16} className="cosmic-agents-detail-model-check-icon" />
                  ) : (
                    <span className="cosmic-agents-detail-mode-dot" aria-hidden="true" />
                  )}
                  <span>{accountName(account)}</span>
                </div>
                <div className="cosmic-agents-detail-actions">
                  <small>
                    {isConnected(account)
                      ? account.has_refresh_token
                        ? 'Connected'
                        : 'Connected · no refresh token'
                      : account.last_auth_error || 'Needs reconnect'}
                  </small>
                  <button
                    type="button"
                    className="cosmic-agents-detail-btn danger icon"
                    onClick={() => disconnect(account)}
                    disabled={Boolean(disconnectingId)}
                    title="Disconnect"
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="cosmic-agents-detail-footnote">
        <Github size={14} />
        <span>
          Repository access is granted per repository when you install, not by this connection.
          Change it any time from the app's page on GitHub.
        </span>
      </div>
    </div>
  )
}

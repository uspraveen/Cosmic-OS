import { useCallback, useEffect, useMemo, useState } from 'react'
import { CheckCircle2, ExternalLink, Github, LogIn, RefreshCw, Trash2 } from 'lucide-react'

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

interface GitHubRepository {
  repo_row_id: string
  account_id?: string
  full_name: string
  html_url?: string
  default_branch?: string
  private: boolean
  can_push: boolean
  status: string
  sync_error?: string
  local_path?: string
  branch?: string
}

interface GitHubHealthAccount {
  account_id: string
  login?: string
  status: string
  needs_reconnect: boolean
  error: string
}

const accountName = (account: GitHubAccount): string =>
  String(
    account.account_display_label ||
      account.display_name ||
      account.account_label ||
      account.email ||
      'GitHub account',
  ).trim()

const accountLogin = (account: GitHubAccount): string =>
  String((account._metadata?.github_login as string | undefined) || '').trim()

const accountPermissions = (account: GitHubAccount): Record<string, unknown> => {
  const perms = account._metadata?.github_permissions
  return perms && typeof perms === 'object' ? (perms as Record<string, unknown>) : {}
}

const lastSyncedAt = (accounts: GitHubAccount[]): number => {
  for (const account of accounts) {
    const syncedAt = Number(
      (account._metadata?.github_repos_synced_at as number | undefined) || 0,
    )
    if (syncedAt > 0) return syncedAt
  }
  return 0
}

/**
 * A connected account is not the same as a usable one. GitHub App tokens are
 * scoped to the repositories chosen at install time, so "connected" with zero
 * repositories selected looks identical here but can do nothing. The copy says
 * so rather than implying access it may not have.
 */
const isConnected = (account: GitHubAccount): boolean =>
  String(account.status || '').trim() === 'active'

// GitHub App permissions are grant scopes, not OAuth scope strings. Only the
// ones a user would reason about are worth a chip; `metadata: read` is
// plumbing every installation carries.
const PERMISSION_ORDER = ['contents', 'pull_requests', 'workflows', 'issues', 'administration']
const PERMISSION_LABELS: Record<string, string> = {
  contents: 'Code',
  pull_requests: 'Pull requests',
  workflows: 'Workflows',
  issues: 'Issues',
  administration: 'Repo admin',
}

const permissionChips = (permissions: Record<string, unknown>): string[] =>
  Object.entries(permissions)
    .filter(([key]) => key !== 'metadata')
    .sort(([a], [b]) => {
      const ai = PERMISSION_ORDER.indexOf(a)
      const bi = PERMISSION_ORDER.indexOf(b)
      return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi)
    })
    .map(([key, value]) => {
      const label = PERMISSION_LABELS[key] || key
      const access = value === 'write' ? 'read/write' : String(value)
      return `${label}: ${access}`
    })

const timeAgo = (epochSeconds: number): string => {
  const elapsed = Math.max(0, Date.now() / 1000 - epochSeconds)
  if (elapsed < 90) return 'just now'
  if (elapsed < 3600) return `${Math.round(elapsed / 60)}m ago`
  if (elapsed < 86400) return `${Math.round(elapsed / 3600)}h ago`
  return `${Math.round(elapsed / 86400)}d ago`
}

export default function GitHubIntegrationSettings({ active }: GitHubIntegrationSettingsProps) {
  const [accounts, setAccounts] = useState<GitHubAccount[]>([])
  const [repositories, setRepositories] = useState<GitHubRepository[]>([])
  const [health, setHealth] = useState<GitHubHealthAccount[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [syncing, setSyncing] = useState(false)
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
    // One failure must not blank the panel: allSettled keeps whatever the
    // gateway answered for, and only the failed slice degrades gracefully.
    const [accountsResult, reposResult, healthResult] = await Promise.allSettled([
      window.cosmic?.getGitHubAccounts(),
      window.cosmic?.getGitHubRepositories(),
      window.cosmic?.getGitHubAuthHealth(),
    ])
    if (accountsResult.status === 'fulfilled') {
      const payload = accountsResult.value
      setAccounts(Array.isArray(payload?.accounts) ? (payload.accounts as GitHubAccount[]) : [])
    } else {
      setError('Unable to load connected GitHub accounts.')
    }
    if (reposResult.status === 'fulfilled') {
      const payload = reposResult.value
      setRepositories(Array.isArray(payload?.repositories) ? (payload.repositories as GitHubRepository[]) : [])
    }
    if (healthResult.status === 'fulfilled') {
      const payload = healthResult.value
      setHealth(Array.isArray(payload?.accounts) ? (payload.accounts as GitHubHealthAccount[]) : [])
    } else {
      // Older gateway without the probe route — fall back to the stored
      // account status for the pill rather than nagging the user.
      setHealth(null)
    }
    setLoading(false)
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

  const syncRepositories = async () => {
    setSyncing(true)
    setError('')
    try {
      const result = await window.cosmic?.syncGitHubRepositories()
      if (result?.sync && result.sync.synced === false) {
        setError(
          result.sync.error ||
            'GitHub would not refresh the repository list. Try reconnecting the account.',
        )
      } else {
        setRepositories(Array.isArray(result?.repositories) ? (result.repositories as GitHubRepository[]) : [])
        setBanner('Repository list verified with GitHub.')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to refresh repositories.')
    } finally {
      setSyncing(false)
    }
  }

  const openOnGitHub = async (url?: string) => {
    if (!url) return
    try {
      await window.cosmic?.openExternal(url)
    } catch {
      // A dead browser is not worth an error banner on top of a settings page.
    }
  }

  const healthByAccount = useMemo(() => {
    const map = new Map<string, GitHubHealthAccount>()
    for (const item of health || []) map.set(item.account_id, item)
    return map
  }, [health])

  // The pill states what a live check just proved, not what the database
  // hopes is true. When the probe is unavailable (older gateway), fall back
  // to the stored account status.
  const pill = useMemo(() => {
    if (accounts.length === 0) return { className: 'warn', label: 'Setup', title: 'Connect a GitHub account to begin.' }
    if (!health) {
      return accounts.some(isConnected)
        ? { className: 'ready', label: 'Ready', title: 'Connected (live check unavailable).'
          }
        : { className: 'warn', label: 'Setup', title: 'Connect a GitHub account to begin.' }
    }
    const anyHealthy = health.some((item) => item.status === 'healthy')
    if (anyHealthy) {
      const degraded = health.find((item) => item.status !== 'healthy')
      return {
        className: 'ready',
        label: 'Ready',
        title: degraded
          ? 'Verified live. Another connected account needs attention below.'
          : 'Verified live: token resolved and confirmed with GitHub.',
      }
    }
    if (health.some((item) => item.status === 'reauth_required')) {
      return { className: 'warn', label: 'Reconnect', title: 'GitHub rejected the credential. Reconnect the account.' }
    }
    return { className: 'warn', label: 'Unreachable', title: 'The gateway could not reach GitHub. Check the connection.' }
  }, [accounts, health])

  const activeRepos = repositories.filter((repo) => repo.status === 'active')
  const removedRepos = repositories.filter((repo) => repo.status !== 'active')
  const syncedAt = lastSyncedAt(accounts)
  const permissionLabels = useMemo(() => {
    for (const account of accounts) {
      if (isConnected(account)) {
        const chips = permissionChips(accountPermissions(account))
        if (chips.length) return chips
      }
    }
    return []
  }, [accounts])

  const manageInstallationsUrl = useMemo(() => {
    for (const account of accounts) {
      const installationId = String(
        (account._metadata?.github_installation_id as string | undefined) || '',
      ).trim()
      if (installationId) return `https://github.com/settings/installations/${installationId}`
    }
    return 'https://github.com/settings/installations'
  }, [accounts])

  const accountStatusText = (account: GitHubAccount): string => {
    const probe = healthByAccount.get(account.account_id)
    if (probe?.status === 'reauth_required') return probe.error || 'Needs reconnect'
    if (probe?.status === 'provider_error') return 'Health check failed'
    if (isConnected(account)) {
      return account.has_refresh_token ? 'Connected' : 'Connected · no refresh token'
    }
    return account.last_auth_error || 'Needs reconnect'
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
            <h4>Connected GitHub accounts</h4>
          </div>
          <div className="cosmic-agents-detail-actions">
            <button
              type="button"
              className="cosmic-agents-detail-btn ghost icon"
              onClick={refresh}
              disabled={loading || connecting}
              title="Refresh and re-check access live"
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
            {accounts.map((account) => {
              const login = accountLogin(account)
              return (
                <div key={account.account_id} className="cosmic-agents-detail-mode">
                  <div className="cosmic-agents-detail-mode-left">
                    {isConnected(account) ? (
                      <CheckCircle2 size={16} className="cosmic-agents-detail-model-check-icon" />
                    ) : (
                      <span className="cosmic-agents-detail-mode-dot" aria-hidden="true" />
                    )}
                    <span className="cosmic-agents-detail-mode-stack">
                      <span>{accountName(account)}</span>
                      {login ? <small>Connected as {login}</small> : null}
                    </span>
                  </div>
                  <div className="cosmic-agents-detail-actions">
                    <small>{accountStatusText(account)}</small>
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
              )
            })}
          </div>
        )}
      </div>

      <div className="cosmic-agents-detail-section">
        <div className="cosmic-agents-detail-section-head">
          <div>
            <span className="cosmic-agents-detail-kicker">Repository access</span>
            <h4>Connected repositories</h4>
            <small className="cosmic-agents-detail-section-sub">
              {loading
                ? 'Checking the installation…'
                : `${activeRepos.length} repositor${activeRepos.length === 1 ? 'y' : 'ies'} available${
                    syncedAt ? ` · synced ${timeAgo(syncedAt)}` : ''
                  }`}
            </small>
          </div>
          <div className="cosmic-agents-detail-actions">
            <button
              type="button"
              className="cosmic-agents-detail-btn ghost icon"
              onClick={syncRepositories}
              disabled={syncing || loading || accounts.length === 0}
              title="Re-check the repository selection with GitHub"
            >
              <RefreshCw size={15} className={syncing ? 'spinning' : undefined} />
            </button>
            <button
              type="button"
              className="cosmic-agents-detail-btn ghost"
              onClick={() => openOnGitHub(manageInstallationsUrl)}
              title="Change which repositories Cosmic may access"
            >
              <ExternalLink size={14} />
              Manage on GitHub
            </button>
          </div>
        </div>

        {permissionLabels.length ? (
          <div className="cosmic-agents-detail-chip-row">
            {permissionLabels.map((label) => (
              <span key={label} className="cosmic-agents-detail-chip accent">
                {label}
              </span>
            ))}
            <small className="cosmic-agents-detail-chip-note">granted to cosmic-agents at install</small>
          </div>
        ) : null}

        {loading && repositories.length === 0 ? (
          <p className="cosmic-agents-detail-section-copy">Reading the repository registry…</p>
        ) : activeRepos.length === 0 && removedRepos.length === 0 ? (
          <p className="cosmic-agents-detail-section-copy">
            {accounts.length === 0
              ? 'Connect an account to see which repositories Cosmic can work in.'
              : 'No repositories in this installation yet. Add one on GitHub, then refresh here.'}
          </p>
        ) : (
          <div className="cosmic-agents-detail-mode-list">
            {activeRepos.map((repo) => (
              <button
                key={repo.repo_row_id}
                type="button"
                className="cosmic-agents-detail-mode"
                onClick={() => openOnGitHub(repo.html_url)}
                title={`Open ${repo.full_name} on GitHub`}
              >
                <span className="cosmic-agents-detail-mode-stack">
                  <span className="cosmic-agents-detail-repo-name">{repo.full_name}</span>
                  <small>
                    {repo.branch || repo.default_branch || 'default branch'}
                    {repo.local_path ? ' · cloned on VM' : ' · not cloned yet'}
                  </small>
                </span>
                <span className="cosmic-agents-detail-chip-row">
                  <span className="cosmic-agents-detail-chip">{repo.private ? 'Private' : 'Public'}</span>
                  {repo.can_push ? <span className="cosmic-agents-detail-chip accent">Push</span> : null}
                </span>
              </button>
            ))}
            {removedRepos.map((repo) => (
              <div key={repo.repo_row_id} className="cosmic-agents-detail-mode removed">
                <span className="cosmic-agents-detail-mode-stack">
                  <span className="cosmic-agents-detail-repo-name">{repo.full_name}</span>
                  <small>{repo.sync_error || 'No longer in the installation.'}</small>
                </span>
                <span className="cosmic-agents-detail-chip-row">
                  <span className="cosmic-agents-detail-chip danger">Access removed</span>
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="cosmic-agents-detail-footnote">
        <Github size={14} />
        <span>
          Repository access is granted per repository when you install, not by this connection —
          the list above mirrors that selection, and GitHub keeps it current via webhooks. Change
          it any time from the app's page on GitHub.
        </span>
      </div>
    </div>
  )
}

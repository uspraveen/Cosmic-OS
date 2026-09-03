import { useCallback, useEffect, useMemo, useState, type ReactElement } from 'react'
import {
  AlertTriangle,
  ChevronDown,
  ExternalLink,
  Github,
  LogIn,
  RefreshCw,
  Trash2,
} from 'lucide-react'

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

const metaString = (account: GitHubAccount, key: string): string =>
  String((account._metadata?.[key] as string | undefined) || '').trim()

const accountLogin = (account: GitHubAccount): string => metaString(account, 'github_login')

const accountPermissions = (account: GitHubAccount): Record<string, unknown> => {
  const perms = account._metadata?.github_permissions
  return perms && typeof perms === 'object' ? (perms as Record<string, unknown>) : {}
}

const accountSyncedAt = (account: GitHubAccount): number =>
  Number((account._metadata?.github_repos_synced_at as number | undefined) || 0)

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

type AccountState = 'healthy' | 'reconnect' | 'unreachable' | 'inactive'

const accountState = (account: GitHubAccount, health: GitHubHealthAccount | undefined): AccountState => {
  if (health?.status === 'reauth_required' || !isConnected(account)) return 'reconnect'
  if (health?.status === 'provider_error') return 'unreachable'
  if (health && health.status === 'healthy') return 'healthy'
  return isConnected(account) ? 'healthy' : 'reconnect'
}

const STATE_LABELS: Record<AccountState, string> = {
  healthy: 'Connected',
  reconnect: 'Reconnect needed',
  unreachable: 'Unreachable',
  inactive: 'Inactive',
}

export default function GitHubIntegrationSettings({ active }: GitHubIntegrationSettingsProps) {
  const [accounts, setAccounts] = useState<GitHubAccount[]>([])
  const [repositories, setRepositories] = useState<GitHubRepository[]>([])
  const [health, setHealth] = useState<GitHubHealthAccount[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [syncingId, setSyncingId] = useState('')
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
      setRepositories(
        Array.isArray(payload?.repositories) ? (payload.repositories as GitHubRepository[]) : [],
      )
    }
    if (healthResult.status === 'fulfilled') {
      const payload = healthResult.value
      setHealth(Array.isArray(payload?.accounts) ? (payload.accounts as GitHubHealthAccount[]) : [])
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

  // Default every card open: with one account — the overwhelmingly common
  // case — the repositories should be visible without a click.
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
      const result = await window.cosmic?.connectGitHubAccount({})
      // The main process reports the real outcome; never assume success from
      // the call resolving. A dead connect that shows a green banner is the
      // exact failure mode this codebase has been bitten by before.
      if (result?.success) {
        setBanner('GitHub connected.')
        await refresh()
      } else if (result?.error === 'cancelled') {
        // The user cancelled from the island; it already reported that, so the
        // panel reopens quietly instead of flagging an error.
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

  const verifyAccount = async (account: GitHubAccount) => {
    setSyncingId(account.account_id)
    setError('')
    try {
      const result = await window.cosmic?.syncGitHubRepositories(account.account_id)
      if (result?.sync && result.sync.synced === false) {
        setError(
          result.sync.error ||
            'GitHub would not refresh the repository list. Try reconnecting the account.',
        )
      } else {
        setRepositories(
          Array.isArray(result?.repositories) ? (result.repositories as GitHubRepository[]) : [],
        )
        setBanner(`Verified ${accountName(account)} with GitHub.`)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to refresh repositories.')
    } finally {
      setSyncingId('')
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
    if (accounts.length === 0)
      return { className: 'warn', label: 'Setup', title: 'Connect a GitHub account to begin.' }
    if (!health) {
      return accounts.some(isConnected)
        ? {
            className: 'ready',
            label: 'Ready',
            title: 'Connected (live check unavailable).',
          }
        : { className: 'warn', label: 'Setup', title: 'Connect a GitHub account to begin.' }
    }
    // A single degraded account must not hide behind a green pill: reconnect
    // beats healthy, provider outage beats both.
    if (health.some((item) => item.status === 'reauth_required')) {
      return {
        className: 'warn',
        label: 'Reconnect',
        title: 'GitHub rejected the credential. Reconnect the account.',
      }
    }
    if (health.some((item) => item.status !== 'healthy')) {
      return {
        className: 'warn',
        label: 'Unreachable',
        title: 'The gateway could not reach GitHub. Check the connection.',
      }
    }
    return {
      className: 'ready',
      label: 'Ready',
      title: 'Verified live: token resolved and confirmed with GitHub.',
    }
  }, [accounts, health])

  const reposForAccount = useMemo(() => {
    const map = new Map<string, GitHubRepository[]>()
    for (const repo of repositories) {
      const key = repo.account_id || ''
      const list = map.get(key) || []
      list.push(repo)
      map.set(key, list)
    }
    return map
  }, [repositories])

  const manageInstallationsUrl = useMemo(() => {
    for (const account of accounts) {
      const installationId = metaString(account, 'github_installation_id')
      if (installationId) return `https://github.com/settings/installations/${installationId}`
    }
    return 'https://github.com/settings/installations'
  }, [accounts])

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
              title="Refresh accounts and re-check access live"
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
          <div className="cosmic-agents-detail-acct-list">
            {accounts.map((account) => {
              const isOpen = expanded.has(account.account_id)
              const state = accountState(account, healthByAccount.get(account.account_id))
              const login = accountLogin(account)
              const syncedAt = accountSyncedAt(account)
              const accountRepos = reposForAccount.get(account.account_id) || []
              const activeRepos = accountRepos.filter((repo) => repo.status === 'active')
              const removedRepos = accountRepos.filter((repo) => repo.status !== 'active')
              const grants = permissionChips(accountPermissions(account))
              const syncing = syncingId === account.account_id
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
                          <Github size={20} />
                        )}
                      </span>
                      <span className="cosmic-agents-detail-acct-id">
                        <span className="cosmic-agents-detail-acct-name">
                          {accountName(account)}
                        </span>
                        <span className="cosmic-agents-detail-acct-sub">
                          {login ? `${login} · ` : ''}GitHub App
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
                                'GitHub rejected this connection.'}{' '}
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

                        {grants.length ? (
                          <div className="cosmic-agents-detail-grant-row">
                            {grants.map((label) => (
                              <span key={label} className="cosmic-agents-detail-chip accent">
                                {label}
                              </span>
                            ))}
                            <small className="cosmic-agents-detail-chip-note">
                              granted to cosmic-agents at install
                            </small>
                          </div>
                        ) : null}

                        <div className="cosmic-agents-detail-repos-head">
                          <span className="cosmic-agents-detail-kicker">
                            Repositories · {activeRepos.length}
                          </span>
                          <span className="cosmic-agents-detail-repos-meta">
                            {syncedAt ? `synced ${timeAgo(syncedAt)}` : 'not verified yet'}
                          </span>
                          <button
                            type="button"
                            className="cosmic-agents-detail-btn ghost sm"
                            onClick={() => verifyAccount(account)}
                            disabled={syncing || state === 'reconnect'}
                            title="Re-check the repository selection with GitHub"
                          >
                            <RefreshCw size={12} className={syncing ? 'spinning' : undefined} />
                            {syncing ? 'Verifying…' : 'Verify'}
                          </button>
                        </div>

                        {activeRepos.length === 0 && removedRepos.length === 0 ? (
                          <div className="cosmic-agents-detail-repos-empty">
                            <p>
                              No repositories in this installation yet. Add one on GitHub, then
                              verify here.
                            </p>
                            <button
                              type="button"
                              className="cosmic-agents-detail-btn ghost sm"
                              onClick={() => openOnGitHub(manageInstallationsUrl)}
                            >
                              <ExternalLink size={12} />
                              Manage on GitHub
                            </button>
                          </div>
                        ) : (
                          <div className="cosmic-agents-detail-repo-list">
                            {activeRepos.map((repo) => (
                              <button
                                key={repo.repo_row_id}
                                type="button"
                                className="cosmic-agents-detail-repo"
                                onClick={() => openOnGitHub(repo.html_url)}
                                title={`Open ${repo.full_name} on GitHub`}
                              >
                                <span className="cosmic-agents-detail-repo-stack">
                                  <span className="cosmic-agents-detail-repo-name">
                                    {splitRepoName(repo.full_name)}
                                  </span>
                                  <span className="cosmic-agents-detail-repo-sub">
                                    {repo.branch || repo.default_branch || 'default branch'}
                                    {repo.local_path ? ' · cloned on VM' : ' · not cloned yet'}
                                  </span>
                                </span>
                                <span className="cosmic-agents-detail-chip-row">
                                  <span className="cosmic-agents-detail-chip">
                                    {repo.private ? 'Private' : 'Public'}
                                  </span>
                                  {repo.can_push ? (
                                    <span className="cosmic-agents-detail-chip accent">Push</span>
                                  ) : null}
                                </span>
                              </button>
                            ))}
                            {removedRepos.map((repo) => (
                              <div
                                key={repo.repo_row_id}
                                className="cosmic-agents-detail-repo removed"
                              >
                                <span className="cosmic-agents-detail-repo-stack">
                                  <span className="cosmic-agents-detail-repo-name">
                                    {splitRepoName(repo.full_name)}
                                  </span>
                                  <span className="cosmic-agents-detail-repo-sub">
                                    {repo.sync_error || 'No longer in the installation.'}
                                  </span>
                                </span>
                                <span className="cosmic-agents-detail-chip-row">
                                  <span className="cosmic-agents-detail-chip danger">
                                    Access removed
                                  </span>
                                </span>
                              </div>
                            ))}
                          </div>
                        )}

                        {activeRepos.length > 0 ? (
                          <div className="cosmic-agents-detail-acct-foot">
                            <button
                              type="button"
                              className="cosmic-agents-detail-btn ghost sm"
                              onClick={() => openOnGitHub(manageInstallationsUrl)}
                              title="Change which repositories Cosmic may access"
                            >
                              <ExternalLink size={12} />
                              Manage on GitHub
                            </button>
                          </div>
                        ) : null}
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
        <Github size={14} />
        <span>
          Repository access is granted per repository when you install, not by this connection —
          each account above mirrors its installation, and GitHub keeps it current. Change it any
          time from the app's page on GitHub.
        </span>
      </div>
    </div>
  )
}

/** Owner dimmed, repo name bright: "acme/ makes /site" reads at a glance. */
const splitRepoName = (fullName: string): ReactElement => {
  const slash = fullName.indexOf('/')
  if (slash <= 0) return <>{fullName}</>
  return (
    <>
      <span className="cosmic-agents-detail-repo-owner">{fullName.slice(0, slash + 1)}</span>
      {fullName.slice(slash + 1)}
    </>
  )
}

import { useCallback, useEffect, useMemo, useState } from 'react'
import './vault-settings.css'

interface VaultPolicy {
  mode: 'always_ask' | 'always_allow' | 'window'
  window_expires_at?: string | null
  updated_at?: string | null
}

interface VaultEntry {
  entry_id: string
  title: string
  site_url?: string | null
  site_domain?: string | null
  username?: string | null
  tags?: string[]
  source?: string | null
  has_password?: boolean
  has_totp?: boolean
  has_notes?: boolean
  created_at?: string | null
  updated_at?: string | null
  policy?: VaultPolicy | null
}

interface VaultPendingRequest {
  request_id: string
  action?: string | null
  entry_id?: string | null
  status?: string | null
  purpose?: string | null
  created_at?: string | null
  payload?: {
    title?: string | null
    site_url?: string | null
    site_domain?: string | null
    username?: string | null
    password_mask?: string | null
  } | null
}

interface VaultAuditRow {
  audit_id: number
  timestamp: string
  entry_id?: string | null
  actor?: string | null
  action?: string | null
  result?: string | null
}

interface PasswordVaultSettingsProps {
  active: boolean
}

type EditorState = {
  entryId: string | null
  title: string
  siteUrl: string
  username: string
  password: string
  totpSeed: string
  notes: string
}

const EMPTY_EDITOR: EditorState = {
  entryId: null,
  title: '',
  siteUrl: '',
  username: '',
  password: '',
  totpSeed: '',
  notes: '',
}

const REVEAL_HIDE_MS = 15000

function formatTimestamp(value?: string | null): string {
  const normalized = String(value || '').trim()
  if (!normalized) return '—'
  const parsed = new Date(normalized)
  if (Number.isNaN(parsed.getTime())) return normalized
  return parsed.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function getErrorMessage(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback
}

function policyLabel(mode?: string | null): string {
  switch (String(mode || 'always_ask')) {
    case 'always_allow':
      return 'Always allow'
    case 'window':
      return 'Time window'
    default:
      return 'Always ask'
  }
}

function windowActive(policy?: VaultPolicy | null): boolean {
  if (!policy || policy.mode !== 'window' || !policy.window_expires_at) return false
  const parsed = new Date(policy.window_expires_at)
  return !Number.isNaN(parsed.getTime()) && parsed.getTime() > Date.now()
}

const AUDIT_ACTION_LABELS: Record<string, string> = {
  create: 'Created entry',
  update: 'Edited entry',
  delete: 'Deleted entry',
  use: 'Used by Cosmic',
  resolve: 'Injected into a task',
  view_password: 'Password revealed',
  view_totp: '2FA code viewed',
  policy_change: 'Policy changed',
  save_approved: 'Agent save approved',
  use_approved: 'Access approved',
  request_rejected: 'Request denied',
  save_requested: 'Cosmic asked to save credentials',
  lookup_denied: 'Access needed approval',
}

function ShieldGlyph({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M12 3.25 5.25 6v5.4c0 4.3 2.85 8.05 6.75 9.35 3.9-1.3 6.75-5.05 6.75-9.35V6L12 3.25Z"
        stroke="currentColor"
        strokeWidth="1.35"
        strokeLinejoin="round"
      />
      <path
        d="M12 10.25v3.5M12 8.6a1.1 1.1 0 1 0 0-.05"
        stroke="currentColor"
        strokeWidth="1.35"
        strokeLinecap="round"
      />
    </svg>
  )
}

export default function PasswordVaultSettings({ active }: PasswordVaultSettingsProps) {
  const [entries, setEntries] = useState<VaultEntry[]>([])
  const [pending, setPending] = useState<VaultPendingRequest[]>([])
  const [audit, setAudit] = useState<VaultAuditRow[]>([])
  const [showAudit, setShowAudit] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busyEntryId, setBusyEntryId] = useState<string | null>(null)
  const [revealedId, setRevealedId] = useState<string | null>(null)
  const [revealedPassword, setRevealedPassword] = useState('')
  const [totpState, setTotpState] = useState<{ entryId: string; code: string; seconds: number } | null>(null)
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)
  const [editor, setEditor] = useState<EditorState | null>(null)
  const [saving, setSaving] = useState(false)

  const refresh = useCallback(async () => {
    if (!window.cosmic?.vaultListEntries) return
    setLoading(true)
    setError(null)
    try {
      const [entriesPayload, pendingPayload] = await Promise.all([
        window.cosmic.vaultListEntries(),
        window.cosmic.vaultListPending ? window.cosmic.vaultListPending() : Promise.resolve({ pending: [] }),
      ])
      setEntries(Array.isArray(entriesPayload?.entries) ? entriesPayload.entries as VaultEntry[] : [])
      setPending(Array.isArray(pendingPayload?.pending) ? pendingPayload.pending as VaultPendingRequest[] : [])
      if (window.cosmic.vaultListAudit && showAudit) {
        const auditPayload = await window.cosmic.vaultListAudit()
        setAudit(Array.isArray(auditPayload?.audit) ? auditPayload.audit as VaultAuditRow[] : [])
      }
    } catch (err: unknown) {
      setError(getErrorMessage(err, 'Failed to load the password vault.'))
    } finally {
      setLoading(false)
    }
  }, [showAudit])

  useEffect(() => {
    if (!active) return
    void refresh()
  }, [active, refresh])

  // Live refresh while the panel is open: approval requests, approvals made
  // from a chat card, and entry changes all arrive as gateway events.
  useEffect(() => {
    if (!active) return
    if (!window.cosmic?.onGatewayEvent) return
    const unsubscribe = window.cosmic.onGatewayEvent((event: any) => {
      const type = String(event?.type || '')
      if (type === 'vault.notification' || type === 'response.action.updated') {
        void refresh()
      }
    })
    return unsubscribe
  }, [active, refresh])

  // Auto-hide a revealed password.
  useEffect(() => {
    if (!revealedId) return
    const timer = window.setTimeout(() => {
      setRevealedId(null)
      setRevealedPassword('')
    }, REVEAL_HIDE_MS)
    return () => window.clearTimeout(timer)
  }, [revealedId])

  // Keep the visible TOTP code ticking; refetch when the window rolls over.
  useEffect(() => {
    if (!totpState) return
    if (totpState.seconds > 1) {
      const timer = window.setInterval(() => {
        setTotpState((current) =>
          current && current.seconds > 1
            ? { ...current, seconds: current.seconds - 1 }
            : current,
        )
      }, 1000)
      return () => window.clearInterval(timer)
    }
    let cancelled = false
    const refetch = async () => {
      if (!window.cosmic?.vaultTotpCode) return
      try {
        const payload = await window.cosmic.vaultTotpCode(totpState.entryId)
        if (!cancelled && payload?.code) {
          setTotpState({
            entryId: totpState.entryId,
            code: payload.code,
            seconds: payload.seconds_remaining || 30,
          })
        } else if (!cancelled) {
          setTotpState(null)
        }
      } catch {
        if (!cancelled) setTotpState(null)
      }
    }
    void refetch()
    return () => {
      cancelled = true
    }
  }, [totpState])

  const openEditor = useCallback((entry?: VaultEntry) => {
    setConfirmDeleteId(null)
    setEditor(
      entry
        ? {
            entryId: entry.entry_id,
            title: entry.title || '',
            siteUrl: entry.site_url || '',
            username: entry.username || '',
            password: '',
            totpSeed: '',
            notes: '',
          }
        : { ...EMPTY_EDITOR },
    )
  }, [])

  const handleSave = useCallback(async () => {
    if (!editor || saving) return
    setSaving(true)
    setError(null)
    try {
      if (editor.entryId) {
        const patch: Record<string, unknown> = {
          title: editor.title,
          site_url: editor.siteUrl,
          username: editor.username,
          notes: editor.notes,
        }
        if (editor.password) patch.password = editor.password
        if (editor.totpSeed) patch.totp_seed = editor.totpSeed
        await window.cosmic?.vaultUpdateEntry?.(editor.entryId, patch)
      } else {
        await window.cosmic?.vaultCreateEntry?.({
          title: editor.title,
          site_url: editor.siteUrl,
          username: editor.username,
          password: editor.password,
          totp_seed: editor.totpSeed,
          notes: editor.notes,
        })
      }
      setEditor(null)
      await refresh()
    } catch (err: unknown) {
      setError(getErrorMessage(err, 'Failed to save the vault entry.'))
    } finally {
      setSaving(false)
    }
  }, [editor, saving, refresh])

  const handleDelete = useCallback(async (entry: VaultEntry) => {
    if (!window.cosmic?.vaultDeleteEntry) return
    setBusyEntryId(entry.entry_id)
    setError(null)
    try {
      await window.cosmic.vaultDeleteEntry(entry.entry_id)
      setConfirmDeleteId(null)
      if (revealedId === entry.entry_id) {
        setRevealedId(null)
        setRevealedPassword('')
      }
      await refresh()
    } catch (err: unknown) {
      setError(getErrorMessage(err, 'Failed to delete the vault entry.'))
    } finally {
      setBusyEntryId(null)
    }
  }, [refresh, revealedId])

  const handleReveal = useCallback(async (entry: VaultEntry) => {
    if (!window.cosmic?.vaultRevealPassword) return
    setError(null)
    try {
      if (revealedId === entry.entry_id) {
        setRevealedId(null)
        setRevealedPassword('')
        return
      }
      const payload = await window.cosmic.vaultRevealPassword(entry.entry_id)
      setRevealedId(entry.entry_id)
      setRevealedPassword(String(payload?.password || ''))
    } catch (err: unknown) {
      setError(getErrorMessage(err, 'Failed to reveal the password.'))
    }
  }, [revealedId])

  const handleShowTotp = useCallback(async (entry: VaultEntry) => {
    if (!window.cosmic?.vaultTotpCode) return
    setError(null)
    try {
      if (totpState?.entryId === entry.entry_id) {
        setTotpState(null)
        return
      }
      const payload = await window.cosmic.vaultTotpCode(entry.entry_id)
      if (!payload?.code) {
        setError('No 2FA seed is stored for this entry.')
        return
      }
      setTotpState({ entryId: entry.entry_id, code: payload.code, seconds: payload.seconds_remaining || 30 })
    } catch (err: unknown) {
      setError(getErrorMessage(err, 'Failed to generate the 2FA code.'))
    }
  }, [totpState])

  const handlePolicy = useCallback(async (entry: VaultEntry, mode: VaultPolicy['mode']) => {
    if (!window.cosmic?.vaultSetPolicy) return
    setBusyEntryId(entry.entry_id)
    setError(null)
    try {
      await window.cosmic.vaultSetPolicy(entry.entry_id, {
        mode,
        window_seconds: mode === 'window' ? 24 * 3600 : null,
      })
      await refresh()
    } catch (err: unknown) {
      setError(getErrorMessage(err, 'Failed to update the access policy.'))
    } finally {
      setBusyEntryId(null)
    }
  }, [refresh])

  const handlePendingAction = useCallback(async (requestId: string, kind: 'approve' | 'reject') => {
    const bridge = kind === 'approve' ? window.cosmic?.vaultApprovePending : window.cosmic?.vaultRejectPending
    if (!bridge) return
    setBusyEntryId(requestId)
    setError(null)
    try {
      await bridge(requestId)
      await refresh()
    } catch (err: unknown) {
      setError(getErrorMessage(err, 'Failed to respond to the vault request.'))
    } finally {
      setBusyEntryId(null)
    }
  }, [refresh])

  const toggleAudit = useCallback(async () => {
    const next = !showAudit
    setShowAudit(next)
    if (next && window.cosmic?.vaultListAudit) {
      try {
        const payload = await window.cosmic.vaultListAudit()
        setAudit(Array.isArray(payload?.audit) ? payload.audit as VaultAuditRow[] : [])
      } catch (err: unknown) {
        setError(getErrorMessage(err, 'Failed to load the vault activity.'))
      }
    }
  }, [showAudit])

  const openPending = useMemo(() => pending.filter((item) => String(item.status || 'pending') === 'pending'), [pending])
  const alwaysAllowCount = useMemo(
    () => entries.filter((entry) => entry.policy?.mode === 'always_allow' || windowActive(entry.policy)).length,
    [entries],
  )

  return (
    <div className="setting-subpage vault-page">
      <header className="vault-header">
        <div className="vault-header-top">
          <div className="vault-header-text">
            <span className="vault-kicker">Security</span>
            <h2 className="vault-title">Password Vault</h2>
          </div>
          <div className="vault-header-actions">
            <button
              type="button"
              className="vault-btn vault-btn--subtle"
              onClick={() => void refresh()}
              disabled={loading}
            >
              {loading ? 'Refreshing…' : 'Refresh'}
            </button>
            <button
              type="button"
              className="vault-btn vault-btn--primary"
              onClick={() => openEditor()}
            >
              Add credential
            </button>
          </div>
        </div>
        <p className="vault-lead">
          Credentials stored here are encrypted on your VM. Cosmic's orchestrator can use them to sign
          in for you — it only ever sees a reference, never the password itself, and each site follows
          the access rule you set below.
        </p>
      </header>

      <div className="vault-stats" aria-label="Vault summary">
        <div className="vault-stat">
          <span className="vault-stat-value">{entries.length}</span>
          <span className="vault-stat-label">Saved logins</span>
        </div>
        <div className="vault-stat vault-stat--accent">
          <span className="vault-stat-value">{alwaysAllowCount}</span>
          <span className="vault-stat-label">Auto-allowed sites</span>
        </div>
        <div className="vault-stat">
          <span className="vault-stat-value">{openPending.length}</span>
          <span className="vault-stat-label">Awaiting your approval</span>
        </div>
      </div>

      {error ? (
        <div className="vault-error" role="alert">
          {error}
        </div>
      ) : null}

      {openPending.length > 0 ? (
        <div className="vault-pending-section">
          <h3 className="vault-section-title">Cosmic is asking for vault access</h3>
          {openPending.map((item) => {
            const payload = item.payload || {}
            const isAdd = item.action === 'add_entry'
            return (
              <article key={item.request_id} className="vault-pending-card">
                <div className="vault-pending-main">
                  <div className="vault-pending-headline">
                    {isAdd ? 'Cosmic wants to save a new login' : 'Cosmic wants to use a saved login'}
                  </div>
                  <div className="vault-pending-site">
                    {payload.title || payload.site_domain || 'Unknown site'}
                    {payload.site_domain && payload.title ? ` · ${payload.site_domain}` : ''}
                    {payload.username ? ` · ${payload.username}` : ''}
                  </div>
                  {isAdd && payload.password_mask ? (
                    <div className="vault-pending-detail">Password {payload.password_mask} — stored encrypted on approval.</div>
                  ) : null}
                  {item.purpose ? <div className="vault-pending-detail">Reason: {item.purpose}</div> : null}
                </div>
                <div className="vault-pending-actions">
                  <button
                    type="button"
                    className="vault-btn vault-btn--subtle"
                    disabled={busyEntryId === item.request_id}
                    onClick={() => void handlePendingAction(item.request_id, 'reject')}
                  >
                    Deny
                  </button>
                  <button
                    type="button"
                    className="vault-btn vault-btn--primary"
                    disabled={busyEntryId === item.request_id}
                    onClick={() => void handlePendingAction(item.request_id, 'approve')}
                  >
                    {busyEntryId === item.request_id ? 'Working…' : isAdd ? 'Allow & save' : 'Allow once'}
                  </button>
                </div>
              </article>
            )
          })}
        </div>
      ) : null}

      {loading && entries.length === 0 ? (
        <div className="vault-loading" aria-live="polite">
          <span className="vault-loading-dot" />
          Loading vault…
        </div>
      ) : null}

      {entries.length === 0 && !loading ? (
        <div className="vault-empty">
          <div className="vault-empty-icon" aria-hidden>
            <ShieldGlyph className="vault-empty-glyph" />
          </div>
          <strong>No credentials saved yet</strong>
          <p>Add one here, or ask Cosmic to create an account for you — it will save the login after your approval.</p>
        </div>
      ) : null}

      <div className="vault-list">
        {entries.map((entry) => {
          const policy = entry.policy
          const isRevealed = revealedId === entry.entry_id
          const showTotp = totpState?.entryId === entry.entry_id
          const confirmingDelete = confirmDeleteId === entry.entry_id
          return (
            <article key={entry.entry_id} className="vault-entry-card">
              <div className="vault-entry-top">
                <div className="vault-entry-icon-wrap" aria-hidden>
                  <ShieldGlyph className="vault-entry-icon" />
                </div>
                <div className="vault-entry-main">
                  <div className="vault-entry-title-row">
                    <div className="vault-entry-title-block">
                      <span className="vault-entry-eyebrow">{entry.site_domain || 'credential'}</span>
                      <div className="vault-entry-name">{entry.title || entry.entry_id}</div>
                    </div>
                    <div className="vault-entry-pill-wrap">
                      <span className={`vault-entry-pill is-${entry.source === 'agent' ? 'agent' : 'user'}`}>
                        {entry.source === 'agent' ? 'Added by Cosmic' : 'Added by you'}
                      </span>
                      {entry.has_totp ? <span className="vault-entry-pill is-totp">2FA</span> : null}
                    </div>
                  </div>
                  {entry.username ? <div className="vault-entry-username">{entry.username}</div> : null}
                </div>
              </div>

              <div className="vault-policy-panel">
                <span className="vault-policy-label">Cosmic access</span>
                <div className="vault-policy-segments" role="group" aria-label="Agent access policy">
                  {(['always_ask', 'always_allow', 'window'] as const).map((mode) => (
                    <button
                      key={mode}
                      type="button"
                      className={`vault-policy-segment${(policy?.mode || 'always_ask') === mode ? ' is-active' : ''}`}
                      disabled={busyEntryId === entry.entry_id}
                      title={
                        mode === 'always_ask'
                          ? 'Ask you with an approval card every time'
                          : mode === 'always_allow'
                            ? 'Use without asking, fully audited'
                            : 'Allow automatically for the next 24 hours'
                      }
                      onClick={() => void handlePolicy(entry, mode)}
                    >
                      {policyLabel(mode)}
                    </button>
                  ))}
                </div>
                {policy?.mode === 'window' && windowActive(policy) ? (
                  <span className="vault-policy-window-note">until {formatTimestamp(policy.window_expires_at)}</span>
                ) : null}
              </div>

              {isRevealed ? (
                <div className="vault-secret-row">
                  <span className="vault-secret-value">{revealedPassword || '(empty)'}</span>
                  <span className="vault-secret-note">hides automatically</span>
                </div>
              ) : null}
              {showTotp ? (
                <div className="vault-secret-row is-totp">
                  <span className="vault-secret-value">{totpState?.code}</span>
                  <span className="vault-secret-note">{totpState?.seconds}s remaining</span>
                </div>
              ) : null}

              <div className="vault-entry-actions">
                <button
                  type="button"
                  className="vault-btn vault-btn--subtle"
                  disabled={!entry.has_password}
                  onClick={() => void handleReveal(entry)}
                >
                  {isRevealed ? 'Hide password' : 'Reveal'}
                </button>
                {entry.has_totp ? (
                  <button
                    type="button"
                    className="vault-btn vault-btn--subtle"
                    onClick={() => void handleShowTotp(entry)}
                  >
                    {showTotp ? 'Hide code' : '2FA code'}
                  </button>
                ) : null}
                <button
                  type="button"
                  className="vault-btn vault-btn--subtle"
                  onClick={() => openEditor(entry)}
                >
                  Edit
                </button>
                {confirmingDelete ? (
                  <>
                    <button
                      type="button"
                      className="vault-btn vault-btn--ghost"
                      onClick={() => setConfirmDeleteId(null)}
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      className="vault-btn vault-btn--danger"
                      disabled={busyEntryId === entry.entry_id}
                      onClick={() => void handleDelete(entry)}
                    >
                      {busyEntryId === entry.entry_id ? 'Deleting…' : 'Confirm delete'}
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    className="vault-btn vault-btn--danger"
                    onClick={() => setConfirmDeleteId(entry.entry_id)}
                  >
                    Delete
                  </button>
                )}
              </div>
            </article>
          )
        })}
      </div>

      <div className="vault-audit-section">
        <button type="button" className="vault-audit-toggle" onClick={() => void toggleAudit()}>
          {showAudit ? 'Hide vault activity' : 'Show vault activity'}
        </button>
        {showAudit ? (
          audit.length === 0 ? (
            <div className="vault-audit-empty">No activity recorded yet.</div>
          ) : (
            <ul className="vault-audit-list">
              {audit.map((row) => (
                <li key={row.audit_id} className="vault-audit-row">
                  <span className="vault-audit-action">{AUDIT_ACTION_LABELS[String(row.action || '')] || row.action}</span>
                  <span className="vault-audit-actor">{row.actor === 'orchestrator' ? 'Cosmic' : 'You'}</span>
                  <span className="vault-audit-time">{formatTimestamp(row.timestamp)}</span>
                </li>
              ))}
            </ul>
          )
        ) : null}
      </div>

      {editor ? (
        <div className="vault-modal-overlay" role="dialog" aria-modal="true">
          <div className="vault-modal">
            <h3 className="vault-modal-title">{editor.entryId ? 'Edit credential' : 'Add credential'}</h3>
            <label className="vault-field">
              <span>Name</span>
              <input
                type="text"
                value={editor.title}
                placeholder="GitHub"
                onChange={(event) => setEditor({ ...editor, title: event.target.value })}
              />
            </label>
            <label className="vault-field">
              <span>Site URL</span>
              <input
                type="text"
                value={editor.siteUrl}
                placeholder="https://github.com"
                onChange={(event) => setEditor({ ...editor, siteUrl: event.target.value })}
              />
            </label>
            <label className="vault-field">
              <span>Username</span>
              <input
                type="text"
                value={editor.username}
                autoComplete="off"
                onChange={(event) => setEditor({ ...editor, username: event.target.value })}
              />
            </label>
            <label className="vault-field">
              <span>{editor.entryId ? 'New password (leave blank to keep)' : 'Password'}</span>
              <input
                type="password"
                value={editor.password}
                autoComplete="new-password"
                onChange={(event) => setEditor({ ...editor, password: event.target.value })}
              />
            </label>
            <label className="vault-field">
              <span>2FA seed (optional, base32)</span>
              <input
                type="password"
                value={editor.totpSeed}
                autoComplete="off"
                placeholder="JBSWY3DPEHPK3PXP"
                onChange={(event) => setEditor({ ...editor, totpSeed: event.target.value })}
              />
            </label>
            <label className="vault-field">
              <span>Notes (optional, encrypted)</span>
              <textarea
                value={editor.notes}
                rows={3}
                onChange={(event) => setEditor({ ...editor, notes: event.target.value })}
              />
            </label>
            <div className="vault-modal-actions">
              <button
                type="button"
                className="vault-btn vault-btn--ghost"
                onClick={() => setEditor(null)}
                disabled={saving}
              >
                Cancel
              </button>
              <button
                type="button"
                className="vault-btn vault-btn--primary"
                onClick={() => void handleSave()}
                disabled={saving || (!editor.entryId && !editor.password)}
              >
                {saving ? 'Saving…' : editor.entryId ? 'Save changes' : 'Save credential'}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}

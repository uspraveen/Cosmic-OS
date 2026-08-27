import { useEffect, useMemo, useRef, useState } from 'react'
import { Bot, CheckCircle2, ExternalLink, LogIn, RefreshCw, ShieldCheck, Terminal, Trash2 } from 'lucide-react'
import {
  describeLoginReason,
  extractLoginUrl,
  loginSessionLines,
  loginStartOutcome,
  stripAnsi,
} from './agentLogin'
import { CursorMark } from './brandIcons'

type CursorApprovalMode = 'suggest' | 'auto_edit' | 'full_auto'

const MODEL_OPTIONS = [
  { value: 'auto', label: 'Auto', note: 'Use CLI config' },
  { value: 'cursor-grok-4.5-high', label: 'Cursor Grok 4.5', note: 'High effort, not Fast' },
  { value: 'composer-2.5', label: 'Composer 2.5', note: 'Standard, not Fast' },
  { value: 'gpt-5', label: 'GPT-5', note: 'Manual' },
  { value: 'claude-4-sonnet', label: 'Claude Sonnet', note: 'Manual' },
  { value: 'opus-4.6', label: 'Opus 4.6', note: 'Manual' },
]

const APPROVAL_MODES: Array<{ value: CursorApprovalMode; label: string; note: string }> = [
  { value: 'suggest', label: 'Suggest', note: 'Review first' },
  { value: 'auto_edit', label: 'Auto Edit', note: 'Apply code edits' },
  { value: 'full_auto', label: 'Full Auto', note: 'Allow commands' },
]

interface CursorAgentSettingsProps {
  active: boolean
}

interface CursorGatewayStatus {
  preferred_model?: string
  approval_mode?: string
  vm_sync_enabled?: boolean
  status?: string
  login_required_reason?: string
  cursor_home?: string
  cli?: {
    available?: boolean
    authenticated?: boolean
    stdout?: string
    stderr?: string
    reason?: string
  }
  login_session?: {
    session_id?: string
    state?: string
    stdout?: string[]
    stderr?: string[]
  } | null
}

function normalizeApprovalMode(value: unknown): CursorApprovalMode {
  if (value === 'auto_edit' || value === 'full_auto') return value
  return 'suggest'
}

function normalizeCursorModelOption(value: unknown) {
  const normalized = String(value ?? 'auto').trim()
  if (!normalized || normalized === 'auto') return 'auto'
  const lowered = normalized.toLowerCase()
  if ([
    'grok',
    'grok 4.5',
    'grok4.5',
    'grok-4.5',
    'grok-4.5-high',
    'cursor grok',
    'cursor-grok',
    'cursor grok 4.5',
    'cursor-grok-4.5',
    'cursor-grok-4.5-high',
    'cursor grok 4.5 high',
  ].includes(lowered)) return 'cursor-grok-4.5-high'
  if ([
    'composer',
    'composer-2',
    'composer 2',
    'composer2',
    'composer-2.5',
    'composer 2.5',
    'composer2.5',
  ].includes(lowered)) return 'composer-2.5'
  return normalized
}

function extractFirstUrl(value: string) {
  return extractLoginUrl([value])
}

export default function CursorAgentSettings({ active }: CursorAgentSettingsProps) {
  const [preferredModel, setPreferredModel] = useState('auto')
  const [approvalMode, setApprovalMode] = useState<CursorApprovalMode>('suggest')
  const [vmSyncEnabled, setVmSyncEnabled] = useState(true)
  const [gatewayStatus, setGatewayStatus] = useState<CursorGatewayStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [banner, setBanner] = useState('')
  const [error, setError] = useState('')
  const openedLoginUrlRef = useRef('')

  useEffect(() => {
    if (!banner) return
    const timer = window.setTimeout(() => setBanner(''), 2600)
    return () => window.clearTimeout(timer)
  }, [banner])

  useEffect(() => {
    if (!active) return
    let cancelled = false
    const refresh = async () => {
      setLoading(true)
      setError('')
      try {
        const status = await window.cosmic?.getGatewayCursorStatus()
        if (!cancelled) applyGatewayStatus(status)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Unable to load Cursor status from the VM.')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void refresh()
    return () => {
      cancelled = true
    }
  }, [active])

  useEffect(() => {
    if (!active || gatewayStatus?.status !== 'login_pending') return
    const timer = window.setInterval(async () => {
      try {
        const status = await window.cosmic?.getGatewayCursorStatus()
        applyGatewayStatus(status)
      } catch {
        // keep the current pending session visible until the next explicit refresh
      }
    }, 2500)
    return () => window.clearInterval(timer)
  }, [active, gatewayStatus?.status])

  // The CLI prints its sign-in URL a moment after the process starts, so the
  // click that starts a login cannot open it. Open it as soon as it appears -
  // once per login session, so a status poll never reopens the tab. Settings
  // dismisses itself off the main process's external-opened signal.
  useEffect(() => {
    if (!active) return
    const session = gatewayStatus?.login_session
    if (!session || session.state !== 'running') return
    const url = extractLoginUrl(loginSessionLines(session))
    if (!url) return
    const key = `${session.session_id || ''}:${url}`
    if (openedLoginUrlRef.current === key) return
    openedLoginUrlRef.current = key
    void window.cosmic?.openExternal(url)
  }, [active, gatewayStatus?.login_session])

  const cliMissing = gatewayStatus?.cli?.available === false

  const connectionLabel = useMemo(() => {
    if (loading) return 'Checking VM status'
    // A missing CLI outranks every auth state: no sign-in can fix it, and
    // calling it "needs re-authentication" sends the user round a loop that
    // cannot terminate.
    if (cliMissing) return 'Cursor CLI missing on VM'
    if (gatewayStatus?.status === 'authenticated') return 'Cursor authenticated on VM'
    if (gatewayStatus?.status === 'login_pending') return 'OAuth waiting for browser approval'
    if (gatewayStatus?.status === 'relogin_required') return 'Cursor needs re-authentication on the VM'
    if (gatewayStatus?.status === 'login_required') return 'Cursor OAuth sign-in required on the VM'
    return 'OAuth sign-in required'
  }, [cliMissing, gatewayStatus, loading])

  const needsReauth = gatewayStatus?.status === 'relogin_required' || gatewayStatus?.status === 'login_required'

  const loginOutput = loginSessionLines(gatewayStatus?.login_session).slice(-8)
  const loginUrl = extractLoginUrl(loginOutput)

  const applyGatewayStatus = (rawStatus: unknown) => {
    const status = (rawStatus && typeof rawStatus === 'object' ? rawStatus : {}) as CursorGatewayStatus
    const nextModel = normalizeCursorModelOption(status.preferred_model)
    setGatewayStatus(status)
    setPreferredModel(MODEL_OPTIONS.some((option) => option.value === nextModel) ? nextModel : 'auto')
    setApprovalMode(normalizeApprovalMode(status.approval_mode))
    setVmSyncEnabled(status.vm_sync_enabled !== false)
  }

  const saveRemoteConfig = async (payload: {
    preferredModel?: string
    approvalMode?: CursorApprovalMode
    vmSyncEnabled?: boolean
  }, successMessage?: string) => {
    setSaving(true)
    setError('')
    try {
      const status = await window.cosmic?.saveGatewayCursorConfig(payload)
      applyGatewayStatus(status)
      if (successMessage) setBanner(successMessage)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to save Cursor settings to the VM.')
    } finally {
      setSaving(false)
    }
  }

  const startCursorLogin = async () => {
    setSaving(true)
    setError('')
    try {
      const status = await window.cosmic?.startGatewayCursorLogin()
      applyGatewayStatus(status)
      // The endpoint answers 200 whether or not it managed to start anything;
      // the truth is in the status it returns. Reporting success blind is what
      // made a dead Login button look like a working one.
      const outcome = loginStartOutcome(status as CursorGatewayStatus | null, 'Cursor')
      if (outcome.ok) setBanner(outcome.message)
      else setError(outcome.message)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to start Cursor login on the VM.')
    } finally {
      setSaving(false)
    }
  }

  const logoutCursor = async () => {
    setSaving(true)
    setError('')
    try {
      const status = await window.cosmic?.logoutGatewayCursor()
      applyGatewayStatus(status)
      setBanner('Cursor logged out on the VM.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to log Cursor out on the VM.')
    } finally {
      setSaving(false)
    }
  }

  const refreshStatus = async () => {
    setLoading(true)
    setError('')
    try {
      const status = await window.cosmic?.getGatewayCursorStatus()
      applyGatewayStatus(status)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to refresh Cursor status.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="cosmic-agents-detail-page">
      <div className="cosmic-agents-detail-hero provider-cursor">
        <div className="cosmic-agents-detail-hero-top">
          <div className="cosmic-agents-detail-hero-icon cursor" aria-hidden="true">
            <CursorMark size={26} />
          </div>
          <div className="cosmic-agents-detail-hero-text">
            <h3>Cursor CLI for Alpha</h3>
            <p>{connectionLabel}</p>
            <span>Runs Cursor Agent headless on your VM through OAuth-only login.</span>
          </div>
        </div>
        <div className={`cosmic-agents-detail-status-pill ${gatewayStatus?.status === 'authenticated' ? 'ready' : gatewayStatus?.status === 'login_pending' ? 'pending' : 'warn'}`}>
          {cliMissing
            ? 'Setup'
            : gatewayStatus?.status === 'authenticated'
              ? 'Ready'
              : gatewayStatus?.status === 'login_pending'
                ? 'Pending'
                : needsReauth
                  ? 'Reauth'
                  : 'Setup'}
        </div>
      </div>

      {banner ? <div className="cosmic-agents-detail-banner success" role="status"><span className="cosmic-agents-detail-banner-icon">✓</span>{banner}</div> : null}
      {error ? <div className="cosmic-agents-detail-banner error" role="alert"><span className="cosmic-agents-detail-banner-icon">!</span>{error}</div> : null}

      <div className="cosmic-agents-detail-section">
        <div className="cosmic-agents-detail-section-head">
          <div>
            <span className="cosmic-agents-detail-kicker">OAuth</span>
            <h4>{gatewayStatus?.status === 'authenticated' ? 'Signed in on VM' : 'Browser sign-in session'}</h4>
          </div>
          <div className="cosmic-agents-detail-actions">
            <button type="button" className="cosmic-agents-detail-btn ghost icon" onClick={refreshStatus} disabled={loading || saving} title="Refresh Cursor status">
              <RefreshCw size={15} />
            </button>
            {gatewayStatus?.status === 'authenticated' ? (
              <button type="button" className="cosmic-agents-detail-btn danger icon" onClick={logoutCursor} disabled={saving} title="Log out Cursor">
                <Trash2 size={15} />
              </button>
            ) : (
              <button
                type="button"
                className="cosmic-agents-detail-btn"
                onClick={startCursorLogin}
                disabled={saving || cliMissing}
                title={cliMissing ? 'Install the Cursor CLI on the VM first' : undefined}
              >
                <LogIn size={15} />
                {gatewayStatus?.status === 'login_pending' ? 'Restart' : 'Login'}
              </button>
            )}
          </div>
        </div>

        {cliMissing ? (
          <p className="cosmic-agents-detail-section-copy">{describeLoginReason(gatewayStatus)}</p>
        ) : loginOutput.length ? (
          <div className="cosmic-agents-detail-login-output">
            {loginUrl ? (
              <span>
                Sign-in page opened in your browser.
                <button type="button" onClick={() => window.cosmic?.openExternal(loginUrl)}>
                  <ExternalLink size={12} />
                  Open again
                </button>
              </span>
            ) : null}
            {loginOutput.map((line, index) => {
              const cleanLine = stripAnsi(line)
              const url = extractFirstUrl(line)
              return (
                <span key={`${line}-${index}`}>
                  {cleanLine}
                  {url && url !== loginUrl ? (
                    <button type="button" onClick={() => window.cosmic?.openExternal(url)}>
                      <ExternalLink size={12} />
                      Open
                    </button>
                  ) : null}
                </span>
              )
            })}
          </div>
        ) : (
          <p className="cosmic-agents-detail-section-copy">
            Starts `cursor-agent login` on the VM and streams the OAuth handoff output here.
          </p>
        )}
      </div>

      <div className="cosmic-agents-detail-section cosmic-agents-detail-runner">
        <div className="cosmic-agents-detail-section-head">
          <div>
            <span className="cosmic-agents-detail-kicker">Runner defaults</span>
            <h4>Model and autonomy</h4>
          </div>
        </div>

        <div className="cosmic-agents-detail-model-grid compact">
          {MODEL_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              className={`cosmic-agents-detail-model-card ${preferredModel === option.value ? 'active' : ''}`}
              onClick={() => {
                setPreferredModel(option.value)
                void saveRemoteConfig({ preferredModel: option.value })
              }}
              disabled={saving}
            >
              <span className="cosmic-agents-detail-model-name">{option.label}</span>
              <small>{option.note}</small>
              {preferredModel === option.value ? <CheckCircle2 size={16} className="cosmic-agents-detail-model-check-icon" /> : null}
            </button>
          ))}
        </div>

        <div className="cosmic-agents-detail-mode-list">
          {APPROVAL_MODES.map((mode) => (
            <button
              key={mode.value}
              type="button"
              className={`cosmic-agents-detail-mode ${approvalMode === mode.value ? 'active' : ''}`}
              onClick={() => {
                setApprovalMode(mode.value)
                void saveRemoteConfig({ approvalMode: mode.value })
              }}
              disabled={saving}
            >
              <div className="cosmic-agents-detail-mode-left">
                <span className="cosmic-agents-detail-mode-dot" aria-hidden="true" />
                <span>{mode.label}</span>
              </div>
              <small>{mode.note}</small>
            </button>
          ))}
        </div>
      </div>

      <div className="cosmic-agents-detail-runtime">
        <div className="cosmic-agents-detail-runtime-row">
          <div className="cosmic-agents-detail-runtime-icon" aria-hidden="true">
            <Terminal size={18} />
          </div>
          <div>
            <strong>VM Alpha workspace</strong>
            <span>Cursor runs in the resolved project workspace with `CURSOR_AGENT=1`.</span>
          </div>
        </div>
        <button
          type="button"
          className={`cosmic-agents-detail-sync-btn ${vmSyncEnabled ? 'active' : ''}`}
          onClick={() => {
            setVmSyncEnabled(!vmSyncEnabled)
            void saveRemoteConfig({ vmSyncEnabled: !vmSyncEnabled })
          }}
          disabled={saving}
        >
          <ShieldCheck size={15} />
          {vmSyncEnabled ? 'Sync on' : 'Sync off'}
        </button>
      </div>

      <div className="cosmic-agents-detail-footnote">
        <Bot size={14} />
        <span>{gatewayStatus?.cursor_home ? `HOME: ${gatewayStatus.cursor_home}` : 'Cursor home is managed on the VM.'}</span>
      </div>
    </div>
  )
}

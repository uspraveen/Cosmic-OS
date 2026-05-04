import { useEffect, useMemo, useState } from 'react'
import { Bot, CheckCircle2, ExternalLink, LogIn, RefreshCw, ShieldCheck, Terminal, Trash2 } from 'lucide-react'

type CursorApprovalMode = 'suggest' | 'auto_edit' | 'full_auto'

const MODEL_OPTIONS = [
  { value: 'auto', label: 'Auto' },
  { value: 'gpt-5', label: 'GPT-5' },
  { value: 'claude-4-sonnet', label: 'Claude Sonnet' },
  { value: 'opus-4.6', label: 'Opus 4.6' },
  { value: 'composer-2', label: 'Composer 2' },
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
    state?: string
    stdout?: string[]
    stderr?: string[]
  } | null
}

function normalizeApprovalMode(value: unknown): CursorApprovalMode {
  if (value === 'auto_edit' || value === 'full_auto') return value
  return 'suggest'
}

function extractFirstUrl(value: string) {
  return value.match(/https?:\/\/\S+/)?.[0]?.replace(/[),.]+$/, '') || ''
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

  const connectionLabel = useMemo(() => {
    if (loading) return 'Checking VM status'
    if (gatewayStatus?.status === 'authenticated') return 'Cursor authenticated on VM'
    if (gatewayStatus?.status === 'login_pending') return 'OAuth waiting for browser approval'
    if (gatewayStatus?.cli?.available === false) return 'Cursor CLI missing on VM'
    return 'OAuth sign-in required'
  }, [gatewayStatus, loading])

  const loginOutput = [
    ...(gatewayStatus?.login_session?.stdout || []),
    ...(gatewayStatus?.login_session?.stderr || []),
  ].slice(-8)

  const applyGatewayStatus = (rawStatus: unknown) => {
    const status = (rawStatus && typeof rawStatus === 'object' ? rawStatus : {}) as CursorGatewayStatus
    const nextModel = String(status.preferred_model ?? 'auto').trim() || 'auto'
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
      setBanner('Cursor OAuth login started on the VM.')
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
            <Terminal size={28} />
          </div>
          <div className="cosmic-agents-detail-hero-text">
            <h3>Cursor CLI for Alpha</h3>
            <p>{connectionLabel}</p>
            <span>Runs Cursor Agent headless on your VM through OAuth-only login.</span>
          </div>
        </div>
        <div className={`cosmic-agents-detail-status-pill ${gatewayStatus?.status === 'authenticated' ? 'ready' : gatewayStatus?.status === 'login_pending' ? 'pending' : 'warn'}`}>
          {gatewayStatus?.status === 'authenticated' ? 'Ready' : gatewayStatus?.status === 'login_pending' ? 'Pending' : 'Setup'}
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
              <button type="button" className="cosmic-agents-detail-btn" onClick={startCursorLogin} disabled={saving}>
                <LogIn size={15} />
                {gatewayStatus?.status === 'login_pending' ? 'Restart' : 'Login'}
              </button>
            )}
          </div>
        </div>

        {loginOutput.length ? (
          <div className="cosmic-agents-detail-login-output">
            {loginOutput.map((line, index) => {
              const url = extractFirstUrl(line)
              return (
                <span key={`${line}-${index}`}>
                  {line}
                  {url ? (
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

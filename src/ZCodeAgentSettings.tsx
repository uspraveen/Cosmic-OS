import { useEffect, useMemo, useRef, useState } from 'react'
import { Bot, CheckCircle2, ExternalLink, KeyRound, LogIn, RefreshCw, ShieldCheck, Terminal, Trash2 } from 'lucide-react'
import {
  describeLoginReason,
  extractLoginUrl,
  loginSessionLines,
  loginStartOutcome,
  stripAnsi,
} from './agentLogin'
import { ZCodeMark } from './brandIcons'

type ZcodeAuthMode = 'oauth' | 'api_key'
type ZcodeThinkingMode = 'auto' | 'low' | 'high' | 'max'

// GLM-5.3 and GLM-5.3-Flash are the whole catalog ZCode ships. Thinking
// follows the family's own low/high/max ladder; Auto leaves the model's
// default (max) in place.
const MODEL_OPTIONS = [
  { value: 'glm-5.3', label: 'GLM-5.3', note: 'Flagship · stable 1M context' },
  { value: 'glm-5.3-flash', label: 'GLM-5.3 Flash', note: 'Fast · multimodal input' },
]

const THINKING_MODES: Array<{ value: ZcodeThinkingMode; label: string; note: string }> = [
  { value: 'auto', label: 'Auto', note: "Model's default" },
  { value: 'low', label: 'Low', note: 'Fastest' },
  { value: 'high', label: 'High', note: 'Balanced' },
  { value: 'max', label: 'Max', note: 'Deepest' },
]

interface ZcodeAgentSettingsProps {
  active: boolean
}

interface ZcodeGatewayStatus {
  auth_mode?: string
  preferred_model?: string
  reasoning_effort?: string
  thinking?: string
  vm_sync_enabled?: boolean
  status?: string
  login_required_reason?: string
  zcode_home?: string
  has_api_key?: boolean
  cli?: {
    available?: boolean
    authenticated?: boolean
    version?: string
    has_api_key?: boolean
    main_model?: string
    reason?: string
  }
  login_session?: {
    session_id?: string
    state?: string
    stdout?: string[]
    stderr?: string[]
  } | null
}

function normalizeAuthMode(value: unknown): ZcodeAuthMode {
  return value === 'oauth' ? 'oauth' : 'api_key'
}

function normalizeZcodeModelOption(value: unknown): string {
  const normalized = String(value ?? '').trim().toLowerCase()
  const bare = normalized.includes('/') ? normalized.split('/')[1] : normalized
  if (bare === 'glm-5.3' || bare === 'glm-5.3-flash') return bare
  return 'glm-5.3-flash'
}

function normalizeThinking(value: unknown): ZcodeThinkingMode {
  const normalized = String(value ?? 'auto').trim().toLowerCase()
  if (normalized === 'low' || normalized === 'high' || normalized === 'max') return normalized
  return 'auto'
}

export default function ZCodeAgentSettings({ active }: ZcodeAgentSettingsProps) {
  const [authMode, setAuthMode] = useState<ZcodeAuthMode>('oauth')
  const [preferredModel, setPreferredModel] = useState('glm-5.3-flash')
  const [thinking, setThinking] = useState<ZcodeThinkingMode>('auto')
  const [vmSyncEnabled, setVmSyncEnabled] = useState(true)
  const [hasApiKey, setHasApiKey] = useState(false)
  const [apiKeyDraft, setApiKeyDraft] = useState('')
  const [gatewayStatus, setGatewayStatus] = useState<ZcodeGatewayStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [banner, setBanner] = useState('')
  const [error, setError] = useState('')
  const modelsSectionRef = useRef<HTMLDivElement | null>(null)
  const openedLoginUrlRef = useRef('')

  useEffect(() => {
    if (!banner) return
    const timer = window.setTimeout(() => setBanner(''), 2600)
    return () => window.clearTimeout(timer)
  }, [banner])

  useEffect(() => {
    if (!error) return
    const timer = window.setTimeout(() => setError(''), 4200)
    return () => window.clearTimeout(timer)
  }, [error])

  useEffect(() => {
    if (!active) return
    let cancelled = false
    const refresh = async () => {
      setLoading(true)
      setError('')
      try {
        const status = await window.cosmic?.getGatewayZcodeStatus()
        if (!cancelled) applyGatewayStatus(status)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Unable to load ZCode status from the VM.')
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
        const status = await window.cosmic?.getGatewayZcodeStatus()
        applyGatewayStatus(status)
      } catch {
        // keep the current pending session visible until the next explicit refresh
      }
    }, 2500)
    return () => window.clearInterval(timer)
  }, [active, gatewayStatus?.status])

  // `zcode login --no-browser` prints its OAuth URL shortly after start and
  // waits for the browser round-trip. Open the URL as soon as it appears —
  // once per login session, so a status poll never reopens the tab.
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
    if (cliMissing) return 'ZCode CLI missing on VM'
    if (gatewayStatus?.status === 'authenticated') return 'Z.ai account connected on VM'
    if (gatewayStatus?.status === 'login_pending') return 'Waiting for Z.ai sign-in approval'
    if (gatewayStatus?.status === 'blocked') return 'VM cannot write the ZCode home'
    if (gatewayStatus?.status === 'update_in_progress') return 'ZCode is updating — Alpha routes around it'
    if (gatewayStatus?.status === 'relogin_required') return 'ZCode needs re-authentication on the VM'
    if (authMode === 'api_key') return hasApiKey ? 'API key saved on VM' : 'API key needed'
    return 'Connect your Z.ai account to use GLM-5.3'
  }, [authMode, cliMissing, gatewayStatus, hasApiKey, loading])

  const authenticated = gatewayStatus?.status === 'authenticated'

  function applyGatewayStatus(rawStatus: unknown) {
    const status = (rawStatus && typeof rawStatus === 'object' ? rawStatus : {}) as ZcodeGatewayStatus
    setGatewayStatus(status)
    setAuthMode(normalizeAuthMode(status.auth_mode))
    setPreferredModel(normalizeZcodeModelOption(status.preferred_model))
    setThinking(normalizeThinking(status.thinking ?? status.reasoning_effort))
    setVmSyncEnabled(status.vm_sync_enabled !== false)
    setHasApiKey(Boolean(status.has_api_key))
  }

  const saveRemoteConfig = async (
    payload: {
      authMode?: ZcodeAuthMode
      apiKey?: string
      preferredModel?: string
      thinking?: ZcodeThinkingMode
      vmSyncEnabled?: boolean
    },
    successMessage?: string,
  ) => {
    setSaving(true)
    setError('')
    try {
      const status = await window.cosmic?.saveGatewayZcodeConfig(payload)
      applyGatewayStatus(status)
      if (successMessage) setBanner(successMessage)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to save ZCode settings to the VM.')
    } finally {
      setSaving(false)
    }
  }

  const saveAuthMode = (nextMode: ZcodeAuthMode) => {
    setAuthMode(nextMode)
    void saveRemoteConfig(
      { authMode: nextMode },
      nextMode === 'oauth'
        ? 'Z.ai sign-in selected for Alpha ZCode.'
        : 'API key mode selected for Alpha ZCode.',
    )
  }

  const saveApiKey = () => {
    const nextKey = apiKeyDraft.trim()
    if (!nextKey) {
      setBanner('Paste a Z.ai or BigModel API key before saving.')
      return
    }
    setApiKeyDraft('')
    void saveRemoteConfig({ apiKey: nextKey }, 'ZCode API key saved on the VM.')
  }

  const startZcodeLogin = async () => {
    setSaving(true)
    setError('')
    try {
      const status = await window.cosmic?.startGatewayZcodeLogin()
      applyGatewayStatus(status)
      // The endpoint answers 200 whether or not it managed to start anything;
      // the truth is in the status it returns.
      const outcome = loginStartOutcome(status as ZcodeGatewayStatus | null, 'ZCode')
      if (outcome.ok) setBanner(outcome.message)
      else setError(outcome.message)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to start ZCode login on the VM.')
    } finally {
      setSaving(false)
    }
  }

  const logoutZcode = async () => {
    setSaving(true)
    setError('')
    try {
      const status = await window.cosmic?.logoutGatewayZcode()
      applyGatewayStatus(status)
      setBanner('ZCode logged out on the VM.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to log ZCode out on the VM.')
    } finally {
      setSaving(false)
    }
  }

  const refreshStatus = async () => {
    setLoading(true)
    setError('')
    try {
      const status = await window.cosmic?.getGatewayZcodeStatus()
      applyGatewayStatus(status)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to refresh ZCode status.')
    } finally {
      setLoading(false)
    }
  }

  // The login session is rendered by its real state: running shows the
  // sign-in URL, failed shows a persistent failure card (status polls must
  // not blink it away), completed-but-unauthenticated asks for a retry.
  const loginSession = gatewayStatus?.login_session ?? null
  const loginState =
    loginSession?.state === 'running' ||
    loginSession?.state === 'failed' ||
    loginSession?.state === 'completed'
      ? loginSession.state
      : ''
  const loginOutput = loginState ? loginSessionLines(loginSession).slice(-8) : []
  const loginUrl = extractLoginUrl(loginOutput)
  const loginFailureLines =
    loginState === 'failed'
      ? loginSessionLines(loginSession)
          .map(stripAnsi)
          .filter((line) => /error|failed|denied|credential|timed out/i.test(line))
          .slice(-3)
      : []
  const currentModel = MODEL_OPTIONS.find((option) => option.value === preferredModel) ?? MODEL_OPTIONS[1]

  return (
    <div className="cosmic-agents-detail-page">
      <div className="cosmic-agents-detail-hero">
        <div className="cosmic-agents-detail-hero-top">
          <div className="cosmic-agents-detail-hero-icon" aria-hidden="true">
            <ZCodeMark size={26} />
          </div>
          <div className="cosmic-agents-detail-hero-text">
            <h3>ZCode for Alpha</h3>
            <p>{connectionLabel}</p>
            <span>Z.ai&apos;s official GLM-5.3 agent, headless on your VM.</span>
          </div>
        </div>
        <div className={`cosmic-agents-detail-status-pill ${authenticated ? 'ready' : cliMissing || gatewayStatus?.status !== 'login_pending' && gatewayStatus?.status !== 'update_in_progress' ? 'warn' : 'pending'}`}>
          {cliMissing
            ? 'Setup'
            : authenticated
              ? 'Ready'
              : gatewayStatus?.status === 'login_pending' || gatewayStatus?.status === 'update_in_progress'
                ? 'Pending'
                : 'Setup'}
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

      {/* ── Active model strip: what Alpha will actually run next ─────── */}
      <div className="zcode-current">
        <span className="zcode-current-chip" data-tone={authenticated ? 'on' : 'off'}>
          <ZCodeMark size={17} />
        </span>
        <div className="zcode-current-main">
          <span className="zcode-current-label">
            {currentModel.label}
            {thinking !== 'auto' ? <span className="zcode-free-pill">{thinking}</span> : null}
          </span>
          <small>
            zai/{currentModel.value}
            {authenticated ? '' : ' · connect Z.ai to run'}
          </small>
        </div>
        <button
          type="button"
          className="zcode-current-change"
          onClick={() => modelsSectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
        >
          Change
        </button>
      </div>

      {/* ── Default model + thinking ───────────────────────────────────── */}
      <div className="cosmic-agents-detail-section cosmic-agents-detail-runner" ref={modelsSectionRef}>
        <div className="cosmic-agents-detail-section-head">
          <div>
            <span className="cosmic-agents-detail-kicker">Default model</span>
            <h4>Pick Alpha&apos;s GLM model</h4>
          </div>
        </div>

        <div className="cosmic-agents-detail-model-grid codex">
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

        <span className="cosmic-agents-detail-control-label">Thinking</span>
        <div className="zcode-variant-seg" role="group" aria-label="Thinking mode">
          {THINKING_MODES.map((mode) => (
            <button
              key={mode.value}
              type="button"
              className={thinking === mode.value ? 'active' : ''}
              onClick={() => {
                setThinking(mode.value)
                void saveRemoteConfig({ thinking: mode.value })
              }}
              disabled={saving}
              title="Applied as the GLM-5.3 family's thinking default on the VM."
            >
              <strong>{mode.label}</strong>
              <small>{mode.note}</small>
            </button>
          ))}
        </div>
      </div>

      {/* ── Z.ai account: sign-in, or fall back to an API key ──────────── */}
      <div className="cosmic-agents-detail-section">
        <div className="cosmic-agents-detail-section-head">
          <div>
            <span className="cosmic-agents-detail-kicker">Z.ai account</span>
            <h4>
              {authMode === 'api_key'
                ? hasApiKey
                  ? 'API key saved on VM'
                  : 'Connect with an API key'
                : authenticated
                  ? 'Signed in on VM'
                  : 'Connect your Z.ai account'}
            </h4>
          </div>
          <div className="cosmic-agents-detail-actions">
            <button type="button" className="cosmic-agents-detail-btn ghost icon" onClick={refreshStatus} disabled={loading || saving} title="Refresh ZCode status">
              <RefreshCw size={15} />
            </button>
            {authMode === 'oauth' && authenticated ? (
              <button type="button" className="cosmic-agents-detail-btn danger icon" onClick={logoutZcode} disabled={saving} title="Log out ZCode">
                <Trash2 size={15} />
              </button>
            ) : null}
          </div>
        </div>

        {authMode === 'api_key' ? (
          <>
            <div className="cosmic-agents-detail-key-row">
              <input
                type="password"
                value={apiKeyDraft}
                onChange={(event) => setApiKeyDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') saveApiKey()
                }}
                placeholder="Z.ai or BigModel API key"
                spellCheck={false}
                autoComplete="off"
              />
              <button type="button" className="cosmic-agents-detail-btn" onClick={saveApiKey} disabled={saving}>
                Save
              </button>
            </div>
            <p className="cosmic-agents-detail-section-copy">
              Uses platform billing instead of a Coding Plan.
              <button type="button" onClick={() => saveAuthMode('oauth')} disabled={saving}>
                Use Z.ai sign-in instead
              </button>
            </p>
          </>
        ) : cliMissing ? (
          <>
            <p className="cosmic-agents-detail-section-copy">{describeLoginReason(gatewayStatus)}</p>
          </>
        ) : authenticated ? (
          <p className="cosmic-agents-detail-section-copy">
            Signed in with your Z.ai account. Your GLM Coding Plan quota applies automatically to Alpha runs.
          </p>
        ) : loginState === 'failed' ? (
          <div className="cosmic-agents-detail-login-output error" role="alert">
            <span className="cosmic-agents-detail-login-error-title">Z.ai sign-in failed on the VM</span>
            <span>{describeLoginReason(gatewayStatus) || 'The sign-in process ended without completing.'}</span>
            {loginFailureLines.length ? (
              loginFailureLines.map((line, index) => <span key={`${line}-${index}`}>{line}</span>)
            ) : null}
          </div>
        ) : loginState === 'running' ? (
          <div className="cosmic-agents-detail-login-output">
            {loginUrl ? (
              <span>
                Sign-in page opened in your browser.
                <button type="button" onClick={() => window.cosmic?.openExternal(loginUrl)}>
                  <ExternalLink size={12} />
                  Open again
                </button>
              </span>
            ) : (
              <span>Starting the Z.ai sign-in on the VM…</span>
            )}
            {loginOutput.map((line, index) => {
              const cleanLine = stripAnsi(line)
              const url = extractLoginUrl([line])
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
        ) : loginState === 'completed' ? (
          <p className="cosmic-agents-detail-section-copy">
            Sign-in finished, but the VM did not report a saved key. Try connecting again.
          </p>
        ) : gatewayStatus?.status === 'blocked' ? (
          <p className="cosmic-agents-detail-section-copy">{describeLoginReason(gatewayStatus)}</p>
        ) : (
          <p className="cosmic-agents-detail-section-copy">
            Starts the Z.ai OAuth sign-in on the VM and opens the approval page in your browser. Your GLM Coding Plan quota applies automatically.
          </p>
        )}

        {authMode === 'oauth' && !authenticated ? (
          <div className="cosmic-agents-detail-actions" style={{ marginTop: 12 }}>
            <button
              type="button"
              className="cosmic-agents-detail-btn"
              onClick={startZcodeLogin}
              disabled={saving || cliMissing}
              title={cliMissing ? 'Install the ZCode CLI on the VM first' : undefined}
            >
              <LogIn size={15} />
              {gatewayStatus?.status === 'login_pending' ? 'Restart sign-in' : 'Connect'}
            </button>
            <button type="button" className="cosmic-agents-detail-btn ghost" onClick={() => saveAuthMode('api_key')} disabled={saving}>
              <KeyRound size={13} />
              Use an API key instead
            </button>
          </div>
        ) : null}
      </div>

      <div className="cosmic-agents-detail-runtime">
        <div className="cosmic-agents-detail-runtime-row">
          <div className="cosmic-agents-detail-runtime-icon" aria-hidden="true">
            <Terminal size={18} />
          </div>
          <div>
            <strong>VM Alpha workspace</strong>
            <span>Headless `zcode --prompt --mode yolo --json` with session resume.</span>
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
        <span>
          {gatewayStatus?.zcode_home
            ? `HOME: ${gatewayStatus.zcode_home}`
            : 'ZCode home is managed on the VM.'}
        </span>
        <span style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          <KeyRound size={12} />
          {gatewayStatus?.cli?.version ? `CLI ${gatewayStatus.cli.version}` : ''}
        </span>
      </div>
    </div>
  )
}

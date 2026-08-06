import { useEffect, useMemo, useState } from 'react'
import { Bot, CheckCircle2, Code2, Copy, ExternalLink, KeyRound, LogIn, ShieldCheck, Terminal, Trash2 } from 'lucide-react'
import { describeLoginReason, loginStartOutcome } from './agentLogin'

type CodexAuthMode = 'chatgpt' | 'api_key'
type CodexApprovalMode = 'suggest' | 'auto_edit' | 'full_auto'
type CodexReasoningEffort = 'auto' | 'low' | 'medium' | 'high' | 'xhigh'

const MODEL_OPTIONS = [
  { value: 'auto', label: 'Auto' },
  { value: 'gpt-5.5', label: 'GPT-5.5' },
  { value: 'gpt-5.4', label: 'GPT-5.4' },
  { value: 'gpt-5.4-mini', label: 'GPT-5.4 Mini' },
  { value: 'gpt-5.3-codex', label: 'GPT-5.3 Codex' },
  { value: 'gpt-5.2', label: 'GPT-5.2' },
]

const APPROVAL_MODES: Array<{ value: CodexApprovalMode; label: string; note: string }> = [
  { value: 'suggest', label: 'Suggest', note: 'Read and propose' },
  { value: 'auto_edit', label: 'Auto Edit', note: 'Write after routing' },
  { value: 'full_auto', label: 'Full Auto', note: 'Container scoped' },
]

const REASONING_OPTIONS: Array<{ value: CodexReasoningEffort; label: string; note: string }> = [
  { value: 'auto', label: 'Auto', note: 'CLI default' },
  { value: 'low', label: 'Low', note: 'Faster' },
  { value: 'medium', label: 'Medium', note: 'Balanced' },
  { value: 'high', label: 'High', note: 'Deeper' },
  { value: 'xhigh', label: 'XHigh', note: 'Maximum' },
]

interface CodexAgentSettingsProps {
  active: boolean
}

interface CodexGatewayStatus {
  auth_mode?: string
  has_api_key?: boolean
  preferred_model?: string
  reasoning_effort?: string
  approval_mode?: string
  vm_sync_enabled?: boolean
  status?: string
  login_required_reason?: string
  codex_home?: string
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

function normalizeAuthMode(value: unknown): CodexAuthMode {
  return value === 'api_key' ? 'api_key' : 'chatgpt'
}

function normalizeApprovalMode(value: unknown): CodexApprovalMode {
  if (value === 'auto_edit' || value === 'full_auto') return value
  return 'suggest'
}

function normalizeReasoningEffort(value: unknown): CodexReasoningEffort {
  if (value === 'low' || value === 'medium' || value === 'high' || value === 'xhigh') return value
  return 'auto'
}

function stripAnsi(value: string) {
  return value
    .replace(/\u001b\[[0-9;]*m/g, '')
    .replace(/\[[0-9;]*m/g, '')
}

function extractFirstUrl(value: string) {
  const clean = stripAnsi(value)
  const url = clean.match(/https?:\/\/auth\.openai\.com\/codex\/device|https?:\/\/[^\s\]]+/)?.[0] || ''
  return url.replace(/[),.;]+$/, '')
}

function extractCodexDeviceLogin(lines: string[]) {
  const cleanLines = lines.map(stripAnsi).map((line) => line.trim()).filter(Boolean)
  const url = cleanLines.map(extractFirstUrl).find(Boolean) || ''
  const code = cleanLines
    .map((line) => line.match(/\b[A-Z0-9]{4,8}-[A-Z0-9]{4,8}\b/)?.[0] || '')
    .find(Boolean) || ''
  const expiry = cleanLines.find((line) => /expires/i.test(line))?.match(/expires[^)]*/i)?.[0] || ''
  const warning = cleanLines.find((line) => /phishing/i.test(line)) || ''
  return { cleanLines, code, expiry, url, warning }
}

export default function CodexAgentSettings({ active }: CodexAgentSettingsProps) {
  const [authMode, setAuthMode] = useState<CodexAuthMode>('chatgpt')
  const [apiKeyDraft, setApiKeyDraft] = useState('')
  const [hasApiKey, setHasApiKey] = useState(false)
  const [preferredModel, setPreferredModel] = useState('auto')
  const [reasoningEffort, setReasoningEffort] = useState<CodexReasoningEffort>('auto')
  const [approvalMode, setApprovalMode] = useState<CodexApprovalMode>('suggest')
  const [vmSyncEnabled, setVmSyncEnabled] = useState(true)
  const [gatewayStatus, setGatewayStatus] = useState<CodexGatewayStatus | null>(null)
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
        const status = await window.cosmic?.getGatewayCodexStatus()
        if (!cancelled) applyGatewayStatus(status)
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Unable to load Codex status from the VM.')
        }
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
        const status = await window.cosmic?.getGatewayCodexStatus()
        applyGatewayStatus(status)
      } catch {
        // keep the current pending session visible until a manual refresh/action succeeds
      }
    }, 2500)
    return () => window.clearInterval(timer)
  }, [active, gatewayStatus?.status])

  const cliMissing = gatewayStatus?.cli?.available === false

  const connectionLabel = useMemo(() => {
    if (loading) return 'Checking VM status'
    if (cliMissing) return 'Codex CLI missing on VM'
    if (gatewayStatus?.status === 'authenticated') return 'Codex authenticated on VM'
    if (gatewayStatus?.status === 'login_pending') return 'Login waiting for browser approval'
    if (gatewayStatus?.status === 'relogin_required') return 'Codex needs re-authentication on the VM'
    if (gatewayStatus?.status === 'login_required') return 'Codex sign-in required on the VM'
    if (authMode === 'api_key') return hasApiKey ? 'API key saved on VM' : 'API key needed'
    return 'ChatGPT sign-in selected'
  }, [authMode, cliMissing, gatewayStatus?.status, hasApiKey, loading])

  const needsReauth = gatewayStatus?.status === 'relogin_required' || gatewayStatus?.status === 'login_required'

  const applyGatewayStatus = (rawStatus: unknown) => {
    const status = (rawStatus && typeof rawStatus === 'object' ? rawStatus : {}) as CodexGatewayStatus
    const nextAuthMode = normalizeAuthMode(status.auth_mode)
    const nextModel = String(status.preferred_model ?? 'auto').trim() || 'auto'

    setGatewayStatus(status)
    setAuthMode(nextAuthMode)
    setHasApiKey(Boolean(status.has_api_key))
    setPreferredModel(MODEL_OPTIONS.some((option) => option.value === nextModel) ? nextModel : 'auto')
    setReasoningEffort(normalizeReasoningEffort(status.reasoning_effort))
    setApprovalMode(normalizeApprovalMode(status.approval_mode))
    setVmSyncEnabled(status.vm_sync_enabled !== false)
  }

  const saveRemoteConfig = async (payload: {
    authMode?: CodexAuthMode
    apiKey?: string
    preferredModel?: string
    reasoningEffort?: CodexReasoningEffort
    approvalMode?: CodexApprovalMode
    vmSyncEnabled?: boolean
  }, successMessage?: string) => {
    setSaving(true)
    setError('')
    try {
      const status = await window.cosmic?.saveGatewayCodexConfig(payload)
      applyGatewayStatus(status)
      if (successMessage) setBanner(successMessage)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to save Codex settings to the VM.')
    } finally {
      setSaving(false)
    }
  }

  const saveAuthMode = (nextMode: CodexAuthMode) => {
    setAuthMode(nextMode)
    void saveRemoteConfig(
      { authMode: nextMode },
      nextMode === 'chatgpt'
        ? 'ChatGPT sign-in selected for Alpha Codex.'
        : 'API key mode selected for Alpha Codex.',
    )
  }

  const savePreferredModel = (nextModel: string) => {
    setPreferredModel(nextModel)
    void saveRemoteConfig({ preferredModel: nextModel })
  }

  const saveReasoningEffort = (nextEffort: CodexReasoningEffort) => {
    setReasoningEffort(nextEffort)
    void saveRemoteConfig({ reasoningEffort: nextEffort })
  }

  const saveApprovalMode = (nextMode: CodexApprovalMode) => {
    setApprovalMode(nextMode)
    void saveRemoteConfig({ approvalMode: nextMode })
  }

  const saveVmSync = (enabled: boolean) => {
    setVmSyncEnabled(enabled)
    void saveRemoteConfig({ vmSyncEnabled: enabled })
  }

  const saveApiKey = () => {
    const nextKey = apiKeyDraft.trim()
    if (!nextKey) {
      setBanner('Paste an OpenAI API key before saving.')
      return
    }
    setApiKeyDraft('')
    setAuthMode('api_key')
    void saveRemoteConfig(
      { authMode: 'api_key', apiKey: nextKey },
      'Codex API key saved to the VM and synced to Codex.',
    )
  }

  const logoutCodex = async () => {
    setSaving(true)
    setError('')
    try {
      const status = await window.cosmic?.logoutGatewayCodex()
      applyGatewayStatus(status)
      setBanner('Codex logged out on the VM.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to log Codex out on the VM.')
    } finally {
      setSaving(false)
    }
  }

  const startChatGptLogin = async () => {
    setSaving(true)
    setError('')
    try {
      const status = await window.cosmic?.startGatewayCodexLogin()
      applyGatewayStatus(status)
      // Same trap the Cursor panel fell into: the endpoint answers 200 even
      // when it refused to start anything. Read the status it returned.
      const outcome = loginStartOutcome(status as CodexGatewayStatus | null, 'Codex')
      if (outcome.ok) setBanner(outcome.message)
      else setError(outcome.message)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to start Codex login on the VM.')
    } finally {
      setSaving(false)
    }
  }

  const clearApiKey = async () => {
    await logoutCodex()
    setApiKeyDraft('')
  }

  const loginOutput = [
    ...(gatewayStatus?.login_session?.stdout || []),
    ...(gatewayStatus?.login_session?.stderr || []),
  ].slice(-12)
  const deviceLogin = extractCodexDeviceLogin(loginOutput)
  const hasDeviceLogin = Boolean(deviceLogin.url || deviceLogin.code)
  const copyDeviceCode = () => {
    if (!deviceLogin.code) return
    void navigator.clipboard?.writeText(deviceLogin.code)
    setBanner('Device code copied.')
  }

  return (
    <div className="cosmic-agents-detail-page">
      <div className="cosmic-agents-detail-hero">
        <div className="cosmic-agents-detail-hero-top">
          <div className="cosmic-agents-detail-hero-icon" aria-hidden="true">
            <Code2 size={28} />
          </div>
          <div className="cosmic-agents-detail-hero-text">
            <h3>Codex for Alpha</h3>
            <p>{connectionLabel}</p>
            <span>Feeds the VM Alpha agent runner when backend handoff is enabled.</span>
          </div>
        </div>
        <div className={`cosmic-agents-detail-status-pill ${needsReauth ? 'warn' : gatewayStatus?.status === 'login_pending' ? 'pending' : authMode === 'api_key' && !hasApiKey ? 'warn' : 'ready'}`}>
          {needsReauth
            ? 'Reauth'
            : gatewayStatus?.status === 'login_pending'
              ? 'Pending'
              : authMode === 'api_key' && !hasApiKey
                ? 'Setup'
                : 'Ready'}
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
            <span className="cosmic-agents-detail-kicker">Authentication</span>
            <h4>Choose how Alpha will authenticate Codex</h4>
          </div>
        </div>

        <div className="cosmic-agents-detail-auth-grid">
          <button
            type="button"
            className={`cosmic-agents-detail-auth-card ${authMode === 'chatgpt' ? 'active' : ''}`}
            onClick={() => saveAuthMode('chatgpt')}
            disabled={saving}
          >
            <LogIn size={20} />
            <strong>ChatGPT sign-in</strong>
            <span>Uses Codex CLI OAuth/subscription access.</span>
            {authMode === 'chatgpt' ? <CheckCircle2 size={17} className="cosmic-agents-detail-card-check" /> : null}
          </button>

          <button
            type="button"
            className={`cosmic-agents-detail-auth-card ${authMode === 'api_key' ? 'active' : ''}`}
            onClick={() => saveAuthMode('api_key')}
            disabled={saving}
          >
            <KeyRound size={20} />
            <strong>OpenAI API key</strong>
            <span>Uses platform billing through OPENAI_API_KEY.</span>
            {authMode === 'api_key' ? <CheckCircle2 size={17} className="cosmic-agents-detail-card-check" /> : null}
          </button>
        </div>
      </div>

      <div className="cosmic-agents-detail-section">
        <div className="cosmic-agents-detail-section-head">
          <div>
            <span className="cosmic-agents-detail-kicker">API key</span>
            <h4>{hasApiKey ? 'Saved on VM' : 'No key saved'}</h4>
          </div>
          {hasApiKey ? (
            <button type="button" className="cosmic-agents-detail-btn danger" onClick={clearApiKey} title="Clear saved API key" disabled={saving}>
              <Trash2 size={16} />
            </button>
          ) : null}
        </div>
        <div className="cosmic-agents-detail-key-row">
          <input
            type="password"
            value={apiKeyDraft}
            onChange={(event) => setApiKeyDraft(event.target.value)}
            placeholder="sk-..."
            spellCheck={false}
            autoComplete="off"
          />
          <button type="button" className="cosmic-agents-detail-btn" onClick={saveApiKey} disabled={saving}>
            Save
          </button>
        </div>
      </div>

      {authMode === 'chatgpt' ? (
        <div className="cosmic-agents-detail-section">
          <div className="cosmic-agents-detail-section-head">
            <div>
              <span className="cosmic-agents-detail-kicker">ChatGPT login</span>
              <h4>{gatewayStatus?.status === 'authenticated' ? 'Signed in on VM' : 'VM sign-in session'}</h4>
            </div>
            <button
              type="button"
              className="cosmic-agents-detail-btn"
              onClick={startChatGptLogin}
              disabled={saving || cliMissing}
              title={cliMissing ? 'Install the Codex CLI on the VM first' : undefined}
            >
              {gatewayStatus?.status === 'login_pending' ? 'Restart' : 'Login'}
            </button>
          </div>
          {cliMissing ? (
            <p className="cosmic-agents-detail-section-copy">{describeLoginReason(gatewayStatus)}</p>
          ) : hasDeviceLogin ? (
            <div className="cosmic-agents-login-card">
              <div className="cosmic-agents-login-card-head">
                <div>
                  <span className="cosmic-agents-detail-kicker">Device authorization</span>
                  <h5>Sign in with ChatGPT</h5>
                </div>
                {deviceLogin.expiry ? <span className="cosmic-agents-login-expiry">{deviceLogin.expiry}</span> : null}
              </div>
              <p className="cosmic-agents-login-card-copy">
                Open the Codex sign-in page, then enter the one-time code shown here.
              </p>
              <div className="cosmic-agents-login-actions">
                {deviceLogin.url ? (
                  <button type="button" className="cosmic-agents-login-open" onClick={() => window.cosmic?.openExternal(deviceLogin.url)}>
                    <ExternalLink size={14} />
                    Open sign-in
                  </button>
                ) : null}
                {deviceLogin.code ? (
                  <button type="button" className="cosmic-agents-login-code" onClick={copyDeviceCode} title="Copy device code">
                    <KeyRound size={14} />
                    <span>{deviceLogin.code}</span>
                    <Copy size={13} />
                  </button>
                ) : null}
              </div>
              <div className="cosmic-agents-login-warning">
                <ShieldCheck size={13} />
                <span>{deviceLogin.warning || 'Only enter this code on the official OpenAI authorization page.'}</span>
              </div>
              {deviceLogin.cleanLines.length ? (
                <details className="cosmic-agents-login-raw">
                  <summary>Raw CLI output</summary>
                  <div className="cosmic-agents-detail-login-output">
                    {deviceLogin.cleanLines.map((line, index) => (
                      <span key={`${line}-${index}`}>{line}</span>
                    ))}
                  </div>
                </details>
              ) : null}
            </div>
          ) : loginOutput.length ? (
            <div className="cosmic-agents-detail-login-output">
              {loginOutput.map((line, index) => {
                const url = extractFirstUrl(line)
                const cleanLine = stripAnsi(line)
                return (
                  <span key={`${line}-${index}`}>
                    {cleanLine}
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
              Starts codex login --device-auth on the VM and keeps the session visible here for browser approval or re-login.
            </p>
          )}
        </div>
      ) : null}

      <div className="cosmic-agents-detail-section cosmic-agents-detail-runner">
        <div className="cosmic-agents-detail-section-head">
          <div>
            <span className="cosmic-agents-detail-kicker">Runner defaults</span>
            <h4>Model and autonomy</h4>
          </div>
        </div>

        <span className="cosmic-agents-detail-control-label">Model</span>
        <div className="cosmic-agents-detail-model-grid codex">
          {MODEL_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              className={`cosmic-agents-detail-model-card ${preferredModel === option.value ? 'active' : ''}`}
              onClick={() => savePreferredModel(option.value)}
              disabled={saving}
            >
              <span className="cosmic-agents-detail-model-name">{option.label}</span>
              {preferredModel === option.value && (
                <span className="cosmic-agents-detail-model-check" aria-hidden="true">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
                </span>
              )}
            </button>
          ))}
        </div>

        <span className="cosmic-agents-detail-control-label">Reasoning effort</span>
        <div className="cosmic-agents-detail-model-grid compact">
          {REASONING_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              className={`cosmic-agents-detail-model-card ${reasoningEffort === option.value ? 'active' : ''}`}
              onClick={() => saveReasoningEffort(option.value)}
              disabled={saving}
            >
              <span className="cosmic-agents-detail-model-name">{option.label}</span>
              <small>{option.note}</small>
              {reasoningEffort === option.value ? <CheckCircle2 size={16} className="cosmic-agents-detail-model-check-icon" /> : null}
            </button>
          ))}
        </div>

        <span className="cosmic-agents-detail-control-label">Autonomy</span>
        <div className="cosmic-agents-detail-mode-list">
          {APPROVAL_MODES.map((mode) => (
            <button
              key={mode.value}
              type="button"
              className={`cosmic-agents-detail-mode ${approvalMode === mode.value ? 'active' : ''}`}
              onClick={() => saveApprovalMode(mode.value)}
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
            <strong>VM Alpha container</strong>
            <span>Codex will run inside per-task Alpha workspaces.</span>
          </div>
        </div>
        <button
          type="button"
          className={`cosmic-agents-detail-sync-btn ${vmSyncEnabled ? 'active' : ''}`}
          onClick={() => saveVmSync(!vmSyncEnabled)}
          disabled={saving}
        >
          <ShieldCheck size={15} />
          {vmSyncEnabled ? 'Sync on' : 'Sync off'}
        </button>
      </div>

      <div className="cosmic-agents-detail-footnote">
        <Bot size={14} />
        <span>{gatewayStatus?.codex_home ? `CODEX_HOME: ${gatewayStatus.codex_home}` : 'Alpha Codex home is managed on the VM.'}</span>
      </div>
    </div>
  )
}

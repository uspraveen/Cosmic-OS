import { useEffect, useMemo, useState } from 'react'
import { Bot, CheckCircle2, KeyRound, RefreshCw, ShieldCheck, Sparkles, Trash2 } from 'lucide-react'
import { describeLoginReason } from './agentLogin'

type OpenCodeModel = {
  id: string
  label: string
  qualified: string
  free: boolean
}

interface OpenCodeAgentSettingsProps {
  active: boolean
}

interface OpenCodeGatewayStatus {
  preferred_model?: string
  vm_sync_enabled?: boolean
  status?: string
  login_required_reason?: string
  opencode_home?: string
  has_api_key?: boolean
  cli?: {
    available?: boolean
    version?: string
    reason?: string
  }
}

function prettyModelLabel(id: string) {
  return id
    .split('-')
    .map((part) =>
      /^(v?\d)/.test(part) || part === 'pro' || part === 'max' || part === 'mini' || part === 'nano' || part === 'free'
        ? part.toUpperCase()
        : part.charAt(0).toUpperCase() + part.slice(1),
    )
    .join(' ')
}

export default function OpenCodeAgentSettings({ active }: OpenCodeAgentSettingsProps) {
  const [preferredModel, setPreferredModel] = useState('mimo-v2.5-free')
  const [hasApiKey, setHasApiKey] = useState(false)
  const [apiKeyDraft, setApiKeyDraft] = useState('')
  const [vmSyncEnabled, setVmSyncEnabled] = useState(true)
  const [gatewayStatus, setGatewayStatus] = useState<OpenCodeGatewayStatus | null>(null)
  const [models, setModels] = useState<OpenCodeModel[]>([])
  const [modelsSource, setModelsSource] = useState('')
  const [modelsFetchedAt, setModelsFetchedAt] = useState('')
  const [modelsLoading, setModelsLoading] = useState(false)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [banner, setBanner] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (!banner) return
    const timer = window.setTimeout(() => setBanner(''), 2600)
    return () => window.clearTimeout(timer)
  }, [banner])

  const loadModels = async (forceRefresh: boolean) => {
    setModelsLoading(true)
    try {
      const payload = await window.cosmic?.getGatewayOpenCodeModels({ forceRefresh })
      if (Array.isArray(payload?.models)) {
        setModels(payload.models)
        setModelsSource(String(payload.source || ''))
        setModelsFetchedAt(String(payload.fetched_at || ''))
      }
    } catch {
      // Keep whatever catalog is already on screen; the banner stays honest.
      if (forceRefresh) setError('Unable to refresh the Zen model list from the VM.')
    } finally {
      setModelsLoading(false)
    }
  }

  useEffect(() => {
    if (!active) return
    let cancelled = false
    const refresh = async () => {
      setLoading(true)
      setError('')
      try {
        const status = await window.cosmic?.getGatewayOpenCodeStatus()
        if (!cancelled) applyGatewayStatus(status)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Unable to load OpenCode status from the VM.')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void refresh()
    void loadModels(false)
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active])

  const cliMissing = gatewayStatus?.cli?.available === false

  const connectionLabel = useMemo(() => {
    if (loading) return 'Checking VM status'
    if (cliMissing) return 'OpenCode CLI missing on VM'
    if (gatewayStatus?.status === 'authenticated') return 'OpenCode ready on VM'
    if (gatewayStatus?.status === 'update_in_progress') return 'OpenCode is updating — Alpha routes around it'
    if (hasApiKey) return 'Zen API key saved'
    return 'OpenCode Zen API key needed'
  }, [cliMissing, gatewayStatus?.status, hasApiKey, loading])

  const applyGatewayStatus = (rawStatus: unknown) => {
    const status = (rawStatus && typeof rawStatus === 'object' ? rawStatus : {}) as OpenCodeGatewayStatus
    const nextModel = String(status.preferred_model ?? 'mimo-v2.5-free').trim() || 'mimo-v2.5-free'
    setGatewayStatus(status)
    setHasApiKey(Boolean(status.has_api_key))
    setPreferredModel(nextModel.replace(/^opencode\//, ''))
    setVmSyncEnabled(status.vm_sync_enabled !== false)
  }

  const saveRemoteConfig = async (
    payload: { preferredModel?: string; vmSyncEnabled?: boolean; apiKey?: string },
    successMessage?: string,
  ) => {
    setSaving(true)
    setError('')
    try {
      const status = await window.cosmic?.saveGatewayOpenCodeConfig(payload)
      applyGatewayStatus(status)
      if (successMessage) setBanner(successMessage)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to save OpenCode settings to the VM.')
    } finally {
      setSaving(false)
    }
  }

  const savePreferredModel = (modelId: string) => {
    setPreferredModel(modelId)
    void saveRemoteConfig({ preferredModel: modelId })
  }

  const saveApiKey = () => {
    const nextKey = apiKeyDraft.trim()
    if (!nextKey) {
      setBanner('Paste an OpenCode Zen API key before saving.')
      return
    }
    setApiKeyDraft('')
    void saveRemoteConfig(
      { apiKey: nextKey },
      'Zen API key saved to the VM. OpenCode will use it on the next Alpha run.',
    )
  }

  const clearApiKey = async () => {
    setSaving(true)
    setError('')
    try {
      const status = await window.cosmic?.logoutGatewayOpenCode()
      applyGatewayStatus(status)
      setBanner('OpenCode Zen key cleared on the VM.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to clear the Zen API key on the VM.')
    } finally {
      setSaving(false)
    }
  }

  // Any live model id not present in the current grid still gets a card, so a
  // saved model that disappears from the catalog remains visible/selectable.
  const visibleModels = useMemo(() => {
    const map = new Map(models.map((model) => [model.id, model]))
    if (preferredModel && !map.has(preferredModel)) {
      map.set(preferredModel, {
        id: preferredModel,
        label: prettyModelLabel(preferredModel),
        qualified: `opencode/${preferredModel}`,
        free: preferredModel.endsWith('-free') || preferredModel === 'big-pickle',
      })
    }
    const list = [...map.values()]
    const knownIds = models.length ? new Set(models.map((m) => m.id)) : new Set<string>()
    return list.sort((a, b) => {
      const aUnknown = models.length > 0 && !knownIds.has(a.id) ? 1 : 0
      const bUnknown = models.length > 0 && !knownIds.has(b.id) ? 1 : 0
      if (a.free !== b.free) return a.free ? -1 : 1
      if (aUnknown !== bUnknown) return aUnknown - bUnknown
      return a.id.localeCompare(b.id)
    })
  }, [models, preferredModel])

  return (
    <div className="cosmic-agents-detail-page">
      <div className="cosmic-agents-detail-hero">
        <div className="cosmic-agents-detail-hero-top">
          <div className="cosmic-agents-detail-hero-icon" aria-hidden="true">
            <Sparkles size={28} />
          </div>
          <div className="cosmic-agents-detail-hero-text">
            <h3>OpenCode for Alpha</h3>
            <p>{connectionLabel}</p>
            <span>Runs OpenCode headlessly in per-task workspaces using OpenCode Zen models.</span>
          </div>
        </div>
        <div className={`cosmic-agents-detail-status-pill ${gatewayStatus?.status === 'authenticated' ? 'ready' : gatewayStatus?.status === 'update_in_progress' ? 'pending' : 'warn'}`}>
          {cliMissing
            ? 'Setup'
            : gatewayStatus?.status === 'authenticated'
              ? 'Ready'
              : gatewayStatus?.status === 'update_in_progress'
                ? 'Updating'
                : hasApiKey
                  ? 'Ready'
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

      <div className="cosmic-agents-detail-section">
        <div className="cosmic-agents-detail-section-head">
          <div>
            <span className="cosmic-agents-detail-kicker">OpenCode Zen</span>
            <h4>{hasApiKey ? 'API key saved on VM' : 'Paste your Zen API key'}</h4>
          </div>
          {hasApiKey ? (
            <button type="button" className="cosmic-agents-detail-btn danger icon" onClick={clearApiKey} title="Clear saved Zen API key" disabled={saving}>
              <Trash2 size={16} />
            </button>
          ) : null}
        </div>
        <p className="cosmic-agents-detail-section-copy">
          Sign in at opencode.ai/auth, copy your Zen API key, and paste it here. Keys are stored encrypted on the VM and
          injected into OpenCode runs only.
        </p>
        <div className="cosmic-agents-detail-key-row">
          <input
            type="password"
            value={apiKeyDraft}
            onChange={(event) => setApiKeyDraft(event.target.value)}
            placeholder="sk-zen-..."
            spellCheck={false}
            autoComplete="off"
          />
          <button type="button" className="cosmic-agents-detail-btn" onClick={saveApiKey} disabled={saving}>
            Save
          </button>
        </div>
        {!hasApiKey ? (
          <p className="cosmic-agents-detail-section-copy">{describeLoginReason(gatewayStatus)}</p>
        ) : null}
      </div>

      <div className="cosmic-agents-detail-section cosmic-agents-detail-runner">
        <div className="cosmic-agents-detail-section-head">
          <div>
            <span className="cosmic-agents-detail-kicker">Runner defaults</span>
            <h4>Zen model catalog</h4>
          </div>
          <button
            type="button"
            className="cosmic-agents-detail-btn ghost icon"
            onClick={() => void loadModels(true)}
            disabled={modelsLoading || saving}
            title="Refresh the Zen model list now"
          >
            <RefreshCw size={15} className={modelsLoading ? 'spin' : ''} />
          </button>
        </div>

        <div className="cosmic-agents-detail-model-grid compact">
          {visibleModels.map((option) => (
            <button
              key={option.id}
              type="button"
              className={`cosmic-agents-detail-model-card ${preferredModel === option.id ? 'active' : ''}`}
              onClick={() => savePreferredModel(option.id)}
              disabled={saving}
            >
              <span className="cosmic-agents-detail-model-name">{prettyModelLabel(option.id)}</span>
              <small>{option.free ? 'Free on Zen' : 'Pay-per-token'}</small>
              {preferredModel === option.id ? <CheckCircle2 size={16} className="cosmic-agents-detail-model-check-icon" /> : null}
            </button>
          ))}
        </div>
        <p className="cosmic-agents-detail-section-copy">
          {modelsLoading
            ? 'Refreshing OpenCode Zen catalog…'
            : modelsSource
              ? `${models.length} models · ${modelsSource === 'live' ? 'live from opencode.ai/zen' : modelsSource.startsWith('cache') ? 'cached catalog' : 'offline fallback'}${modelsFetchedAt ? ` · ${new Date(modelsFetchedAt).toLocaleString()}` : ''}`
              : 'Auto-refreshes every few hours from the live Zen catalog.'}
        </p>
      </div>

      <div className="cosmic-agents-detail-runtime">
        <div className="cosmic-agents-detail-runtime-row">
          <div className="cosmic-agents-detail-runtime-icon" aria-hidden="true">
            <Bot size={18} />
          </div>
          <div>
            <strong>VM Alpha workspace</strong>
            <span>Headless `opencode run --auto` inside per-task workspaces with session resume.</span>
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
          {gatewayStatus?.opencode_home
            ? `HOME: ${gatewayStatus.opencode_home}`
            : 'OpenCode home is managed on the VM.'}
        </span>
        <span style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          <KeyRound size={12} />
          {gatewayStatus?.cli?.version ? `CLI ${gatewayStatus.cli.version}` : ''}
        </span>
      </div>
    </div>
  )
}

import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Bot,
  CheckCircle2,
  ChevronDown,
  KeyRound,
  Lock,
  Plug,
  RefreshCw,
  Search,
  ShieldCheck,
  X,
} from 'lucide-react'
import { OpenAIMark, OpenCodeMark } from './brandIcons'

function CurrentProviderMark({ providerId }: { providerId?: string | null }) {
  const pid = (providerId || 'opencode').toLowerCase()
  if (pid === 'opencode') return <OpenCodeMark size={17} tone="light" />
  if (pid === 'openai') return <OpenAIMark size={16} />
  return <span className="opencode-current-glyph">{(providerId || 'O').charAt(0).toUpperCase()}</span>
}

type OpenCodeModel = {
  id: string
  label: string
  qualified: string
  free: boolean
  usable: boolean
}

type OpenCodeModelGroup = {
  id: string
  label: string
  keyless_free_tier?: boolean
  connected?: boolean
  models: OpenCodeModel[]
}

type OpenCodeProviderTile = {
  id: string
  label: string
  recommended: boolean
  needs_key: boolean
  local_only?: boolean
  connected: boolean
}

interface OpenCodeAgentSettingsProps {
  active: boolean
}

interface OpenCodeGatewayStatus {
  preferred_model?: string
  reasoning_effort?: string
  vm_sync_enabled?: boolean
  status?: string
  login_required_reason?: string
  opencode_home?: string
  has_api_key?: boolean
  connected_providers?: string[]
  cli?: {
    available?: boolean
    version?: string
    reason?: string
  }
}

type VariantOption = 'auto' | 'low' | 'medium' | 'high' | 'xhigh'

const VARIANT_OPTIONS: Array<{ value: VariantOption; label: string; note: string }> = [
  { value: 'auto', label: 'Auto', note: "Model's default" },
  { value: 'low', label: 'Low', note: 'Faster' },
  { value: 'medium', label: 'Medium', note: 'Balanced' },
  { value: 'high', label: 'High', note: 'Deeper' },
  { value: 'xhigh', label: 'XHigh', note: 'Max' },
]

const MODEL_LIST_PREVIEW = 10

export default function OpenCodeAgentSettings({ active }: OpenCodeAgentSettingsProps) {
  const [preferredModel, setPreferredModel] = useState('mimo-v2.5-free')
  const [variant, setVariant] = useState<VariantOption>('auto')
  const [vmSyncEnabled, setVmSyncEnabled] = useState(true)
  const [gatewayStatus, setGatewayStatus] = useState<OpenCodeGatewayStatus | null>(null)
  const [groups, setGroups] = useState<OpenCodeModelGroup[]>([])
  const [providerTiles, setProviderTiles] = useState<OpenCodeProviderTile[]>([])
  const [connectedProviders, setConnectedProviders] = useState<string[]>([])
  const [catalogSource, setCatalogSource] = useState('')
  const [usableTotal, setUsableTotal] = useState(0)
  const [catalogLoading, setCatalogLoading] = useState(false)
  const [modelQuery, setModelQuery] = useState('')
  const [openGroupId, setOpenGroupId] = useState<string | null>(null)
  const [groupModelsExpanded, setGroupModelsExpanded] = useState(false)
  const [connectTarget, setConnectTarget] = useState<string | null>(null)
  const [keyDrafts, setKeyDrafts] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [banner, setBanner] = useState('')
  const [error, setError] = useState('')
  const modelsSectionRef = useRef<HTMLDivElement | null>(null)

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

  const loadCatalog = async (forceRefresh: boolean) => {
    setCatalogLoading(true)
    try {
      const payload = await window.cosmic?.getGatewayOpenCodeCatalog?.({ forceRefresh })
      if (!payload) return
      const nextGroups: OpenCodeModelGroup[] = Array.isArray(payload.groups) ? payload.groups : []
      setGroups(nextGroups)
      setConnectedProviders(Array.isArray(payload.connected_providers) ? payload.connected_providers : [])
      setCatalogSource(String(payload.source || ''))
      setUsableTotal(Number(payload.usable_models || 0))
      setOpenGroupId((prev) => {
        if (prev && nextGroups.some((group) => group.id === prev)) return prev
        const owning = nextGroups.find((group) =>
          group.models.some((model) => model.id === preferredModel),
        )
        if (owning) return owning.id
        const firstUsable = nextGroups.find((group) => group.connected && group.models.length)
        return firstUsable?.id ?? nextGroups[0]?.id ?? null
      })
    } catch {
      if (forceRefresh) setError('Could not refresh the model catalog from the VM.')
    } finally {
      setCatalogLoading(false)
    }
  }

  const loadProviders = async () => {
    try {
      const payload = await window.cosmic?.getGatewayOpenCodeProviders?.()
      if (Array.isArray(payload?.providers)) {
        setProviderTiles(payload.providers)
      }
    } catch {
      // Tiles are additive; status stays usable without them.
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
    void loadProviders()
    void loadCatalog(false)
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active])

  const cliMissing = gatewayStatus?.cli?.available === false

  const connectionLabel = useMemo(() => {
    if (loading) return 'Checking VM status'
    if (cliMissing) return 'OpenCode CLI missing on VM'
    if (gatewayStatus?.status === 'update_in_progress') return 'OpenCode is updating — Alpha routes around it'
    if (connectedProviders.length > 1) return `Ready · ${connectedProviders.length} providers connected`
    return 'Ready · free Zen models run without a key'
  }, [cliMissing, connectedProviders.length, gatewayStatus?.status, loading])

  function applyGatewayStatus(rawStatus: unknown) {
    const status = (rawStatus && typeof rawStatus === 'object' ? rawStatus : {}) as OpenCodeGatewayStatus
    const nextModel = String(status.preferred_model ?? 'mimo-v2.5-free').trim() || 'mimo-v2.5-free'
    setGatewayStatus(status)
    setPreferredModel(nextModel.replace(/^opencode\//, ''))
    const effort = String(status.reasoning_effort ?? 'auto')
    setVariant(
      effort === 'minimal' ? 'low' : (['auto', 'low', 'medium', 'high', 'xhigh'].includes(effort) ? effort as VariantOption : 'auto'),
    )
    setVmSyncEnabled(status.vm_sync_enabled !== false)
  }

  const saveRemoteConfig = async (
    payload: { preferredModel?: string; vmSyncEnabled?: boolean; variant?: string },
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

  const handleModelPick = (group: OpenCodeModelGroup, model: OpenCodeModel) => {
    if (!model.usable) {
      setError(
        `${group.label} is not connected yet. Connect it in Providers below to use ${model.label}.`,
      )
      return
    }
    const bare = model.qualified.includes('/') ? model.qualified.split('/')[1] : model.id
    setPreferredModel(bare)
    void saveRemoteConfig({ preferredModel: bare })
  }

  const saveVariant = (nextVariant: VariantOption) => {
    setVariant(nextVariant)
    void saveRemoteConfig({ variant: nextVariant })
  }

  const beginConnect = (providerId: string) => {
    setConnectTarget(providerId)
    setKeyDrafts((prev) => ({ ...prev, [providerId]: '' }))
    setError('')
  }

  const scrollToProviders = () => {
    document.getElementById('opencode-providers-section')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  const submitConnect = async () => {
    if (!connectTarget) return
    const key = (keyDrafts[connectTarget] || '').trim()
    if (!key) {
      setBanner('Paste the API key for this provider first.')
      return
    }
    setSaving(true)
    setError('')
    try {
      await window.cosmic?.connectGatewayOpenCodeProvider({ providerId: connectTarget, apiKey: key })
      setConnectTarget(null)
      setBanner(`${connectTarget} connected — its models are now usable.`)
      await Promise.all([loadProviders(), loadCatalog(true)])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to connect the provider on the VM.')
    } finally {
      setSaving(false)
    }
  }

  const disconnectProvider = async (providerId: string) => {
    setSaving(true)
    setError('')
    try {
      await window.cosmic?.disconnectGatewayOpenCodeProvider(providerId)
      setBanner(`${providerId} disconnected.`)
      await Promise.all([loadProviders(), loadCatalog(true)])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to disconnect the provider.')
    } finally {
      setSaving(false)
    }
  }

  const query = modelQuery.trim().toLowerCase()
  const searchHits = useMemo(() => {
    if (!query) return [] as Array<OpenCodeModel & { groupId: string; groupLabel: string; groupConnected: boolean }>
    const hits: Array<OpenCodeModel & { groupId: string; groupLabel: string; groupConnected: boolean }> = []
    for (const group of groups) {
      for (const model of group.models) {
        if (
          model.id.toLowerCase().includes(query) ||
          model.label.toLowerCase().includes(query) ||
          `${group.id}/${model.id}`.toLowerCase().includes(query)
        ) {
          hits.push({ ...model, groupId: group.id, groupLabel: group.label, groupConnected: Boolean(group.connected) })
        }
      }
    }
    return hits
  }, [groups, query])

  const openGroup = useMemo(
    () => groups.find((group) => group.id === openGroupId) || null,
    [groups, openGroupId],
  )

  useEffect(() => {
    if (!openGroupId) {
      setGroupModelsExpanded(false)
      return
    }
    const group = groups.find((item) => item.id === openGroupId)
    if (!group || group.models.length <= MODEL_LIST_PREVIEW) {
      setGroupModelsExpanded(false)
      return
    }
    const preferredIndex = group.models.findIndex((model) => model.id === preferredModel)
    setGroupModelsExpanded(preferredIndex >= MODEL_LIST_PREVIEW)
  }, [openGroupId, groups, preferredModel])

  const visibleGroupModels = useMemo(() => {
    if (!openGroup) return []
    if (groupModelsExpanded || openGroup.models.length <= MODEL_LIST_PREVIEW) {
      return openGroup.models
    }
    return openGroup.models.slice(0, MODEL_LIST_PREVIEW)
  }, [openGroup, groupModelsExpanded])

  // The strip under the hero: what Alpha will actually run next.
  const currentModel = useMemo(() => {
    for (const group of groups) {
      const match = group.models.find((model) => model.id === preferredModel)
      if (match) return { model: match, group }
    }
    return {
      model: {
        id: preferredModel,
        label: preferredModel.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()),
        qualified: `opencode/${preferredModel}`,
        free: preferredModel.endsWith("-free") || preferredModel === "big-pickle",
        usable: true,
      },
      group: null as OpenCodeModelGroup | null,
    }
  }, [groups, preferredModel])

  const openedGroupRef = useRef<HTMLDivElement | null>(null)

  const openProviderGroup = (groupId: string | null) => {
    setModelQuery("")
    setOpenGroupId(groupId)
    if (groupId) {
      // Bring the lineup into view: the next step happens down there.
      window.setTimeout(() => {
        openedGroupRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" })
      }, 60)
    }
  }

  const toggleGroup = (groupId: string) => {
    if (openGroupId === groupId) setOpenGroupId(null)
    else openProviderGroup(groupId)
  }

  return (
    <div className="cosmic-agents-detail-page">
      <div className="cosmic-agents-detail-hero">
        <div className="cosmic-agents-detail-hero-top">
          <div className="cosmic-agents-detail-hero-icon" aria-hidden="true">
            <OpenCodeMark size={26} />
          </div>
          <div className="cosmic-agents-detail-hero-text">
            <h3>OpenCode for Alpha</h3>
            <p>{connectionLabel}</p>
            <span>The GUI for `/models` and `/connect` — live from your VM's OpenCode.</span>
          </div>
        </div>
        <div className={`cosmic-agents-detail-status-pill ${gatewayStatus?.status === 'update_in_progress' ? 'pending' : cliMissing ? 'warn' : 'ready'}`}>
          {cliMissing
            ? 'Setup'
            : gatewayStatus?.status === 'update_in_progress'
              ? 'Updating'
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
          {error.includes('not connected') ? (
            <button type="button" className="cosmic-agents-detail-banner-action" onClick={scrollToProviders}>
              Go to Providers
            </button>
          ) : null}
        </div>
      ) : null}

      {/* ── Active model strip: solid black card, provider-marked ──────── */}
      <div className="opencode-current">
        <span className="opencode-current-chip" data-tone={currentModel.model.usable ? 'on' : 'off'}>
          <CurrentProviderMark providerId={currentModel.group?.id} />
        </span>
        <div className="opencode-current-main">
          <span className="opencode-current-label">
            {currentModel.model.label}
            {currentModel.model.free ? <span className="opencode-free-pill">Free</span> : null}
          </span>
          <small>
            {currentModel.group ? currentModel.group.label : 'OpenCode Zen'} · {currentModel.model.qualified}
          </small>
        </div>
        <button
          type="button"
          className="opencode-current-change"
          onClick={() => {
            if (currentModel.group) openProviderGroup(currentModel.group.id)
            else openProviderGroup(groups[0]?.id ?? null)
            modelsSectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
          }}
        >
          Change
        </button>
      </div>

      {/* ── Default model (GUI of /models): browse everything, pick what's usable ── */}
      <div className="cosmic-agents-detail-section" ref={modelsSectionRef}>
        <div className="cosmic-agents-detail-section-head">
          <div>
            <span className="cosmic-agents-detail-kicker">Default model</span>
            <h4>Browse every provider, pick Alpha&apos;s model</h4>
          </div>
          <button
            type="button"
            className="cosmic-agents-detail-btn ghost icon"
            onClick={() => void loadCatalog(true)}
            disabled={catalogLoading || saving}
            title="Refresh the live model catalog now"
          >
            <RefreshCw size={15} className={catalogLoading ? 'spin' : ''} />
          </button>
        </div>

        <div className="opencode-model-toolbar">
          <div className="opencode-model-search">
            <Search size={14} />
            <input
              type="text"
              value={modelQuery}
              onChange={(event) => setModelQuery(event.target.value)}
              placeholder="Search e.g. opus, mimo, gpt…"
              spellCheck={false}
            />
            {modelQuery ? (
              <button type="button" className="opencode-search-clear" onClick={() => setModelQuery('')} title="Clear search">
                <X size={12} />
              </button>
            ) : null}
          </div>
          <span className="opencode-model-count">
            {catalogLoading
              ? 'Refreshing…'
              : query
                ? `${searchHits.length} match${searchHits.length === 1 ? '' : 'es'}`
                : `${usableTotal || groups.reduce((n, g) => n + g.models.filter((m) => m.usable).length, 0)} usable`}
          </span>
        </div>

        {query ? (
          <div className="opencode-hit-list">
            {searchHits.map((model) => (
              <button
                key={`${model.groupId}/${model.id}`}
                type="button"
                className={`opencode-hit ${!model.usable ? 'locked' : ''}`}
                onClick={() => {
                  const group = groups.find((g) => g.id === model.groupId)
                  if (group) handleModelPick(group, model)
                }}
                disabled={saving}
              >
                <span className="opencode-hit-provider">{model.groupLabel}</span>
                <span className="opencode-hit-body">
                  <span className="opencode-hit-name">{model.label}</span>
                  <small>{model.qualified}</small>
                </span>
                <span className="opencode-row-right">
                  {model.free ? <span className="opencode-free-pill">Free</span> : null}
                  <span className="opencode-row-icon">
                    {!model.usable
                      ? <Lock size={12} />
                      : preferredModel === model.id
                        ? <CheckCircle2 size={14} />
                        : null}
                  </span>
                </span>
              </button>
            ))}
            {!searchHits.length ? (
              <p className="cosmic-agents-detail-section-copy">
                No models match “{modelQuery}”.
              </p>
            ) : null}
          </div>
        ) : (
          <>
            <div className="opencode-group-rail">
              {groups.map((group) => {
                const usableCount = group.models.filter((model) => model.usable).length
                const isActive = group.models.some((model) => model.id === preferredModel)
                return (
                  <button
                    key={group.id}
                    type="button"
                    className={`opencode-rail-item ${openGroupId === group.id ? 'active' : ''}`}
                    onClick={() => toggleGroup(group.id)}
                    title={group.connected ? `${usableCount} usable` : 'Connect to use its models'}
                  >
                    <span className={`opencode-provider-dot ${group.connected ? 'on' : ''}`} data-state={group.connected ? 'on' : 'off'} />
                    <span className="opencode-rail-label">{group.label}</span>
                    {isActive ? <span className="opencode-provider-tag selected">In use</span> : null}
                    <span className="opencode-rail-count">{group.models.length}</span>
                  </button>
                )
              })}
            </div>

            {openGroup ? (
              <div className="opencode-opened-group" ref={openedGroupRef}>
                <div className="opencode-opened-group-head">
                  <button type="button" className="opencode-opened-back" onClick={() => setOpenGroupId(null)}>
                    <ChevronDown size={13} className="opencode-group-chevron up" />
                    All providers
                  </button>
                  <strong>{openGroup.label}</strong>
                  {!openGroup.connected ? (
                    <span className="opencode-locked-note">
                      <Lock size={11} />
                      Connect to use
                    </span>
                  ) : null}
                </div>
                <div className="opencode-model-list">
                  {visibleGroupModels.map((model) => (
                    <button
                      key={model.id}
                      type="button"
                      className={`opencode-model-row ${preferredModel === model.id && model.usable ? 'active' : ''} ${!model.usable ? 'locked' : ''}`}
                      onClick={() => handleModelPick(openGroup, model)}
                      disabled={saving}
                      title={model.usable ? model.qualified : `${openGroup.label} is not connected — connect it below to use this model`}
                    >
                      <span className="opencode-model-body">
                        <span className="opencode-model-name">{model.label}</span>
                        <small>{model.qualified}</small>
                      </span>
                      <span className="opencode-row-right">
                        {model.free ? <span className="opencode-free-pill">Free</span> : null}
                        <span className="opencode-row-icon">
                          {!model.usable
                            ? <Lock size={12} />
                            : preferredModel === model.id
                              ? <CheckCircle2 size={15} />
                              : null}
                        </span>
                      </span>
                    </button>
                  ))}
                  {openGroup.models.length > MODEL_LIST_PREVIEW ? (
                    <button
                      type="button"
                      className="opencode-model-list-toggle"
                      onClick={() => setGroupModelsExpanded((expanded) => !expanded)}
                    >
                      {groupModelsExpanded
                        ? 'Show less'
                        : `Show ${openGroup.models.length - MODEL_LIST_PREVIEW} more`}
                    </button>
                  ) : null}
                </div>
                {!openGroup.connected ? (
                  <button type="button" className="cosmic-agents-detail-btn ghost opencode-connect-cta" onClick={scrollToProviders}>
                    <Plug size={13} />
                    Connect {openGroup.label}
                  </button>
                ) : null}
              </div>
            ) : (
              <p className="cosmic-agents-detail-section-copy opencode-rail-hint">
                Pick a provider to see its full lineup — models need that provider connected to run.
              </p>
            )}
          </>
        )}
        {!query && catalogSource ? (
          <p className="cosmic-agents-detail-section-copy">
            {`Live from your VM's OpenCode${catalogSource.startsWith('cache') ? ' (cached)' : ''}${
              gatewayStatus?.cli?.version ? ` · CLI ${gatewayStatus.cli.version}` : ''
            }`}
          </p>
        ) : null}
      </div>

      {/* ── Reasoning effort → --variant ────────────────────────────────── */}
      <div className="cosmic-agents-detail-section cosmic-agents-detail-runner">
        <div className="cosmic-agents-detail-section-head">
          <div>
            <span className="cosmic-agents-detail-kicker">Runner defaults</span>
            <h4>Reasoning effort</h4>
          </div>
        </div>
        <div className="opencode-variant-seg" role="group" aria-label="Reasoning effort">
          {VARIANT_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              className={variant === option.value ? 'active' : ''}
              onClick={() => saveVariant(option.value)}
              disabled={saving}
              title="Sent to OpenCode as --variant; models without that effort fall back to their default."
            >
              <strong>{option.label}</strong>
              <small>{option.note}</small>
            </button>
          ))}
        </div>
      </div>

      {/* ── Connect providers (GUI of /connect) ─────────────────────────── */}
      <div className="cosmic-agents-detail-section" id="opencode-providers-section">
        <div className="cosmic-agents-detail-section-head">
          <div>
            <span className="cosmic-agents-detail-kicker">Providers</span>
            <h4>Unlock providers with API keys</h4>
          </div>
        </div>
        <p className="cosmic-agents-detail-section-copy">
          Free Zen models work without any keys. Connect a provider once to make its locked models
          usable — keys stay encrypted on your VM.
        </p>

        {(() => {
          const connectedTiles = providerTiles.filter((tile) => tile.connected)
          const availableTiles = providerTiles.filter((tile) => !tile.connected)
          return (
            <>
              <div className="opencode-connected-chips">
                {connectedTiles.length ? (
                  connectedTiles.map((tile) => (
                    <span key={tile.id} className="opencode-chip">
                      <span className="opencode-provider-dot on" data-state="on" />
                      {tile.label}
                      {tile.needs_key ? (
                        <button
                          type="button"
                          onClick={() => void disconnectProvider(tile.id)}
                          disabled={saving}
                          title={`Disconnect ${tile.label}`}
                        >
                          <X size={11} />
                        </button>
                      ) : null}
                    </span>
                  ))
                ) : (
                  <span className="opencode-chips-empty">Nothing connected — fine for free models.</span>
                )}
              </div>

              {availableTiles.length ? (
                <div className="opencode-provider-list">
                  {availableTiles.map((tile) => (
                    <div key={tile.id} className="opencode-provider-row">
                      <strong>{tile.label}</strong>
                      {tile.local_only ? <span className="opencode-provider-tag">Local</span> : null}
                      <div className="opencode-provider-row-actions">
                        {connectTarget === tile.id ? (
                          <>
                            <input
                              autoFocus
                              type="password"
                              value={keyDrafts[tile.id] || ''}
                              onChange={(event) =>
                                setKeyDrafts((prev) => ({ ...prev, [tile.id]: event.target.value }))
                              }
                              onKeyDown={(event) => {
                                if (event.key === 'Enter') void submitConnect()
                                if (event.key === 'Escape') setConnectTarget(null)
                              }}
                              placeholder={`${tile.label} API key`}
                              spellCheck={false}
                              autoComplete="off"
                              disabled={saving}
                            />
                            <button type="button" className="cosmic-agents-detail-btn" onClick={() => void submitConnect()} disabled={saving}>
                              Save
                            </button>
                            <button type="button" className="cosmic-agents-detail-btn ghost icon" onClick={() => setConnectTarget(null)} disabled={saving} title="Cancel">
                              <X size={13} />
                            </button>
                          </>
                        ) : tile.needs_key ? (
                          <button type="button" className="cosmic-agents-detail-btn ghost" onClick={() => beginConnect(tile.id)} disabled={saving}>
                            <Plug size={12} />
                            Connect
                          </button>
                        ) : (
                          <small className="opencode-provider-hint">Auto-detected once running</small>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="cosmic-agents-detail-section-copy">Every known provider is connected.</p>
              )}
            </>
          )
        })()}
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

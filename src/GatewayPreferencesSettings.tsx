import { useEffect, useMemo, useState } from 'react'

type GatewayConnectionState = {
  state: 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'error'
  connected: boolean
  detail?: string
}

type CosmicOrchestratorProvider = 'anthropic' | 'fireworks_kimi' | 'fireworks_glm'

const GLM_53_MODEL = 'accounts/fireworks/models/glm-5p3'
const GLM_53_FLASH_MODEL = 'accounts/fireworks/models/glm-5p3-flash'

interface GatewayPreferencesSettingsProps {
  active: boolean
  isAuthenticated: boolean
  gatewayConnection?: GatewayConnectionState
}

interface TimestampedPreference {
  revision: number
  updatedAt: string | null
  updatedSource: string | null
  updatedDeviceId: string | null
}

interface VisualResponseEnhancementPreference extends TimestampedPreference {
  enabled: boolean
}

interface CosmicOrchestratorModelPreference extends TimestampedPreference {
  provider: CosmicOrchestratorProvider
  model: string
}

interface CosmicHeartbeatPreference extends TimestampedPreference {
  enabled: boolean
  intervalSec?: number | null
  nextFireAt?: string | null
  lastFiredAt?: string | null
  lastSuppressedAt?: string | null
  lastResultStatus?: string | null
  lastResultSummary?: string | null
}

interface GatewayPreferenceSnapshot {
  visual: VisualResponseEnhancementPreference
  cosmic: CosmicOrchestratorModelPreference
  heartbeat: CosmicHeartbeatPreference
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' ? value as Record<string, unknown> : null
}

function errorMessage(err: unknown, fallback: string) {
  return err instanceof Error && err.message ? err.message : fallback
}

function normalizeTimestampedPreference(source: Record<string, unknown>): TimestampedPreference {
  const revision = Number(source.revision)
  return {
    revision: Number.isFinite(revision) && revision > 0 ? Math.trunc(revision) : 1,
    updatedAt:
      typeof source.updated_at === 'string'
        ? source.updated_at
        : typeof source.updatedAt === 'string'
          ? source.updatedAt
          : null,
    updatedSource:
      typeof source.updated_source === 'string'
        ? source.updated_source
        : typeof source.updatedSource === 'string'
          ? source.updatedSource
          : null,
    updatedDeviceId:
      typeof source.updated_device_id === 'string'
        ? source.updated_device_id
        : typeof source.updatedDeviceId === 'string'
          ? source.updatedDeviceId
          : null,
  }
}

function normalizeCosmicProvider(value: unknown): CosmicOrchestratorProvider {
  const normalized = String(value || '').trim().toLowerCase().replace(/-/g, '_')
  if (normalized === 'fireworks' || normalized === 'fireworks_kimi' || normalized === 'kimi' || normalized === 'smarter') {
    return 'fireworks_kimi'
  }
  if (
    normalized === 'fireworks_glm' || normalized === 'glm' || normalized === 'glm_5p2' ||
    normalized === 'glm_5_2' || normalized === 'glm52' || normalized === 'glm_5p3' ||
    normalized === 'glm_5_3' || normalized === 'glm53' || normalized === 'glm_flash' ||
    normalized === 'glm_5p3_flash' || normalized === 'glm_5_3_flash'
  ) {
    return 'fireworks_glm'
  }
  return 'anthropic'
}

function normalizePreferences(payload: unknown): GatewayPreferenceSnapshot | null {
  const payloadRecord = asRecord(payload)
  const root = asRecord(payloadRecord?.preferences) || payloadRecord
  const visualSource = asRecord(root?.visual_response_enhancement)
  const cosmicSource = asRecord(root?.cosmic_orchestrator_model)
  const heartbeatSource = asRecord(root?.cosmic_heartbeat)

  if (!visualSource || !cosmicSource || !heartbeatSource) {
    return null
  }

  return {
    visual: {
      ...normalizeTimestampedPreference(visualSource),
      enabled: visualSource.enabled !== false,
    },
    cosmic: {
      ...normalizeTimestampedPreference(cosmicSource),
      provider: normalizeCosmicProvider(cosmicSource.provider),
      model: typeof cosmicSource.model === 'string' ? cosmicSource.model : '',
    },
    heartbeat: {
      ...normalizeTimestampedPreference(heartbeatSource),
      enabled: heartbeatSource.enabled !== false,
      intervalSec: Number.isFinite(Number(heartbeatSource.interval_sec ?? heartbeatSource.intervalSec))
        ? Number(heartbeatSource.interval_sec ?? heartbeatSource.intervalSec)
        : null,
      nextFireAt:
        typeof heartbeatSource.next_fire_at === 'string'
          ? heartbeatSource.next_fire_at
          : typeof heartbeatSource.nextFireAt === 'string'
            ? heartbeatSource.nextFireAt
            : null,
      lastFiredAt:
        typeof heartbeatSource.last_fired_at === 'string'
          ? heartbeatSource.last_fired_at
          : typeof heartbeatSource.lastFiredAt === 'string'
            ? heartbeatSource.lastFiredAt
            : null,
      lastSuppressedAt:
        typeof heartbeatSource.last_suppressed_at === 'string'
          ? heartbeatSource.last_suppressed_at
          : typeof heartbeatSource.lastSuppressedAt === 'string'
            ? heartbeatSource.lastSuppressedAt
            : null,
      lastResultStatus:
        typeof heartbeatSource.last_result_status === 'string'
          ? heartbeatSource.last_result_status
          : typeof heartbeatSource.lastResultStatus === 'string'
            ? heartbeatSource.lastResultStatus
            : null,
      lastResultSummary:
        typeof heartbeatSource.last_result_summary === 'string'
          ? heartbeatSource.last_result_summary
          : typeof heartbeatSource.lastResultSummary === 'string'
            ? heartbeatSource.lastResultSummary
            : null,
    },
  }
}

function formatUpdatedAt(value: string | null) {
  if (!value) {
    return null
  }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return null
  }
  return date.toLocaleString()
}

function formatRelativeTime(value: string | null | undefined) {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  const diffMs = Date.now() - date.getTime()
  if (diffMs < 0) return 'just now'
  const minute = 60 * 1000
  const hour = 60 * minute
  const day = 24 * hour
  if (diffMs < minute) return 'just now'
  if (diffMs < hour) {
    const minutes = Math.max(1, Math.round(diffMs / minute))
    return `${minutes} min${minutes === 1 ? '' : 's'} ago`
  }
  if (diffMs < day) {
    const hours = Math.max(1, Math.round(diffMs / hour))
    return `${hours} hour${hours === 1 ? '' : 's'} ago`
  }
  const days = Math.max(1, Math.round(diffMs / day))
  return `${days} day${days === 1 ? '' : 's'} ago`
}

function formatFutureTime(value: string | null | undefined) {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  const diffMs = date.getTime() - Date.now()
  if (diffMs <= 0) return 'due now'
  const minute = 60 * 1000
  const hour = 60 * minute
  const day = 24 * hour
  if (diffMs < hour) {
    const minutes = Math.max(1, Math.round(diffMs / minute))
    return `in ${minutes} min${minutes === 1 ? '' : 's'}`
  }
  if (diffMs < day) {
    const hours = Math.max(1, Math.round(diffMs / hour))
    return `in ${hours} hour${hours === 1 ? '' : 's'}`
  }
  const days = Math.max(1, Math.round(diffMs / day))
  return `in ${days} day${days === 1 ? '' : 's'}`
}

function heartbeatIntervalLabel(seconds?: number | null) {
  const normalized = Number(seconds || 0)
  if (!Number.isFinite(normalized) || normalized <= 0) return '30 min'
  if (normalized < 3600) {
    const minutes = Math.max(1, Math.round(normalized / 60))
    return `${minutes} min`
  }
  const hours = normalized / 3600
  return Number.isInteger(hours) ? `${hours} hr` : `${hours.toFixed(1)} hr`
}

function preferenceDetail(preference: TimestampedPreference | null) {
  if (!preference?.updatedSource) {
    return null
  }
  return (
    <>
      Source: {preference.updatedSource}
      {preference.updatedDeviceId ? ` - ${preference.updatedDeviceId}` : ''}
      {preference.revision ? ` - rev ${preference.revision}` : ''}
    </>
  )
}

export default function GatewayPreferencesSettings({
  active,
  isAuthenticated,
  gatewayConnection,
}: GatewayPreferencesSettingsProps) {
  const [preferences, setPreferences] = useState<GatewayPreferenceSnapshot | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [savingKey, setSavingKey] = useState<'visual' | 'cosmic' | 'heartbeat' | null>(null)
  const [showMoreModels, setShowMoreModels] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadPreferences = async () => {
    if (!window.cosmic?.getGatewayPreferences) {
      setError('This desktop build does not expose gateway preference controls yet.')
      return
    }
    setIsLoading(true)
    setError(null)
    try {
      const payload = await window.cosmic.getGatewayPreferences()
      const nextPreferences = normalizePreferences(payload)
      if (!nextPreferences) {
        throw new Error('Gateway returned an invalid preferences payload.')
      }
      setPreferences(nextPreferences)
    } catch (err: unknown) {
      setError(errorMessage(err, 'Unable to load preferences from your VM.'))
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => {
    if (!active || !isAuthenticated) {
      return
    }
    void loadPreferences()
  }, [active, isAuthenticated, gatewayConnection?.connected])

  useEffect(() => {
    if (!active || !isAuthenticated || !window.cosmic?.onGatewayEvent) {
      return
    }
    return window.cosmic.onGatewayEvent((event) => {
      if (String(event?.type || '') !== 'preferences.updated') {
        return
      }
      const nextPreferences = normalizePreferences(event)
      if (!nextPreferences) {
        return
      }
      setPreferences(nextPreferences)
      setError(null)
      setIsLoading(false)
      setSavingKey(null)
    })
  }, [active, isAuthenticated])

  const latestUpdatedAt = useMemo(() => {
    const candidates = [
      preferences?.visual.updatedAt || null,
      preferences?.cosmic.updatedAt || null,
      preferences?.heartbeat.updatedAt || null,
    ]
      .map((value) => (value ? new Date(value) : null))
      .filter((value): value is Date => value instanceof Date && !Number.isNaN(value.getTime()))
      .sort((a, b) => b.getTime() - a.getTime())
    return formatUpdatedAt(candidates[0]?.toISOString() || null)
  }, [preferences?.visual.updatedAt, preferences?.cosmic.updatedAt, preferences?.heartbeat.updatedAt])

  const lastBeatLabel = useMemo(() => {
    const relative = formatRelativeTime(preferences?.heartbeat.lastFiredAt || preferences?.heartbeat.lastSuppressedAt)
    return relative ? `Last beat ${relative}` : 'No beat yet'
  }, [preferences?.heartbeat.lastFiredAt, preferences?.heartbeat.lastSuppressedAt])

  const nextBeatLabel = useMemo(() => {
    if (!preferences?.heartbeat.nextFireAt) return 'Next beat pending'
    const relative = formatFutureTime(preferences.heartbeat.nextFireAt)
    return relative ? `Next beat ${relative}` : 'Next beat scheduled'
  }, [preferences?.heartbeat.nextFireAt])

  const isSaving = Boolean(savingKey)
  const cosmicModel = preferences?.cosmic.model || ''
  const isGlmFlashSelected =
    preferences?.cosmic.provider === 'fireworks_glm' &&
    (!cosmicModel || cosmicModel === GLM_53_FLASH_MODEL)
  const isGlm53Selected =
    preferences?.cosmic.provider === 'fireworks_glm' && cosmicModel === GLM_53_MODEL
  const statusLabel = !isAuthenticated
    ? 'Sign in required'
    : isSaving
    ? 'Saving to VM...'
    : isLoading
      ? 'Loading from VM...'
      : error
        ? 'VM unavailable'
        : preferences
          ? 'Saved on your VM'
          : 'Waiting for VM'

  const statusTone = !isAuthenticated
    ? 'idle'
    : error
    ? 'error'
    : preferences
      ? 'ready'
      : 'idle'

  const canSave =
    isAuthenticated &&
    Boolean(preferences) &&
    !isLoading &&
    !isSaving &&
    Boolean(window.cosmic?.saveGatewayPreferences)

  const handleToggleVisual = async () => {
    if (!preferences || !window.cosmic?.saveGatewayPreferences || !canSave) {
      return
    }
    setSavingKey('visual')
    setError(null)
    try {
      const payload = await window.cosmic.saveGatewayPreferences({
        visualResponseEnhancementEnabled: !preferences.visual.enabled,
      })
      const nextPreferences = normalizePreferences(payload)
      if (!nextPreferences) {
        throw new Error('Gateway returned an invalid preferences payload.')
      }
      setPreferences(nextPreferences)
    } catch (err: unknown) {
      setError(errorMessage(err, 'Unable to save this preference to your VM.'))
    } finally {
      setSavingKey(null)
    }
  }

  const handleToggleHeartbeat = async () => {
    if (!preferences || !window.cosmic?.saveGatewayPreferences || !canSave) {
      return
    }
    setSavingKey('heartbeat')
    setError(null)
    try {
      const payload = await window.cosmic.saveGatewayPreferences({
        cosmicHeartbeatEnabled: !preferences.heartbeat.enabled,
      })
      const nextPreferences = normalizePreferences(payload)
      if (!nextPreferences) {
        throw new Error('Gateway returned an invalid preferences payload.')
      }
      setPreferences(nextPreferences)
    } catch (err: unknown) {
      setError(errorMessage(err, 'Unable to save this preference to your VM.'))
    } finally {
      setSavingKey(null)
    }
  }

  const handleSelectModel = async (provider: CosmicOrchestratorProvider, model?: string) => {
    if (!preferences || !window.cosmic?.saveGatewayPreferences || !canSave) {
      return
    }
    if (model === undefined && preferences.cosmic.provider === provider) {
      return
    }
    if (
      model !== undefined &&
      preferences.cosmic.provider === provider &&
      preferences.cosmic.model === model
    ) {
      return
    }
    setSavingKey('cosmic')
    setError(null)
    try {
      const payload = await window.cosmic.saveGatewayPreferences({
        cosmicOrchestratorProvider: provider,
        ...(model !== undefined ? { cosmicOrchestratorModel: model } : {}),
      })
      const nextPreferences = normalizePreferences(payload)
      if (!nextPreferences) {
        throw new Error('Gateway returned an invalid preferences payload.')
      }
      setPreferences(nextPreferences)
    } catch (err: unknown) {
      setError(errorMessage(err, 'Unable to save this preference to your VM.'))
    } finally {
      setSavingKey(null)
    }
  }

  return (
    <div className="setting-subpage preferences-page">
      <header className="preferences-hero">
        <div className="preferences-hero-top">
          <div className="preferences-hero-text">
            <span className="preferences-kicker">Gateway</span>
            <h2 className="preferences-title">Preferences</h2>
          </div>
          <span className={`preferences-status-chip ${statusTone}`}>{statusLabel}</span>
        </div>
        <p className="preferences-lead">
          These preferences live on your Cosmic VM and follow the desktop and mobile clients attached to this backend.
        </p>
      </header>

      {!isAuthenticated && (
        <div className="preferences-status-meta subtle">
          Sign in to your VM to load and edit backend-backed preferences.
        </div>
      )}

      <div className="preferences-stats" aria-label="Preference summary">
        <div className="preferences-stat">
          <span className="preferences-stat-value">{preferences?.heartbeat.enabled ? 'On' : 'Off'}</span>
          <span className="preferences-stat-label">Heartbeat</span>
        </div>
        <div className="preferences-stat preferences-stat--accent">
          <span className="preferences-stat-value">{heartbeatIntervalLabel(preferences?.heartbeat.intervalSec)}</span>
          <span className="preferences-stat-label">Cadence</span>
        </div>
        {latestUpdatedAt && (
          <div className="preferences-stat">
            <span className="preferences-stat-value">Saved</span>
            <span className="preferences-stat-label">{latestUpdatedAt}</span>
          </div>
        )}
      </div>

      <div className={`preferences-card preferences-card-column ${!canSave ? 'muted' : ''}`}>
        <div className="preferences-card-copy">
          <div className="preferences-card-title">Cosmic Brain</div>
          <div className="preferences-card-note">
            Choose the model behind Cosmic's orchestrator. Smart uses Claude; GLM and Kimi run on Fireworks. GLM 5.3 Flash is natively multimodal, so image turns stay on it. GLM 5.3 has no vision — when a turn needs images, Cosmic temporarily routes that turn through Kimi without changing your preference.
          </div>
          {preferences?.cosmic.model && (
            <div className="preferences-card-detail">
              Model: {preferences.cosmic.model}
            </div>
          )}
          {preferenceDetail(preferences?.cosmic || null) && (
            <div className="preferences-card-detail">
              {preferenceDetail(preferences?.cosmic || null)}
            </div>
          )}
        </div>

        <div className="preferences-provider-control" aria-label="Cosmic orchestrator model">
          <button
            type="button"
            className={`preferences-provider-option ${preferences?.cosmic.provider === 'anthropic' ? 'active smart' : ''}`}
            onClick={() => { void handleSelectModel('anthropic') }}
            disabled={!canSave}
            aria-pressed={preferences?.cosmic.provider === 'anthropic'}
          >
            <span>Smart</span>
            <small>Claude</small>
          </button>
          <button
            type="button"
            className={`preferences-provider-option ${isGlmFlashSelected ? 'active' : ''}`}
            onClick={() => { void handleSelectModel('fireworks_glm', GLM_53_FLASH_MODEL) }}
            disabled={!canSave}
            aria-pressed={isGlmFlashSelected}
          >
            <span>GLM Flash</span>
            <small>Fireworks 5.3 Flash</small>
          </button>
          <button
            type="button"
            className={`preferences-provider-option ${isGlm53Selected ? 'active' : ''}`}
            onClick={() => { void handleSelectModel('fireworks_glm', GLM_53_MODEL) }}
            disabled={!canSave}
            aria-pressed={isGlm53Selected}
          >
            <span>GLM</span>
            <small>Fireworks 5.3</small>
          </button>
        </div>
        <button
          type="button"
          className="preferences-provider-more"
          onClick={() => { setShowMoreModels(!showMoreModels) }}
          aria-expanded={showMoreModels}
        >
          {showMoreModels ? 'Show less' : 'Show more'}
        </button>
        {showMoreModels && (
          <div className="preferences-provider-control preferences-provider-control--more" aria-label="More orchestrator models">
            <button
              type="button"
              className={`preferences-provider-option ${preferences?.cosmic.provider === 'fireworks_kimi' ? 'active' : ''}`}
              onClick={() => { void handleSelectModel('fireworks_kimi') }}
              disabled={!canSave}
              aria-pressed={preferences?.cosmic.provider === 'fireworks_kimi'}
            >
              <span>Kimi</span>
              <small>Fireworks K2.6</small>
            </button>
          </div>
        )}
      </div>

      <div className={`preferences-card ${!canSave ? 'muted' : ''}`}>
        <div className="preferences-card-copy">
          <div className="preferences-card-title">Visual Response Enhancement</div>
          <div className="preferences-card-note">
            Allow richer responses with inline visuals when the backend supports them. This does not force visuals on every turn.
          </div>
          {preferenceDetail(preferences?.visual || null) && (
            <div className="preferences-card-detail">
              {preferenceDetail(preferences?.visual || null)}
            </div>
          )}
        </div>

        <button
          type="button"
          className={`preferences-switch ${preferences?.visual.enabled ? 'enabled' : 'disabled'}`}
          onClick={handleToggleVisual}
          disabled={!canSave}
          aria-pressed={preferences?.visual.enabled === true}
        >
          <span className="preferences-switch-track">
            <span className="preferences-switch-thumb" />
          </span>
          <span className="preferences-switch-label">
            {preferences?.visual.enabled ? 'On' : 'Off'}
          </span>
        </button>
      </div>

      <div className={`preferences-card ${!canSave ? 'muted' : ''}`}>
        <div className="preferences-card-copy">
          <div className="preferences-card-title-row">
            <div className="preferences-card-title">Cosmic Heartbeat</div>
          </div>
          <div className="preferences-card-note">
            Let Cosmic run a quiet ambient check every 30 minutes and only surface a note when there is something genuinely useful.
          </div>
          <div className="preferences-heartbeat-meta">
            <span>{lastBeatLabel}</span>
            <span>{nextBeatLabel}</span>
            {preferences?.heartbeat.lastResultStatus ? (
              <span>{preferences.heartbeat.lastResultStatus}</span>
            ) : null}
          </div>
          {preferenceDetail(preferences?.heartbeat || null) && (
            <div className="preferences-card-detail">
              {preferenceDetail(preferences?.heartbeat || null)}
            </div>
          )}
        </div>

        <div className="preferences-heartbeat-control">
          <span className="preferences-heartbeat-last">
            {lastBeatLabel}
          </span>
          <button
            type="button"
            className={`preferences-switch ${preferences?.heartbeat.enabled ? 'enabled' : 'disabled'}`}
            onClick={handleToggleHeartbeat}
            disabled={!canSave}
            aria-pressed={preferences?.heartbeat.enabled === true}
          >
            <span className="preferences-switch-track">
              <span className="preferences-switch-thumb" />
            </span>
            <span className="preferences-switch-label">
              {preferences?.heartbeat.enabled ? 'On' : 'Off'}
            </span>
          </button>
        </div>
      </div>

      {error && (
        <div className="preferences-error-banner">
          <span>{error}</span>
          <button
            type="button"
            className="preferences-retry-btn"
            onClick={() => { void loadPreferences() }}
            disabled={isLoading}
          >
            Retry
          </button>
        </div>
      )}

      {gatewayConnection?.detail && gatewayConnection.state !== 'connected' && !error && (
        <div className="preferences-status-meta subtle">
          {gatewayConnection.detail}
        </div>
      )}
    </div>
  )
}

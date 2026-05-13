import { useEffect, useMemo, useState } from 'react'

type GatewayConnectionState = {
  state: 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'error'
  connected: boolean
  detail?: string
}

type CosmicOrchestratorProvider = 'anthropic' | 'fireworks_kimi'

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

interface GatewayPreferenceSnapshot {
  visual: VisualResponseEnhancementPreference
  cosmic: CosmicOrchestratorModelPreference
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
  return normalized === 'fireworks' || normalized === 'fireworks_kimi' || normalized === 'kimi' || normalized === 'smarter'
    ? 'fireworks_kimi'
    : 'anthropic'
}

function normalizePreferences(payload: unknown): GatewayPreferenceSnapshot | null {
  const payloadRecord = asRecord(payload)
  const root = asRecord(payloadRecord?.preferences) || payloadRecord
  const visualSource = asRecord(root?.visual_response_enhancement)
  const cosmicSource = asRecord(root?.cosmic_orchestrator_model)

  if (!visualSource || !cosmicSource) {
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
  const [savingKey, setSavingKey] = useState<'visual' | 'cosmic' | null>(null)
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
    ]
      .map((value) => (value ? new Date(value) : null))
      .filter((value): value is Date => value instanceof Date && !Number.isNaN(value.getTime()))
      .sort((a, b) => b.getTime() - a.getTime())
    return formatUpdatedAt(candidates[0]?.toISOString() || null)
  }, [preferences?.visual.updatedAt, preferences?.cosmic.updatedAt])

  const isSaving = Boolean(savingKey)
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

  const handleSelectProvider = async (provider: CosmicOrchestratorProvider) => {
    if (!preferences || !window.cosmic?.saveGatewayPreferences || !canSave || preferences.cosmic.provider === provider) {
      return
    }
    setSavingKey('cosmic')
    setError(null)
    try {
      const payload = await window.cosmic.saveGatewayPreferences({
        cosmicOrchestratorProvider: provider,
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
      <div className="preferences-intro">
        These preferences live on your Cosmic VM and apply across desktop sessions connected to this backend.
      </div>

      {!isAuthenticated && (
        <div className="preferences-status-meta subtle">
          Sign in to your VM to load and edit backend-backed preferences.
        </div>
      )}

      <div className="preferences-status-row">
        <span className={`preferences-status-chip ${statusTone}`}>{statusLabel}</span>
        {latestUpdatedAt && (
          <span className="preferences-status-meta">Last updated {latestUpdatedAt}</span>
        )}
      </div>

      <div className={`preferences-card preferences-card-column ${!canSave ? 'muted' : ''}`}>
        <div className="preferences-card-copy">
          <div className="preferences-card-title">Cosmic Brain</div>
          <div className="preferences-card-note">
            Choose the model provider behind Cosmic's orchestrator. Smart uses Claude, the current production path; Kimi uses Fireworks K2.6 through the OpenAI-compatible path.
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

        <div className="preferences-provider-control" aria-label="Cosmic orchestrator provider">
          <button
            type="button"
            className={`preferences-provider-option ${preferences?.cosmic.provider === 'anthropic' ? 'active smart' : ''}`}
            onClick={() => { void handleSelectProvider('anthropic') }}
            disabled={!canSave}
            aria-pressed={preferences?.cosmic.provider === 'anthropic'}
          >
            <span>Smart</span>
            <small>Claude</small>
          </button>
          <button
            type="button"
            className={`preferences-provider-option ${preferences?.cosmic.provider === 'fireworks_kimi' ? 'active' : ''}`}
            onClick={() => { void handleSelectProvider('fireworks_kimi') }}
            disabled={!canSave}
            aria-pressed={preferences?.cosmic.provider === 'fireworks_kimi'}
          >
            <span>Kimi</span>
            <small>Fireworks K2.6</small>
          </button>
        </div>
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

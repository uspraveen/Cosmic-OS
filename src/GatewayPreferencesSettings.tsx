import { useEffect, useMemo, useState } from 'react'

type GatewayConnectionState = {
  state: 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'error'
  connected: boolean
  detail?: string
}

interface GatewayPreferencesSettingsProps {
  active: boolean
  isAuthenticated: boolean
  gatewayConnection?: GatewayConnectionState
}

interface VisualResponseEnhancementPreference {
  enabled: boolean
  revision: number
  updatedAt: string | null
  updatedSource: string | null
  updatedDeviceId: string | null
}

function normalizeVisualPreference(payload: any): VisualResponseEnhancementPreference | null {
  const source = payload?.visual_response_enhancement && typeof payload.visual_response_enhancement === 'object'
    ? payload.visual_response_enhancement
    : payload?.preferences?.visual_response_enhancement && typeof payload.preferences.visual_response_enhancement === 'object'
      ? payload.preferences.visual_response_enhancement
      : null

  if (!source) {
    return null
  }

  const revision = Number(source.revision)
  return {
    enabled: source.enabled !== false,
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

export default function GatewayPreferencesSettings({
  active,
  isAuthenticated,
  gatewayConnection,
}: GatewayPreferencesSettingsProps) {
  const [preference, setPreference] = useState<VisualResponseEnhancementPreference | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [isSaving, setIsSaving] = useState(false)
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
      const nextPreference = normalizeVisualPreference(payload)
      if (!nextPreference) {
        throw new Error('Gateway returned an invalid preferences payload.')
      }
      setPreference(nextPreference)
    } catch (err: any) {
      setError(err?.message || 'Unable to load preferences from your VM.')
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
      const nextPreference = normalizeVisualPreference(event)
      if (!nextPreference) {
        return
      }
      setPreference(nextPreference)
      setError(null)
      setIsLoading(false)
      setIsSaving(false)
    })
  }, [active, isAuthenticated])

  const updatedAtLabel = useMemo(
    () => formatUpdatedAt(preference?.updatedAt || null),
    [preference?.updatedAt],
  )

  const statusLabel = !isAuthenticated
    ? 'Sign in required'
    : isSaving
    ? 'Saving to VM...'
    : isLoading
      ? 'Loading from VM...'
      : error
        ? 'VM unavailable'
        : preference
          ? 'Saved on your VM'
          : 'Waiting for VM'

  const statusTone = !isAuthenticated
    ? 'idle'
    : error
    ? 'error'
    : preference
      ? 'ready'
      : 'idle'

  const canToggle =
    isAuthenticated &&
    Boolean(preference) &&
    !isLoading &&
    !isSaving &&
    Boolean(window.cosmic?.saveGatewayPreferences)

  const handleToggle = async () => {
    if (!preference || !window.cosmic?.saveGatewayPreferences || !canToggle) {
      return
    }
    setIsSaving(true)
    setError(null)
    try {
      const payload = await window.cosmic.saveGatewayPreferences({
        visualResponseEnhancementEnabled: !preference.enabled,
      })
      const nextPreference = normalizeVisualPreference(payload)
      if (!nextPreference) {
        throw new Error('Gateway returned an invalid preferences payload.')
      }
      setPreference(nextPreference)
    } catch (err: any) {
      setError(err?.message || 'Unable to save this preference to your VM.')
    } finally {
      setIsSaving(false)
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
        {updatedAtLabel && (
          <span className="preferences-status-meta">Last updated {updatedAtLabel}</span>
        )}
      </div>

      <div className={`preferences-card ${!canToggle ? 'muted' : ''}`}>
        <div className="preferences-card-copy">
          <div className="preferences-card-title">Visual Response Enhancement</div>
          <div className="preferences-card-note">
            Allow richer responses with inline visuals when the backend supports them. This does not force visuals on every turn.
          </div>
          {preference?.updatedSource && (
            <div className="preferences-card-detail">
              Source: {preference.updatedSource}
              {preference.updatedDeviceId ? ` - ${preference.updatedDeviceId}` : ''}
              {preference.revision ? ` - rev ${preference.revision}` : ''}
            </div>
          )}
        </div>

        <button
          type="button"
          className={`preferences-switch ${preference?.enabled ? 'enabled' : 'disabled'}`}
          onClick={handleToggle}
          disabled={!canToggle}
          aria-pressed={preference?.enabled === true}
        >
          <span className="preferences-switch-track">
            <span className="preferences-switch-thumb" />
          </span>
          <span className="preferences-switch-label">
            {preference?.enabled ? 'On' : 'Off'}
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

import { useCallback, useEffect, useMemo, useState } from 'react'

interface MobileDeviceRow {
  device_id: string
  device_name?: string | null
  device_name_source?: string | null
  model_name?: string | null
  brand?: string | null
  manufacturer?: string | null
  platform?: string | null
  os_name?: string | null
  os_version?: string | null
  device_type?: string | null
  is_physical_device?: boolean | null
  app_version?: string | null
  app_build?: string | null
  first_seen_at?: string | null
  last_seen_at?: string | null
  last_connected_at?: string | null
  last_disconnected_at?: string | null
  last_session_id?: string | null
  current_session_id?: string | null
  current_channel?: string | null
  revoked_at?: string | null
  revoke_reason?: string | null
  revoked?: boolean
  active?: boolean
}

interface MobileDevicesSettingsProps {
  active: boolean
}

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

function compactParts(parts: Array<string | null | undefined>): string {
  return parts
    .map((part) => String(part || '').trim())
    .filter(Boolean)
    .join(' • ')
}

function getDeviceTitle(device: MobileDeviceRow): string {
  return String(device.device_name || '').trim()
    || String(device.model_name || '').trim()
    || String(device.brand || '').trim()
    || device.device_id
}

function getDeviceSubtitle(device: MobileDeviceRow): string {
  const modelPart = String(device.model_name || '').trim() || compactParts([device.brand, device.manufacturer])
  const osPart = compactParts([device.os_name || device.platform, device.os_version])
  return compactParts([modelPart, osPart, device.device_type || null])
}

function getDeviceSourceLabel(device: MobileDeviceRow): string | null {
  const source = String(device.device_name_source || '').trim()
  if (!source) return null
  if (source === 'generic_ios') return 'Generic iOS name'
  if (source === 'user_assigned') return 'Phone setting name'
  if (source === 'brand_model') return 'Brand/model fallback'
  if (source === 'model') return 'Model fallback'
  if (source === 'fallback') return 'Platform fallback'
  return source.replace(/_/g, ' ')
}

export default function MobileDevicesSettings({ active }: MobileDevicesSettingsProps) {
  const [devices, setDevices] = useState<MobileDeviceRow[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [revokingDeviceId, setRevokingDeviceId] = useState<string | null>(null)
  const [revokingAll, setRevokingAll] = useState(false)

  const refreshDevices = useCallback(async () => {
    if (!window.cosmic?.listMobileDevices) return
    setLoading(true)
    setError(null)
    try {
      const payload = await window.cosmic.listMobileDevices()
      const nextDevices = Array.isArray(payload?.devices) ? payload.devices as MobileDeviceRow[] : []
      setDevices(nextDevices)
    } catch (err: any) {
      setError(err?.message ?? 'Failed to load mobile devices.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!active) return
    void refreshDevices()
  }, [active, refreshDevices])

  const handleRevokeDevice = useCallback(async (deviceId: string) => {
    if (!window.cosmic?.revokeMobileDevice) return
    setRevokingDeviceId(deviceId)
    setError(null)
    try {
      await window.cosmic.revokeMobileDevice(deviceId)
      await refreshDevices()
    } catch (err: any) {
      setError(err?.message ?? 'Failed to remove device.')
    } finally {
      setRevokingDeviceId(null)
    }
  }, [refreshDevices])

  const handleRevokeAll = useCallback(async () => {
    if (!window.cosmic?.revokeAllMobileDevices) return
    setRevokingAll(true)
    setError(null)
    try {
      await window.cosmic.revokeAllMobileDevices()
      await refreshDevices()
    } catch (err: any) {
      setError(err?.message ?? 'Failed to remove all devices.')
    } finally {
      setRevokingAll(false)
    }
  }, [refreshDevices])

  const activeCount = useMemo(
    () => devices.filter((device) => device.active && !device.revoked).length,
    [devices],
  )

  return (
    <div className="setting-subpage mobile-devices-page">
      <div className="mobile-devices-hero">
        <div>
          <strong>Linked mobile devices</strong>
          <p>Remove one phone or revoke all phones from the desktop. Removed phones must log in again to re-authorize.</p>
        </div>
        <div className="mobile-devices-hero-actions">
          <button className="mobile-devices-secondary-btn" onClick={() => void refreshDevices()} disabled={loading || revokingAll}>
            {loading ? 'Refreshing…' : 'Refresh'}
          </button>
          <button
            className="mobile-devices-danger-btn"
            onClick={() => void handleRevokeAll()}
            disabled={devices.length === 0 || revokingAll}
          >
            {revokingAll ? 'Removing…' : 'Remove all'}
          </button>
        </div>
      </div>

      <div className="mobile-devices-summary">
        <span>{devices.length} known device{devices.length === 1 ? '' : 's'}</span>
        <span>{activeCount} active now</span>
      </div>

      {error ? <div className="mobile-devices-error">{error}</div> : null}

      {devices.length === 0 && !loading ? (
        <div className="mobile-devices-empty">
          <strong>No phones linked yet</strong>
          <p>A phone appears here after it opens chat against your VM.</p>
        </div>
      ) : null}

      <div className="mobile-devices-list">
        {devices.map((device) => {
          const removingThis = revokingDeviceId === device.device_id
          const sessionId = device.current_session_id || device.last_session_id || ''
          return (
            <article key={device.device_id} className="mobile-device-card">
              <div className="mobile-device-card-head">
                <div>
                  <div className="mobile-device-label">Device</div>
                  <div className="mobile-device-name" title={getDeviceTitle(device)}>{getDeviceTitle(device)}</div>
                  <div className="mobile-device-subtitle">
                    {getDeviceSubtitle(device) || 'Metadata not available yet'}
                  </div>
                  {getDeviceSourceLabel(device) ? (
                    <div className="mobile-device-source-note">{getDeviceSourceLabel(device)}</div>
                  ) : null}
                  <div className="mobile-device-id" title={device.device_id}>{device.device_id}</div>
                </div>
                <div className="mobile-device-pill-wrap">
                  {device.active && !device.revoked ? <span className="mobile-device-pill is-active">Active</span> : null}
                  {device.revoked ? <span className="mobile-device-pill is-revoked">Removed</span> : null}
                </div>
              </div>

              <div className="mobile-device-meta-grid">
                <div>
                  <span className="mobile-device-meta-label">First seen</span>
                  <span className="mobile-device-meta-value">{formatTimestamp(device.first_seen_at)}</span>
                </div>
                <div>
                  <span className="mobile-device-meta-label">Last seen</span>
                  <span className="mobile-device-meta-value">{formatTimestamp(device.last_seen_at)}</span>
                </div>
                <div>
                  <span className="mobile-device-meta-label">Connected</span>
                  <span className="mobile-device-meta-value">{formatTimestamp(device.last_connected_at)}</span>
                </div>
                <div>
                  <span className="mobile-device-meta-label">Disconnected</span>
                  <span className="mobile-device-meta-value">{formatTimestamp(device.last_disconnected_at)}</span>
                </div>
              </div>

              <div className="mobile-device-session">
                <span className="mobile-device-meta-label">Session</span>
                <span className="mobile-device-session-id" title={sessionId || '—'}>
                  {sessionId || '—'}
                </span>
              </div>

              <div className="mobile-device-session">
                <span className="mobile-device-meta-label">App</span>
                <span className="mobile-device-session-id" title={compactParts([device.app_version, device.app_build ? `build ${device.app_build}` : null]) || '—'}>
                  {compactParts([device.app_version, device.app_build ? `build ${device.app_build}` : null]) || '—'}
                </span>
              </div>

              {device.revoked_at ? (
                <div className="mobile-device-revoked-note">
                  Removed {formatTimestamp(device.revoked_at)}
                  {device.revoke_reason ? ` · ${device.revoke_reason}` : ''}
                </div>
              ) : null}

              <div className="mobile-device-card-actions">
                <button
                  className="mobile-devices-danger-btn"
                  onClick={() => void handleRevokeDevice(device.device_id)}
                  disabled={removingThis || !!device.revoked}
                >
                  {removingThis ? 'Removing…' : device.revoked ? 'Removed' : 'Remove device'}
                </button>
              </div>
            </article>
          )
        })}
      </div>
    </div>
  )
}

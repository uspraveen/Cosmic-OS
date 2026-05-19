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
  duplicate_count?: number
  duplicate_device_ids?: string[]
  deduped_device_ids?: string[]
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

function normalizeDedupePart(value?: string | null): string {
  return String(value || '').trim().toLowerCase().replace(/\s+/g, ' ')
}

function getMobileDeviceDedupeKey(device: MobileDeviceRow): string {
  const platform = normalizeDedupePart(device.platform)
  const model = normalizeDedupePart(device.model_name)
  const brand = normalizeDedupePart(device.brand)
  const manufacturer = normalizeDedupePart(device.manufacturer)
  const osName = normalizeDedupePart(device.os_name)
  const osVersion = normalizeDedupePart(device.os_version)
  const deviceType = normalizeDedupePart(device.device_type)
  const deviceName = normalizeDedupePart(device.device_name)
  const nameSource = normalizeDedupePart(device.device_name_source)
  const genericNames = new Set(['android', 'android device', 'ipad', 'iphone', 'mobile', 'mobile device', 'phone', 'unknown'])

  const meaningfulName = deviceName && !genericNames.has(deviceName) && !['fallback', 'generic_ios'].includes(nameSource)
    ? deviceName
    : ''
  if (platform && meaningfulName && (model || brand || manufacturer)) {
    return ['mobile-named', platform, meaningfulName, model, brand, manufacturer, deviceType, String(device.is_physical_device)].join('|')
  }
  if (platform && model && (brand || manufacturer || osVersion) && deviceType) {
    return ['mobile-model', platform, model, brand, manufacturer, osName, osVersion, deviceType, String(device.is_physical_device)].join('|')
  }
  return `id:${device.device_id}`
}

function deviceRank(device: MobileDeviceRow): string {
  return [
    device.active && !device.revoked ? '1' : '0',
    !device.revoked ? '1' : '0',
    device.last_seen_at || '',
    device.last_connected_at || '',
    device.first_seen_at || '',
    device.device_id,
  ].join('|')
}

function getDeviceRegistrationIds(device: MobileDeviceRow): string[] {
  const ids = [
    device.device_id,
    ...(Array.isArray(device.deduped_device_ids) ? device.deduped_device_ids : []),
    ...(Array.isArray(device.duplicate_device_ids) ? device.duplicate_device_ids : []),
  ]
  return Array.from(new Set(ids.map((id) => String(id || '').trim()).filter(Boolean)))
}

function mergeDeviceGroup(group: MobileDeviceRow[]): MobileDeviceRow {
  const ordered = [...group].sort((a, b) => deviceRank(b).localeCompare(deviceRank(a)))
  const canonical = { ...ordered[0] }
  const allIds = Array.from(new Set(ordered.flatMap(getDeviceRegistrationIds)))
  const duplicateIds = allIds.filter((id) => id !== canonical.device_id)

  const firstSeenValues = ordered.map((device) => device.first_seen_at).filter(Boolean) as string[]
  const latest = (field: keyof MobileDeviceRow) => {
    const values = ordered.map((device) => device[field]).filter(Boolean) as string[]
    const sorted = values.sort()
    return sorted.length > 0 ? sorted[sorted.length - 1] : canonical[field]
  }
  canonical.first_seen_at = firstSeenValues.length > 0 ? firstSeenValues.sort()[0] : canonical.first_seen_at
  canonical.last_seen_at = latest('last_seen_at') as string | null | undefined
  canonical.last_connected_at = latest('last_connected_at') as string | null | undefined
  canonical.last_disconnected_at = latest('last_disconnected_at') as string | null | undefined

  const activeDevice = ordered.find((device) => device.active && !device.revoked)
  if (activeDevice) {
    canonical.active = true
    canonical.current_channel = activeDevice.current_channel
    canonical.current_session_id = activeDevice.current_session_id
  }
  if (duplicateIds.length > 0) {
    canonical.duplicate_count = allIds.length
    canonical.duplicate_device_ids = duplicateIds
    canonical.deduped_device_ids = allIds
  }
  return canonical
}

function dedupeMobileDevices(devices: MobileDeviceRow[]): MobileDeviceRow[] {
  const groups = new Map<string, MobileDeviceRow[]>()
  for (const device of devices) {
    const key = getMobileDeviceDedupeKey(device)
    groups.set(key, [...(groups.get(key) || []), device])
  }
  return Array.from(groups.values())
    .map(mergeDeviceGroup)
    .sort((a, b) => deviceRank(b).localeCompare(deviceRank(a)))
}

function getErrorMessage(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback
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

function PhoneGlyph({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M8 3.5h8A2.5 2.5 0 0 1 18.5 6v12A2.5 2.5 0 0 1 16 20.5H8A2.5 2.5 0 0 1 5.5 18V6A2.5 2.5 0 0 1 8 3.5Z"
        stroke="currentColor"
        strokeWidth="1.35"
        strokeLinejoin="round"
      />
      <path d="M10 18.25h4" stroke="currentColor" strokeWidth="1.35" strokeLinecap="round" />
    </svg>
  )
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
      setDevices(dedupeMobileDevices(nextDevices))
    } catch (err: unknown) {
      setError(getErrorMessage(err, 'Failed to load mobile devices.'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!active) return
    void refreshDevices()
  }, [active, refreshDevices])

  const handleRevokeDevice = useCallback(async (device: MobileDeviceRow) => {
    if (!window.cosmic?.revokeMobileDevice) return
    const deviceIds = getDeviceRegistrationIds(device)
    setRevokingDeviceId(device.device_id)
    setError(null)
    try {
      for (const deviceId of deviceIds) {
        await window.cosmic.revokeMobileDevice(deviceId)
      }
      await refreshDevices()
    } catch (err: unknown) {
      setError(getErrorMessage(err, 'Failed to remove device.'))
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
    } catch (err: unknown) {
      setError(getErrorMessage(err, 'Failed to remove all devices.'))
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
      <header className="mobile-devices-header">
        <div className="mobile-devices-header-top">
          <div className="mobile-devices-header-text">
            <span className="mobile-devices-kicker">Gateway</span>
            <h2 className="mobile-devices-title">Mobile devices</h2>
          </div>
          <div className="mobile-devices-header-actions">
            <button
              type="button"
              className="mobile-devices-btn mobile-devices-btn--subtle"
              onClick={() => void refreshDevices()}
              disabled={loading || revokingAll}
            >
              {loading ? 'Refreshing…' : 'Refresh'}
            </button>
            <button
              type="button"
              className="mobile-devices-btn mobile-devices-btn--danger"
              onClick={() => void handleRevokeAll()}
              disabled={devices.length === 0 || revokingAll}
            >
              {revokingAll ? 'Removing…' : 'Remove all'}
            </button>
          </div>
        </div>
        <p className="mobile-devices-lead">
          Phones linked to this desktop can chat through your VM. Remove a device here to require sign-in again on that phone.
        </p>
      </header>

      <div className="mobile-devices-stats" aria-label="Device summary">
        <div className="mobile-devices-stat">
          <span className="mobile-devices-stat-value">{devices.length}</span>
          <span className="mobile-devices-stat-label">Known</span>
        </div>
        <div className="mobile-devices-stat mobile-devices-stat--accent">
          <span className="mobile-devices-stat-value">{activeCount}</span>
          <span className="mobile-devices-stat-label">Active</span>
        </div>
      </div>

      {error ? (
        <div className="mobile-devices-error" role="alert">
          {error}
        </div>
      ) : null}

      {loading && devices.length === 0 ? (
        <div className="mobile-devices-loading" aria-live="polite">
          <span className="mobile-devices-loading-dot" />
          Loading devices…
        </div>
      ) : null}

      {devices.length === 0 && !loading ? (
        <div className="mobile-devices-empty">
          <div className="mobile-devices-empty-icon" aria-hidden>
            <PhoneGlyph className="mobile-devices-empty-glyph" />
          </div>
          <strong>No phones linked yet</strong>
          <p>A phone appears here after it opens chat against your VM.</p>
        </div>
      ) : null}

      <div className="mobile-devices-list">
        {devices.map((device) => {
          const removingThis = revokingDeviceId === device.device_id
          const sessionId = device.current_session_id || device.last_session_id || ''
          const revoked = !!device.revoked
          return (
            <article
              key={device.device_id}
              className={`mobile-device-card${revoked ? ' is-revoked' : ''}`}
            >
              <div className="mobile-device-card-top">
                <div className="mobile-device-icon-wrap" aria-hidden>
                  <PhoneGlyph className="mobile-device-icon" />
                </div>
                <div className="mobile-device-card-main">
                  <div className="mobile-device-card-title-row">
                    <div className="mobile-device-title-block">
                      <span className="mobile-device-eyebrow">Device</span>
                      <div className="mobile-device-name" title={getDeviceTitle(device)}>
                        {getDeviceTitle(device)}
                      </div>
                    </div>
                    <div className="mobile-device-pill-wrap">
                      {device.active && !device.revoked ? (
                        <span className="mobile-device-pill is-active">Active</span>
                      ) : null}
                      {device.revoked ? <span className="mobile-device-pill is-revoked">Removed</span> : null}
                    </div>
                  </div>
                  <div className="mobile-device-subtitle">
                    {getDeviceSubtitle(device) || 'Metadata not available yet'}
                  </div>
                  {getDeviceSourceLabel(device) ? (
                    <span className="mobile-device-source-chip">{getDeviceSourceLabel(device)}</span>
                  ) : null}
                  <div className="mobile-device-id" title={device.device_id}>
                    {device.device_id}
                  </div>
                  {Number(device.duplicate_count || 0) > 1 ? (
                    <div className="mobile-device-merged-note">
                      Merged {device.duplicate_count} registrations for this phone
                    </div>
                  ) : null}
                </div>
              </div>

              <div className="mobile-device-meta-panel">
                <div className="mobile-device-meta-grid">
                  <div className="mobile-device-meta-cell">
                    <span className="mobile-device-meta-label">First seen</span>
                    <span className="mobile-device-meta-value">{formatTimestamp(device.first_seen_at)}</span>
                  </div>
                  <div className="mobile-device-meta-cell">
                    <span className="mobile-device-meta-label">Last seen</span>
                    <span className="mobile-device-meta-value">{formatTimestamp(device.last_seen_at)}</span>
                  </div>
                  <div className="mobile-device-meta-cell">
                    <span className="mobile-device-meta-label">Connected</span>
                    <span className="mobile-device-meta-value">{formatTimestamp(device.last_connected_at)}</span>
                  </div>
                  <div className="mobile-device-meta-cell">
                    <span className="mobile-device-meta-label">Disconnected</span>
                    <span className="mobile-device-meta-value">{formatTimestamp(device.last_disconnected_at)}</span>
                  </div>
                </div>

                <div className="mobile-device-code-rows">
                  <div className="mobile-device-code-row">
                    <span className="mobile-device-meta-label">Session</span>
                    <span className="mobile-device-session-id" title={sessionId || '—'}>
                      {sessionId || '—'}
                    </span>
                  </div>
                  <div className="mobile-device-code-row">
                    <span className="mobile-device-meta-label">App</span>
                    <span
                      className="mobile-device-session-id"
                      title={
                        compactParts([device.app_version, device.app_build ? `build ${device.app_build}` : null]) || '—'
                      }
                    >
                      {compactParts([device.app_version, device.app_build ? `build ${device.app_build}` : null]) || '—'}
                    </span>
                  </div>
                </div>
              </div>

              {device.revoked_at ? (
                <div className="mobile-device-revoked-note">
                  Removed {formatTimestamp(device.revoked_at)}
                  {device.revoke_reason ? ` · ${device.revoke_reason}` : ''}
                </div>
              ) : null}

              <div className="mobile-device-card-actions">
                <button
                  type="button"
                  className="mobile-devices-btn mobile-devices-btn--danger mobile-devices-btn--compact"
                  onClick={() => void handleRevokeDevice(device)}
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

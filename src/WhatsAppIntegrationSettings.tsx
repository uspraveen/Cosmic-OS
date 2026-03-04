import { useEffect, useState, type CSSProperties } from 'react'
import QRCode from 'qrcode'

interface WhatsAppIntegrationSettingsProps {
  active: boolean
}

interface WhatsAppBridgeStatus {
  status?: string
  connected?: boolean
  pairing_state?: string
  last_disconnect_code?: number | null
  auth_dir?: string
  has_auth_state?: boolean
  qr?: string | null
  qr_updated_at?: string | null
  connected_jid?: string | null
  last_error?: string | null
  bridge_config?: WhatsAppBridgeConfig | null
}

interface WhatsAppBridgeConfig {
  allowed_phone?: string | null
  self_chat_only?: boolean | null
}

type BannerTone = 'success' | 'error' | 'info'

const SETTINGS_KEYS = {
  baseUrl: 'gatewayBaseUrl',
  apiToken: 'gatewayApiToken',
  sshEnabled: 'gatewaySshTunnelEnabled',
  sshHost: 'gatewaySshHost',
  sshPort: 'gatewaySshPort',
  sshUsername: 'gatewaySshUsername',
  sshPrivateKeyPath: 'gatewaySshPrivateKeyPath',
} as const

const WHATSAPP_THEME_STYLE: CSSProperties = {
  ['--accent' as string]: '#25D366',
}

function getPairingLabel(status: WhatsAppBridgeStatus | null) {
  if (!status) return 'Gateway not configured'
  if (status.connected) return 'Connected'

  switch (status.pairing_state) {
    case 'qr_ready':
      return 'Waiting for scan'
    case 'connecting':
      return 'Connecting'
    case 'connected':
      return 'Connected'
    case 'logged_out':
      return 'Logged out'
    case 'disconnected':
      return 'Disconnected'
    case 'error':
      return 'Error'
    default:
      return status.has_auth_state ? 'Ready to reconnect' : 'Not connected'
  }
}

function getStatusDescription(status: WhatsAppBridgeStatus | null) {
  if (!status) {
    return 'Enter your VM Gateway URL and local API token below. Cosmic will use the Gateway control routes, not the bridge port directly.'
  }

  if (status.connected) {
    return 'Your WhatsApp number is linked to the backend bridge on the VM. New messages will flow through Gateway once the rest of the desktop/live pipeline is wired.'
  }

  if (status.qr) {
    return 'Scan the QR with WhatsApp on your phone: Linked devices -> Link a device.'
  }

  if (status.pairing_state === 'connecting') {
    return 'The VM bridge is starting a fresh pairing session.'
  }

  if (status.has_auth_state) {
    return 'Auth state exists on the VM, but the bridge is not currently connected.'
  }

  return 'Request a QR code to link the COSMIC WhatsApp device from this desktop app.'
}

function formatTimestamp(value?: string | null) {
  if (!value) return 'Not yet generated'
  const timestamp = Date.parse(value)
  if (Number.isNaN(timestamp)) return value
  return new Date(timestamp).toLocaleString()
}

function compactValue(value: string, tail = 8) {
  const trimmed = value.trim()
  if (trimmed.length <= tail + 6) return trimmed
  return `${trimmed.slice(0, 6)}...${trimmed.slice(-tail)}`
}

function getErrorMessage(error: unknown, fallback: string) {
  if (error instanceof Error && error.message.trim()) {
    return error.message
  }
  return fallback
}

function buildTunnelPayload(
  enabled: boolean,
  host: string,
  port: string,
  username: string,
  privateKeyPath: string,
) {
  return {
    enabled,
    host: host.trim(),
    port: Number.parseInt(port.trim() || '22', 10) || 22,
    username: username.trim(),
    privateKeyPath: privateKeyPath.trim(),
  }
}

export default function WhatsAppIntegrationSettings({ active }: WhatsAppIntegrationSettingsProps) {
  const [gatewayBaseUrl, setGatewayBaseUrl] = useState('')
  const [gatewayApiToken, setGatewayApiToken] = useState('')
  const [sshTunnelEnabled, setSshTunnelEnabled] = useState(false)
  const [sshHost, setSshHost] = useState('')
  const [sshPort, setSshPort] = useState('22')
  const [sshUsername, setSshUsername] = useState('')
  const [sshPrivateKeyPath, setSshPrivateKeyPath] = useState('')
  const [configLoaded, setConfigLoaded] = useState(false)
  const [status, setStatus] = useState<WhatsAppBridgeStatus | null>(null)
  const [allowedPhone, setAllowedPhone] = useState('')
  const [allowedPhoneDirty, setAllowedPhoneDirty] = useState(false)
  const [statusError, setStatusError] = useState('')
  const [banner, setBanner] = useState<{ tone: BannerTone; message: string } | null>(null)
  const [loadingStatus, setLoadingStatus] = useState(false)
  const [savingConfig, setSavingConfig] = useState(false)
  const [savingBridgeConfig, setSavingBridgeConfig] = useState(false)
  const [pairingBusy, setPairingBusy] = useState(false)
  const [disconnecting, setDisconnecting] = useState(false)
  const [qrDataUrl, setQrDataUrl] = useState('')

  const sshConfigReady =
    sshHost.trim().length > 0 &&
    sshUsername.trim().length > 0 &&
    sshPrivateKeyPath.trim().length > 0
  const configReady =
    gatewayBaseUrl.trim().length > 0 &&
    gatewayApiToken.trim().length > 0 &&
    (!sshTunnelEnabled || sshConfigReady)

  useEffect(() => {
    if (!banner) return
    const timer = window.setTimeout(() => setBanner(null), 3600)
    return () => window.clearTimeout(timer)
  }, [banner])

  useEffect(() => {
    if (!active) return

    const offSettings = window.cosmic?.onSettingsUpdate((settings) => {
      setGatewayBaseUrl(String(settings?.[SETTINGS_KEYS.baseUrl] ?? ''))
      setGatewayApiToken(String(settings?.[SETTINGS_KEYS.apiToken] ?? ''))
      setSshTunnelEnabled(Boolean(settings?.[SETTINGS_KEYS.sshEnabled] ?? false))
      setSshHost(String(settings?.[SETTINGS_KEYS.sshHost] ?? ''))
      setSshPort(String(settings?.[SETTINGS_KEYS.sshPort] ?? '22'))
      setSshUsername(String(settings?.[SETTINGS_KEYS.sshUsername] ?? ''))
      setSshPrivateKeyPath(String(settings?.[SETTINGS_KEYS.sshPrivateKeyPath] ?? ''))
      setConfigLoaded(true)
    })

    window.cosmic?.getSettings()
    return () => {
      offSettings?.()
    }
  }, [active])

  useEffect(() => {
    let cancelled = false

    if (!status?.qr) {
      setQrDataUrl('')
      return
    }

    QRCode.toDataURL(status.qr, {
      width: 280,
      margin: 1,
      errorCorrectionLevel: 'M',
      color: {
        dark: '#111827',
        light: '#ffffff',
      },
    })
      .then((dataUrl) => {
        if (!cancelled) setQrDataUrl(dataUrl)
      })
      .catch(() => {
        if (!cancelled) setQrDataUrl('')
      })

    return () => {
      cancelled = true
    }
  }, [status?.qr])

  useEffect(() => {
    if (!active || !configLoaded || !configReady) {
      if (!configReady) {
        setStatus(null)
        setStatusError('')
        setQrDataUrl('')
        setAllowedPhone('')
        setAllowedPhoneDirty(false)
      }
      return
    }

    let cancelled = false

    const refreshStatus = async (quiet = true) => {
      if (!quiet) setLoadingStatus(true)

      try {
        const nextStatus = await window.cosmic?.getWhatsAppStatus({
          baseUrl: gatewayBaseUrl,
          apiToken: gatewayApiToken,
          tunnel: buildTunnelPayload(
            sshTunnelEnabled,
            sshHost,
            sshPort,
            sshUsername,
            sshPrivateKeyPath,
          ),
        })
        if (cancelled) return
        setStatus(nextStatus ?? null)
        const config = nextStatus?.bridge_config
        if (!allowedPhoneDirty && config && typeof config === 'object') {
          setAllowedPhone(String(config.allowed_phone ?? ''))
        }
        setStatusError('')
      } catch (error: unknown) {
        if (cancelled) return
        setStatus(null)
        setQrDataUrl('')
        setStatusError(getErrorMessage(error, 'Unable to reach the configured Gateway.'))
      } finally {
        if (!cancelled) setLoadingStatus(false)
      }
    }

    void refreshStatus(false)
    const poll = window.setInterval(() => {
      void refreshStatus(true)
    }, 5000)

    return () => {
      cancelled = true
      window.clearInterval(poll)
    }
  }, [
    active,
    allowedPhoneDirty,
    configLoaded,
    configReady,
    gatewayApiToken,
    gatewayBaseUrl,
    sshHost,
    sshPort,
    sshPrivateKeyPath,
    sshTunnelEnabled,
    sshUsername,
  ])

  const persistGatewayConfig = async (successMessage?: string) => {
    const trimmedBaseUrl = gatewayBaseUrl.trim()
    const trimmedToken = gatewayApiToken.trim()
    if (!trimmedBaseUrl || !trimmedToken) {
      throw new Error('Gateway URL and local API token are both required.')
    }

    if (sshTunnelEnabled && !sshConfigReady) {
      throw new Error('SSH tunnel requires host, username, and private key path.')
    }

    setSavingConfig(true)
    try {
      window.cosmic?.saveSetting(SETTINGS_KEYS.baseUrl, trimmedBaseUrl)
      window.cosmic?.saveSetting(SETTINGS_KEYS.apiToken, trimmedToken)
      window.cosmic?.saveSetting(SETTINGS_KEYS.sshEnabled, sshTunnelEnabled)
      window.cosmic?.saveSetting(SETTINGS_KEYS.sshHost, sshHost.trim())
      window.cosmic?.saveSetting(SETTINGS_KEYS.sshPort, sshPort.trim() || '22')
      window.cosmic?.saveSetting(SETTINGS_KEYS.sshUsername, sshUsername.trim())
      window.cosmic?.saveSetting(SETTINGS_KEYS.sshPrivateKeyPath, sshPrivateKeyPath.trim())
      if (successMessage) {
        setBanner({ tone: 'success', message: successMessage })
      }
    } finally {
      setSavingConfig(false)
    }
  }

  const persistBridgeConfig = async (successMessage?: string) => {
    if (!configReady) {
      throw new Error('Save the Gateway connection first.')
    }

    setSavingBridgeConfig(true)
    try {
      const nextConfig = await window.cosmic?.saveWhatsAppConfig({
        baseUrl: gatewayBaseUrl.trim(),
        apiToken: gatewayApiToken.trim(),
        tunnel: buildTunnelPayload(
          sshTunnelEnabled,
          sshHost,
          sshPort,
          sshUsername,
          sshPrivateKeyPath,
        ),
        allowedPhone: allowedPhone.trim() || null,
      })
      const persistedAllowedPhone = String(nextConfig?.allowed_phone ?? '')
      setAllowedPhone(persistedAllowedPhone)
      setAllowedPhoneDirty(false)
      setStatus((current) => current ? {
        ...current,
        bridge_config: {
          ...(current.bridge_config ?? {}),
          allowed_phone: persistedAllowedPhone || null,
        },
      } : current)
      if (successMessage) {
        setBanner({ tone: 'success', message: successMessage })
      }
    } finally {
      setSavingBridgeConfig(false)
    }
  }

  const handleCheckStatus = async () => {
    try {
      await persistGatewayConfig()
      setLoadingStatus(true)
      const nextStatus = await window.cosmic?.getWhatsAppStatus({
        baseUrl: gatewayBaseUrl,
        apiToken: gatewayApiToken,
        tunnel: buildTunnelPayload(
          sshTunnelEnabled,
          sshHost,
          sshPort,
          sshUsername,
          sshPrivateKeyPath,
        ),
      })
      setStatus(nextStatus ?? null)
      const config = nextStatus?.bridge_config
      if (!allowedPhoneDirty && config && typeof config === 'object') {
        setAllowedPhone(String(config.allowed_phone ?? ''))
      }
      setStatusError('')
      setBanner({ tone: 'success', message: 'Gateway connection verified.' })
    } catch (error: unknown) {
      const message = getErrorMessage(error, 'Unable to reach the configured Gateway.')
      setStatus(null)
      setQrDataUrl('')
      setStatusError(message)
      setBanner({ tone: 'error', message })
    } finally {
      setLoadingStatus(false)
    }
  }

  const handleRequestQr = async () => {
    try {
      await persistGatewayConfig()
      await persistBridgeConfig()
      setPairingBusy(true)
      const nextStatus = await window.cosmic?.requestWhatsAppPairingQr({
        baseUrl: gatewayBaseUrl,
        apiToken: gatewayApiToken,
        tunnel: buildTunnelPayload(
          sshTunnelEnabled,
          sshHost,
          sshPort,
          sshUsername,
          sshPrivateKeyPath,
        ),
        refresh: true,
        waitTimeoutMs: 20000,
      })
      setStatus(nextStatus ?? null)
      const config = nextStatus?.bridge_config
      if (config && typeof config === 'object') {
        setAllowedPhone(String(config.allowed_phone ?? ''))
        setAllowedPhoneDirty(false)
      }
      setStatusError('')
      setBanner({
        tone: nextStatus?.qr ? 'info' : 'success',
        message: nextStatus?.qr
          ? 'QR generated. Scan it with WhatsApp on your phone.'
          : 'Pairing request sent to the backend bridge.',
      })
    } catch (error: unknown) {
      const message = getErrorMessage(error, 'Failed to request pairing QR.')
      setBanner({ tone: 'error', message })
      setStatusError(message)
    } finally {
      setPairingBusy(false)
    }
  }

  const handleClearSession = async () => {
    try {
      await persistGatewayConfig()
      setDisconnecting(true)
      const nextStatus = await window.cosmic?.clearWhatsAppSession({
        baseUrl: gatewayBaseUrl,
        apiToken: gatewayApiToken,
        tunnel: buildTunnelPayload(
          sshTunnelEnabled,
          sshHost,
          sshPort,
          sshUsername,
          sshPrivateKeyPath,
        ),
      })
      setStatus(nextStatus ?? null)
      setStatusError('')
      setQrDataUrl('')
      setBanner({ tone: 'success', message: 'WhatsApp session cleared on the VM.' })
    } catch (error: unknown) {
      const message = getErrorMessage(error, 'Failed to clear the WhatsApp session.')
      setBanner({ tone: 'error', message })
      setStatusError(message)
    } finally {
      setDisconnecting(false)
    }
  }

  if (!active) return null

  return (
    <div className="setting-subpage cosmic-google-page cosmic-whatsapp-page" style={WHATSAPP_THEME_STYLE}>
      <div className="cosmic-google-hero">
        <div className="cosmic-google-hero-inner">
          <div className="cosmic-google-hero-icon cosmic-whatsapp-hero-icon" aria-hidden="true">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="white" xmlns="http://www.w3.org/2000/svg">
              <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.888-.788-1.489-1.761-1.663-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51a12.8 12.8 0 0 0-.57-.01c-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 0 1-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 0 1-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 0 1 2.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0 0 12.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 0 0 5.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 0 0-3.48-8.413z" />
            </svg>
          </div>
          <div className="cosmic-google-hero-text">
            <h3>
              WhatsApp
              <span className="cosmic-whatsapp-beta">Beta</span>
            </h3>
            <p className="cosmic-google-hero-stat">{getPairingLabel(status)}</p>
            <p className="cosmic-google-hero-desc">{getStatusDescription(status)}</p>
          </div>
        </div>
        <div className="cosmic-whatsapp-hero-actions">
          <button
            type="button"
            className="cosmic-google-cta cosmic-whatsapp-cta-secondary"
            onClick={handleCheckStatus}
            disabled={loadingStatus || savingConfig}
          >
            {loadingStatus ? 'Checking…' : 'Check status'}
          </button>
          <button
            type="button"
            className="cosmic-google-cta"
            onClick={handleRequestQr}
            disabled={pairingBusy || savingConfig}
          >
            {pairingBusy ? 'Preparing…' : status?.qr ? 'Refresh QR' : 'Connect WhatsApp'}
          </button>
        </div>
      </div>

      {banner && (
        <div className={`cosmic-google-banner cosmic-google-banner-${banner.tone}`} role="status">
          {banner.message}
        </div>
      )}

      <div className="cosmic-google-card cosmic-whatsapp-config-card">
        <div className="cosmic-google-card-header">
          <div className="cosmic-google-card-profile">
            <div className="cosmic-google-avatar cosmic-whatsapp-avatar" aria-hidden="true">
              VM
            </div>
            <div className="cosmic-google-card-info">
              <strong>Gateway connection</strong>
              <span>Use the public Gateway URL and the desktop local API token from the VM.</span>
            </div>
          </div>
          <div className="cosmic-google-card-badges">
            <span className={`cosmic-google-badge ${configReady ? 'status-connected' : 'status-needs_auth'}`}>
              {configReady ? 'Configured' : 'Needs setup'}
            </span>
          </div>
        </div>

        <div className="cosmic-google-card-body">
          <div className="cosmic-google-field">
            <label>Gateway Base URL</label>
            <input
              className="cosmic-google-input"
              value={gatewayBaseUrl}
              onChange={(event) => setGatewayBaseUrl(event.target.value)}
              placeholder="https://your-vm-domain or http://127.0.0.1:8080"
              spellCheck={false}
            />
          </div>

          <div className="cosmic-google-field">
            <label>Gateway Local API Token</label>
            <input
              className="cosmic-google-input"
              type="password"
              value={gatewayApiToken}
              onChange={(event) => setGatewayApiToken(event.target.value)}
              placeholder="Paste the desktop/local Gateway token"
              spellCheck={false}
            />
          </div>

          <div className="cosmic-google-field">
            <label className="cosmic-google-toggle">
              <input
                type="checkbox"
                checked={sshTunnelEnabled}
                onChange={(event) => setSshTunnelEnabled(event.target.checked)}
              />
              <span>Use SSH tunnel for private VM access</span>
            </label>
          </div>

          {sshTunnelEnabled && (
            <>
              <div className="cosmic-google-field">
                <label>SSH Host</label>
                <input
                  className="cosmic-google-input"
                  value={sshHost}
                  onChange={(event) => setSshHost(event.target.value)}
                  placeholder="ec2-...compute.amazonaws.com"
                  spellCheck={false}
                />
              </div>

              <div className="cosmic-google-field">
                <label>SSH Port</label>
                <input
                  className="cosmic-google-input"
                  value={sshPort}
                  onChange={(event) => setSshPort(event.target.value)}
                  placeholder="22"
                  spellCheck={false}
                />
              </div>

              <div className="cosmic-google-field">
                <label>SSH Username</label>
                <input
                  className="cosmic-google-input"
                  value={sshUsername}
                  onChange={(event) => setSshUsername(event.target.value)}
                  placeholder="ubuntu"
                  spellCheck={false}
                />
              </div>

              <div className="cosmic-google-field">
                <label>SSH Private Key Path</label>
                <input
                  className="cosmic-google-input"
                  value={sshPrivateKeyPath}
                  onChange={(event) => setSshPrivateKeyPath(event.target.value)}
                  placeholder="C:\\Users\\you\\Downloads\\server-key.pem"
                  spellCheck={false}
                />
              </div>
            </>
          )}

          <div className="cosmic-google-field">
            <label>Allowed User Phone Number</label>
            <input
              className="cosmic-google-input"
              value={allowedPhone}
              onChange={(event) => {
                setAllowedPhone(event.target.value)
                setAllowedPhoneDirty(true)
              }}
              placeholder="+1 555 123 4567"
              spellCheck={false}
            />
          </div>

          <p className="cosmic-google-apps-hint">
            Do not enter the WhatsApp bridge port here. The desktop app must talk to the Gateway only, and the Gateway will call the internal bridge. If your VM is not publicly exposing the Gateway, enable the SSH tunnel and point the Gateway Base URL at the VM-local Gateway address such as http://127.0.0.1:8080. If an allowed user phone number is set, the bridge will only accept inbound messages from that number and will only send replies back to it.
          </p>

          <div className="cosmic-google-card-actions">
            <button
              type="button"
              className="cosmic-google-action primary"
              onClick={() => {
                void persistGatewayConfig('Gateway connection saved locally.')
              }}
              disabled={savingConfig || !configReady}
            >
              {savingConfig ? 'Saving…' : 'Save connection'}
            </button>
            <button
              type="button"
              className="cosmic-google-action secondary"
              onClick={() => {
                void persistBridgeConfig('Allowed WhatsApp number saved on the VM.')
              }}
              disabled={savingBridgeConfig || !configReady}
            >
              {savingBridgeConfig ? 'Saving…' : 'Save user number'}
            </button>
          </div>

          {statusError && <p className="cosmic-whatsapp-error-note">{statusError}</p>}
        </div>
      </div>

      <div className="cosmic-google-card">
        <div className="cosmic-google-card-header">
          <div className="cosmic-google-card-profile">
            <div className={`cosmic-google-avatar ${status?.connected ? 'connected' : ''}`} aria-hidden="true">
              WA
            </div>
            <div className="cosmic-google-card-info">
              <strong>Bridge status</strong>
              <span>Live state reported by the VM Gateway and WhatsApp bridge.</span>
            </div>
          </div>
          <div className="cosmic-google-card-badges">
            <span className={`cosmic-google-badge ${status?.connected ? 'status-connected' : 'status-needs_auth'}`}>
              {status?.connected ? 'Connected' : 'Pending'}
            </span>
          </div>
        </div>

        <div className="cosmic-google-card-body">
          <div className="cosmic-whatsapp-status-grid">
            <div className="cosmic-whatsapp-status-row">
              <span>Pairing state</span>
              <strong>{getPairingLabel(status)}</strong>
            </div>
            <div className="cosmic-whatsapp-status-row">
              <span>Linked JID</span>
              <strong>{status?.connected_jid ? compactValue(status.connected_jid) : 'Not linked yet'}</strong>
            </div>
            <div className="cosmic-whatsapp-status-row">
              <span>Auth state on VM</span>
              <strong>{status?.has_auth_state ? 'Present' : 'Missing'}</strong>
            </div>
            <div className="cosmic-whatsapp-status-row">
              <span>Latest QR</span>
              <strong>{formatTimestamp(status?.qr_updated_at)}</strong>
            </div>
          </div>

          {status?.last_error && (
            <p className="cosmic-whatsapp-error-note">Last bridge error: {status.last_error}</p>
          )}
        </div>

        <div className="cosmic-google-card-footer">
          <p className="cosmic-google-card-note">
            Once paired, the bridge keeps the auth state on the VM so you do not need to scan again after normal restarts.
          </p>
          <div className="cosmic-google-card-actions">
            <button
              type="button"
              className="cosmic-google-action secondary"
              onClick={handleCheckStatus}
              disabled={loadingStatus}
            >
              Refresh status
            </button>
            <button
              type="button"
              className="cosmic-google-action danger"
              onClick={handleClearSession}
              disabled={disconnecting || !configReady}
            >
              {disconnecting ? 'Clearing…' : 'Unlink number'}
            </button>
          </div>
        </div>
      </div>

      <div className="cosmic-google-card cosmic-whatsapp-qr-card">
        <div className="cosmic-google-card-header">
          <div className="cosmic-google-card-profile">
            <div className="cosmic-google-avatar cosmic-whatsapp-avatar" aria-hidden="true">
              QR
            </div>
            <div className="cosmic-google-card-info">
              <strong>Pairing QR</strong>
              <span>Request a QR from the Gateway, then scan it from your phone&apos;s WhatsApp app.</span>
            </div>
          </div>
        </div>

        <div className="cosmic-google-card-body cosmic-whatsapp-qr-body">
          {qrDataUrl ? (
            <>
              <div className="cosmic-whatsapp-qr-panel">
                <img src={qrDataUrl} alt="WhatsApp pairing QR code" className="cosmic-whatsapp-qr-image" />
              </div>
              <ol className="cosmic-whatsapp-steps">
                <li>Open WhatsApp on your phone.</li>
                <li>Go to Settings or Menu -&gt; Linked devices.</li>
                <li>Choose Link a device and scan this QR.</li>
                <li>Keep this window open until the bridge status changes to Connected.</li>
              </ol>
            </>
          ) : (
            <div className="cosmic-google-empty cosmic-google-empty-invite cosmic-whatsapp-empty">
              <div className="cosmic-google-empty-icon" aria-hidden="true">
                <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="3" y="3" width="18" height="18" rx="2" ry="2" />
                  <rect x="7" y="7" width="3" height="3" />
                  <rect x="14" y="7" width="3" height="3" />
                  <rect x="7" y="14" width="3" height="3" />
                  <rect x="14" y="14" width="3" height="3" />
                </svg>
              </div>
              <p className="cosmic-google-empty-title">No QR generated yet</p>
              <p className="cosmic-google-empty-desc">
                Save your VM Gateway connection details, then click Connect WhatsApp. Cosmic will ask the Gateway for a fresh QR and render it here locally.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

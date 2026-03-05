import { useEffect, useState } from 'react'
import QRCode from 'qrcode'

interface WhatsAppIntegrationSettingsProps {
  active: boolean
  cosmicAuth?: { gatewayUrl: string; gatewayApiToken: string }
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
} as const

function getPairingLabel(status: WhatsAppBridgeStatus | null) {
  if (!status) return 'Not configured'
  if (status.connected) return 'Connected'
  switch (status.pairing_state) {
    case 'qr_ready': return 'Waiting for scan'
    case 'connecting': return 'Connecting'
    case 'connected': return 'Connected'
    case 'logged_out': return 'Logged out'
    case 'disconnected': return 'Disconnected'
    case 'error': return 'Error'
    default: return status.has_auth_state ? 'Ready' : 'Not connected'
  }
}

function getErrorMessage(error: unknown, fallback: string) {
  if (error instanceof Error && error.message.trim()) return error.message
  return fallback
}

export default function WhatsAppIntegrationSettings({ active, cosmicAuth }: WhatsAppIntegrationSettingsProps) {
  const authManaged = !!cosmicAuth
  const [gatewayBaseUrl, setGatewayBaseUrl] = useState('')
  const [gatewayApiToken, setGatewayApiToken] = useState('')
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
  const [sendingTest, setSendingTest] = useState(false)
  const [qrDataUrl, setQrDataUrl] = useState('')
  const [showDetails, setShowDetails] = useState(false)

  const configReady = gatewayBaseUrl.trim().length > 0 && gatewayApiToken.trim().length > 0

  // Banner auto-dismiss
  useEffect(() => {
    if (!banner) return
    const timer = window.setTimeout(() => setBanner(null), 4000)
    return () => window.clearTimeout(timer)
  }, [banner])

  // Load settings
  useEffect(() => {
    if (!active) return
    const offSettings = window.cosmic?.onSettingsUpdate((settings) => {
      setGatewayBaseUrl(String(settings?.[SETTINGS_KEYS.baseUrl] ?? ''))
      setGatewayApiToken(String(settings?.[SETTINGS_KEYS.apiToken] ?? ''))
      setConfigLoaded(true)
    })
    window.cosmic?.getSettings()
    return () => { offSettings?.() }
  }, [active])

  // QR rendering
  useEffect(() => {
    let cancelled = false
    if (!status?.qr) { setQrDataUrl(''); return }
    QRCode.toDataURL(status.qr, {
      width: 260, margin: 1, errorCorrectionLevel: 'M',
      color: { dark: '#111827', light: '#ffffff' },
    })
      .then((url) => { if (!cancelled) setQrDataUrl(url) })
      .catch(() => { if (!cancelled) setQrDataUrl('') })
    return () => { cancelled = true }
  }, [status?.qr])

  // Status polling
  useEffect(() => {
    if (!active || !configLoaded || !configReady) {
      if (!configReady) { setStatus(null); setStatusError(''); setQrDataUrl(''); setAllowedPhone(''); setAllowedPhoneDirty(false) }
      return
    }
    let cancelled = false
    const refreshStatus = async (quiet = true) => {
      if (!quiet) setLoadingStatus(true)
      try {
        const s = await window.cosmic?.getWhatsAppStatus({ baseUrl: gatewayBaseUrl, apiToken: gatewayApiToken })
        if (cancelled) return
        setStatus(s ?? null)
        if (!allowedPhoneDirty && s?.bridge_config) setAllowedPhone(String(s.bridge_config.allowed_phone ?? ''))
        setStatusError('')
      } catch (e: unknown) {
        if (cancelled) return
        setStatus(null); setQrDataUrl('')
        setStatusError(getErrorMessage(e, 'Unable to reach Gateway.'))
      } finally {
        if (!cancelled) setLoadingStatus(false)
      }
    }
    void refreshStatus(false)
    const poll = window.setInterval(() => void refreshStatus(true), 5000)
    return () => { cancelled = true; window.clearInterval(poll) }
  }, [active, allowedPhoneDirty, configLoaded, configReady, gatewayApiToken, gatewayBaseUrl])

  const persistGatewayConfig = async (msg?: string) => {
    const url = gatewayBaseUrl.trim(); const token = gatewayApiToken.trim()
    if (!url || !token) throw new Error('Gateway URL and API token are required.')
    setSavingConfig(true)
    try {
      window.cosmic?.saveSetting(SETTINGS_KEYS.baseUrl, url)
      window.cosmic?.saveSetting(SETTINGS_KEYS.apiToken, token)
      if (msg) setBanner({ tone: 'success', message: msg })
    } finally { setSavingConfig(false) }
  }

  const persistBridgeConfig = async (msg?: string) => {
    if (!configReady) throw new Error('Save connection first.')
    setSavingBridgeConfig(true)
    try {
      const r = await window.cosmic?.saveWhatsAppConfig({
        baseUrl: gatewayBaseUrl.trim(), apiToken: gatewayApiToken.trim(),
        allowedPhone: allowedPhone.trim() || null,
      })
      const phone = String(r?.allowed_phone ?? '')
      setAllowedPhone(phone); setAllowedPhoneDirty(false)
      setStatus((c) => c ? { ...c, bridge_config: { ...(c.bridge_config ?? {}), allowed_phone: phone || null } } : c)
      if (msg) setBanner({ tone: 'success', message: msg })
    } finally { setSavingBridgeConfig(false) }
  }

  const handleCheckStatus = async () => {
    try {
      await persistGatewayConfig()
      setLoadingStatus(true)
      const s = await window.cosmic?.getWhatsAppStatus({ baseUrl: gatewayBaseUrl, apiToken: gatewayApiToken })
      setStatus(s ?? null)
      if (!allowedPhoneDirty && s?.bridge_config) setAllowedPhone(String(s.bridge_config.allowed_phone ?? ''))
      setStatusError(''); setBanner({ tone: 'success', message: 'Connection verified.' })
    } catch (e: unknown) {
      const msg = getErrorMessage(e, 'Unable to reach Gateway.')
      setStatus(null); setQrDataUrl(''); setStatusError(msg); setBanner({ tone: 'error', message: msg })
    } finally { setLoadingStatus(false) }
  }

  const handleRequestQr = async () => {
    try {
      await persistGatewayConfig(); await persistBridgeConfig()
      setPairingBusy(true)
      const s = await window.cosmic?.requestWhatsAppPairingQr({
        baseUrl: gatewayBaseUrl, apiToken: gatewayApiToken, refresh: true, waitTimeoutMs: 20000,
      })
      setStatus(s ?? null)
      if (s?.bridge_config) { setAllowedPhone(String(s.bridge_config.allowed_phone ?? '')); setAllowedPhoneDirty(false) }
      setStatusError('')
      setBanner({ tone: s?.qr ? 'info' : 'success', message: s?.qr ? 'Scan the QR code with WhatsApp.' : 'Pairing request sent.' })
    } catch (e: unknown) {
      const msg = getErrorMessage(e, 'Failed to request QR.')
      const is408 = msg.includes('408') || msg.includes('timeout') || msg.includes('Timeout')
      if (is408) {
        setBanner({ tone: 'info', message: 'Waiting for QR code… this may take a moment.' })
      } else {
        setBanner({ tone: 'error', message: msg }); setStatusError(msg)
      }
    } finally { setPairingBusy(false) }
  }

  const handleClearSession = async () => {
    try {
      await persistGatewayConfig(); setDisconnecting(true)
      const s = await window.cosmic?.clearWhatsAppSession({ baseUrl: gatewayBaseUrl, apiToken: gatewayApiToken })
      setStatus(s ?? null); setStatusError(''); setQrDataUrl('')
      setBanner({ tone: 'success', message: 'WhatsApp session cleared.' })
    } catch (e: unknown) {
      const msg = getErrorMessage(e, 'Failed to clear session.'); setBanner({ tone: 'error', message: msg }); setStatusError(msg)
    } finally { setDisconnecting(false) }
  }

  const handleSendTest = async () => {
    const phone = allowedPhone.trim()
    if (!phone || !configReady) return
    setSendingTest(true)
    try {
      await window.cosmic?.sendWhatsAppTest({
        baseUrl: gatewayBaseUrl, apiToken: gatewayApiToken, number: phone, message: 'Hey! Cosmic here - If you got this, we\'re connected. Let\'s get something epic done.',
      })
      setBanner({ tone: 'success', message: 'Test message sent!' })
    } catch (e: unknown) {
      setBanner({ tone: 'error', message: getErrorMessage(e, 'Failed to send test message.') })
    } finally { setSendingTest(false) }
  }

  if (!active) return null

  const isConnected = !!status?.connected

  return (
    <div className="setting-subpage cosmic-wa-page">

      {/* ── Identity ── */}
      <div className="cosmic-wa-identity">
        <div className="cosmic-wa-eyebrow-row">
          <span className="cosmic-wa-eyebrow">WhatsApp</span>
          <span className="cosmic-wa-eyebrow soft">Beta</span>
        </div>
        <h2 className="cosmic-wa-title">{isConnected ? 'You\u2019re connected' : 'Link your account'}</h2>
        <p className="cosmic-wa-subtitle">Send and receive messages through your WhatsApp number.</p>
      </div>

      {/* ── Status strip ── */}
      <div className="cosmic-wa-status-strip">
        <div className="cosmic-wa-status-left">
          <span className={`cosmic-wa-live-dot ${isConnected ? 'on' : ''}`} />
          <span className="cosmic-wa-status-text">{getPairingLabel(status)}</span>
          {status?.connected_jid && (
            <span className="cosmic-wa-status-jid">{status.connected_jid}</span>
          )}
        </div>
        <button
          type="button"
          className="cosmic-wa-cta"
          onClick={isConnected ? handleCheckStatus : handleRequestQr}
          disabled={pairingBusy || loadingStatus || (!configReady && !isConnected)}
        >
          {pairingBusy ? 'Connecting…' : loadingStatus ? 'Checking…' : isConnected ? 'Refresh' : 'Connect'}
        </button>
      </div>

      {/* ── Banner ── */}
      {banner && (
        <div className={`cosmic-wa-banner ${banner.tone}`} role="status">
          <span className="cosmic-wa-banner-icon">
            {banner.tone === 'success' ? '✓' : banner.tone === 'error' ? '✕' : 'ℹ'}
          </span>
          {banner.message}
        </div>
      )}

      {/* ── Connection Setup ── */}
      {!authManaged && (
        <div className="cosmic-wa-tile">
          <div className="cosmic-wa-tile-head">
            <span>Connection</span>
            <span className={`cosmic-wa-meta-pill ${configReady ? 'ready' : ''}`}>
              {configReady ? 'Configured' : 'Required'}
            </span>
          </div>
          <div className="cosmic-wa-tile-body">
            <div className="cosmic-wa-inner-surface">
              <label className="cosmic-wa-field">
                <span>Gateway URL</span>
                <input
                  value={gatewayBaseUrl}
                  onChange={(e) => setGatewayBaseUrl(e.target.value)}
                  placeholder="http://your-vm-ip:8080"
                  spellCheck={false}
                />
              </label>
              <label className="cosmic-wa-field">
                <span>API Token</span>
                <input
                  type="password"
                  value={gatewayApiToken}
                  onChange={(e) => setGatewayApiToken(e.target.value)}
                  placeholder="Gateway API token"
                  spellCheck={false}
                />
              </label>
            </div>
            <div className="cosmic-wa-actions">
              <button
                type="button"
                className="cosmic-wa-btn secondary"
                onClick={() => void persistGatewayConfig('Connection saved.')}
                disabled={savingConfig || !configReady}
              >
                {savingConfig ? 'Saving…' : 'Save'}
              </button>
              <button
                type="button"
                className="cosmic-wa-btn ghost"
                onClick={handleCheckStatus}
                disabled={loadingStatus || savingConfig}
              >
                {loadingStatus ? 'Checking…' : 'Verify'}
              </button>
            </div>
            {statusError && <p className="cosmic-wa-error">{statusError}</p>}
          </div>
        </div>
      )}

      {/* ── QR Pairing ── */}
      {qrDataUrl && (
        <div className="cosmic-wa-tile cosmic-wa-tile-center">
          <div className="cosmic-wa-tile-head">
            <span>Pair your device</span>
          </div>
          <div className="cosmic-wa-tile-body">
            <div className="cosmic-wa-qr-well">
              <img src={qrDataUrl} alt="WhatsApp pairing QR" />
            </div>
            <div className="cosmic-wa-qr-steps">
              <p><span className="cosmic-wa-step-num">1</span> Open WhatsApp on your phone</p>
              <p><span className="cosmic-wa-step-num">2</span> Tap <strong>Linked devices</strong></p>
              <p><span className="cosmic-wa-step-num">3</span> Tap <strong>Link a device</strong></p>
              <p><span className="cosmic-wa-step-num">4</span> Point your phone at this QR</p>
            </div>
          </div>
        </div>
      )}

      {/* ── Messaging ── */}
      {configReady && (
        <div className="cosmic-wa-tile">
          <div className="cosmic-wa-tile-head">
            <span>Messaging</span>
            <span className={`cosmic-wa-meta-pill ${isConnected ? 'ready' : 'subtle'}`}>
              {isConnected ? 'Ready' : 'Connect first'}
            </span>
          </div>
          <div className="cosmic-wa-tile-body">
            <div className="cosmic-wa-inner-surface">
              <label className="cosmic-wa-field">
                <span>Cosmic texts me at</span>
                <input
                  value={allowedPhone}
                  onChange={(e) => { setAllowedPhone(e.target.value); setAllowedPhoneDirty(true) }}
                  placeholder="+1 555 123 4567"
                  spellCheck={false}
                />
              </label>
            </div>
            <div className="cosmic-wa-actions">
              <button
                type="button"
                className="cosmic-wa-btn secondary"
                onClick={() => void persistBridgeConfig('Phone number saved.')}
                disabled={savingBridgeConfig || !configReady}
              >
                {savingBridgeConfig ? 'Saving…' : 'Save'}
              </button>
              {isConnected && allowedPhone.trim() && (
                <button
                  type="button"
                  className="cosmic-wa-btn primary"
                  onClick={handleSendTest}
                  disabled={sendingTest}
                >
                  {sendingTest ? 'Sending…' : 'Send test'}
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {/* ── Diagnostics ── */}
      {status && (
        <div className="cosmic-wa-tile cosmic-wa-tile-flush">
          <button
            type="button"
            className="cosmic-wa-diag-toggle"
            onClick={() => setShowDetails(!showDetails)}
          >
            <span>Bridge diagnostics</span>
            <span className="cosmic-wa-diag-chevron" data-open={showDetails}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="m9 18 6-6-6-6" /></svg>
            </span>
          </button>
          {showDetails && (
            <div className="cosmic-wa-diag-grid">
              <div className="cosmic-wa-diag-cell">
                <span>State</span>
                <strong>{getPairingLabel(status)}</strong>
              </div>
              <div className="cosmic-wa-diag-cell">
                <span>Auth</span>
                <strong>{status.has_auth_state ? 'Present' : 'None'}</strong>
              </div>
              <div className="cosmic-wa-diag-cell">
                <span>Device JID</span>
                <strong>{status.connected_jid || '—'}</strong>
              </div>
              {status.last_error && (
                <div className="cosmic-wa-diag-cell full error">
                  <span>Last Error</span>
                  <strong>{status.last_error}</strong>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* ── Session actions ── */}
      {(isConnected || status?.has_auth_state) && (
        <div className="cosmic-wa-session-strip">
          <p className="cosmic-wa-session-note">
            {isConnected ? `Linked as ${status?.connected_jid || 'unknown'}` : 'Session data present. Unlink to clear.'}
          </p>
          <div className="cosmic-wa-session-actions">
            <button
              type="button"
              className="cosmic-wa-btn ghost"
              onClick={handleCheckStatus}
              disabled={loadingStatus || savingConfig}
            >
              {loadingStatus ? 'Checking…' : 'Check status'}
            </button>
            <button
              type="button"
              className="cosmic-wa-btn danger"
              onClick={handleClearSession}
              disabled={disconnecting}
            >
              {disconnecting ? 'Clearing…' : 'Unlink'}
            </button>
          </div>
        </div>
      )}

      {authManaged && (
        <p className="cosmic-wa-footer-note">Auto-configured via Cosmic API key.</p>
      )}
    </div>
  )
}

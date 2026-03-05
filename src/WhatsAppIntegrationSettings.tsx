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

      {/* ── Hero ── */}
      <div className="cosmic-wa-hero">
        <div className="cosmic-wa-hero-inner">
          <div className="cosmic-wa-hero-icon" aria-hidden="true">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="#25D366" xmlns="http://www.w3.org/2000/svg">
              <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.888-.788-1.489-1.761-1.663-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51a12.8 12.8 0 0 0-.57-.01c-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 0 1-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 0 1-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 0 1 2.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0 0 12.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 0 0 5.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 0 0-3.48-8.413z" />
            </svg>
          </div>
          <div className="cosmic-wa-hero-text">
            <h3>WhatsApp <span className="cosmic-wa-beta">Beta</span></h3>
            <p className="cosmic-wa-hero-stat">{isConnected ? 'Account linked' : 'No account linked'}</p>
            <p className="cosmic-wa-hero-desc">Send and receive messages through your WhatsApp account.</p>
          </div>
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

      {/* ── Connection Setup (manual gateway config) ── */}
      {!authManaged && (
        <div className="cosmic-wa-section">
          <div className="cosmic-wa-section-head">
            <span className="cosmic-wa-section-label">Connection</span>
            <span className="cosmic-wa-section-hint">{authManaged ? 'Auto-configured' : configReady ? 'Configured' : 'Required'}</span>
          </div>
          <div className="cosmic-wa-section-card">
            <div className="cosmic-wa-field">
              <label>Gateway URL</label>
              <input
                className="cosmic-wa-input"
                value={gatewayBaseUrl}
                onChange={(e) => setGatewayBaseUrl(e.target.value)}
                placeholder="http://your-vm-ip:8080"
                spellCheck={false}
              />
            </div>
            <div className="cosmic-wa-field">
              <label>API Token</label>
              <input
                className="cosmic-wa-input"
                type="password"
                value={gatewayApiToken}
                onChange={(e) => setGatewayApiToken(e.target.value)}
                placeholder="Gateway API token"
                spellCheck={false}
              />
            </div>
            <div className="cosmic-wa-section-actions">
              <button
                type="button"
                className="cosmic-wa-btn secondary"
                onClick={() => void persistGatewayConfig('Connection saved.')}
                disabled={savingConfig || !configReady}
              >
                {savingConfig ? 'Saving…' : 'Save connection'}
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
            {statusError && (
              <p className="cosmic-wa-field-error">{statusError}</p>
            )}
          </div>
        </div>
      )}

      {/* ── QR Pairing ── */}
      {qrDataUrl && (
        <div className="cosmic-wa-section">
          <div className="cosmic-wa-section-head">
            <span className="cosmic-wa-section-label">Pair your device</span>
          </div>
          <div className="cosmic-wa-qr-card">
            <div className="cosmic-wa-qr-frame">
              <img src={qrDataUrl} alt="WhatsApp pairing QR" className="cosmic-wa-qr-img" />
            </div>
            <div className="cosmic-wa-qr-steps">
              <p className="cosmic-wa-qr-step"><span className="cosmic-wa-step-num">1</span> Open WhatsApp on your phone</p>
              <p className="cosmic-wa-qr-step"><span className="cosmic-wa-step-num">2</span> Tap <strong>Linked devices</strong></p>
              <p className="cosmic-wa-qr-step"><span className="cosmic-wa-step-num">3</span> Tap <strong>Link a device</strong></p>
              <p className="cosmic-wa-qr-step"><span className="cosmic-wa-step-num">4</span> Point your phone at this QR</p>
            </div>
          </div>
        </div>
      )}

      {/* ── Phone & Messaging ── */}
      {configReady && (
        <div className="cosmic-wa-section">
          <div className="cosmic-wa-section-head">
            <span className="cosmic-wa-section-label">Messaging</span>
            <span className="cosmic-wa-section-hint">{isConnected ? 'Ready to message' : 'Connect first'}</span>
          </div>
          <div className="cosmic-wa-section-card">
            <div className="cosmic-wa-field">
              <label>Cosmic texts me at</label>
              <input
                className="cosmic-wa-input"
                value={allowedPhone}
                onChange={(e) => { setAllowedPhone(e.target.value); setAllowedPhoneDirty(true) }}
                placeholder="+1 555 123 4567"
                spellCheck={false}
              />
            </div>
            <div className="cosmic-wa-section-actions">
              <button
                type="button"
                className="cosmic-wa-btn secondary"
                onClick={() => void persistBridgeConfig('Phone number saved.')}
                disabled={savingBridgeConfig || !configReady}
              >
                {savingBridgeConfig ? 'Saving…' : 'Save number'}
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
        <div className="cosmic-wa-section">
          <button
            type="button"
            className="cosmic-wa-details-toggle"
            onClick={() => setShowDetails(!showDetails)}
          >
            <span className="cosmic-wa-section-label">Bridge diagnostics</span>
            <span className="cosmic-wa-details-chevron" data-open={showDetails}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="m9 18 6-6-6-6" /></svg>
            </span>
          </button>

          {showDetails && (
            <div className="cosmic-wa-details-grid">
              <div className="cosmic-wa-detail">
                <span>State</span>
                <strong>{getPairingLabel(status)}</strong>
              </div>
              <div className="cosmic-wa-detail">
                <span>Auth</span>
                <strong>{status.has_auth_state ? 'Present' : 'None'}</strong>
              </div>
              <div className="cosmic-wa-detail">
                <span>Device JID</span>
                <strong>{status.connected_jid || '—'}</strong>
              </div>
              {status.last_error && (
                <div className="cosmic-wa-detail cosmic-wa-detail-full cosmic-wa-detail-error">
                  <span>Last Error</span>
                  <strong>{status.last_error}</strong>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* ── Unlink footer ── */}
      {(isConnected || status?.has_auth_state) && (
        <div className="cosmic-wa-card-footer">
          <p className="cosmic-wa-card-note">
            {isConnected ? `Linked as ${status?.connected_jid || 'unknown'}` : 'Session data present. Unlink to clear.'}
          </p>
          <div className="cosmic-wa-card-actions">
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

      {/* ── Footer note ── */}
      {authManaged && (
        <p className="cosmic-wa-footer-note">Auto-configured via Cosmic API key.</p>
      )}
    </div>
  )
}

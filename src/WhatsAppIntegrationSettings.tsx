import { useEffect, useRef, useState } from 'react'
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

function getErrorMessage(error: unknown, fallback: string) {
  if (error instanceof Error && error.message.trim()) return error.message
  return fallback
}

function formatQrTimestamp(value?: string | null) {
  if (!value) return 'Waiting for QR update'
  const parsed = Date.parse(value)
  if (!Number.isFinite(parsed)) return 'QR refreshed recently'
  const seconds = Math.max(0, Math.floor((Date.now() - parsed) / 1000))
  if (seconds < 60) return `Refreshed ${seconds}s ago`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `Refreshed ${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `Refreshed ${hours}h ago`
  return `Refreshed ${Math.floor(hours / 24)}d ago`
}

function getBannerIcon(tone: BannerTone) {
  switch (tone) {
    case 'success':
      return 'OK'
    case 'error':
      return '!'
    default:
      return 'i'
  }
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

  const [savingBridgeConfig, setSavingBridgeConfig] = useState(false)
  const [pairingBusy, setPairingBusy] = useState(false)
  const [disconnecting, setDisconnecting] = useState(false)
  const [sendingTest, setSendingTest] = useState(false)
  const [qrDataUrl, setQrDataUrl] = useState('')
  const prevConnectedRef = useRef(false)

  const configReady = gatewayBaseUrl.trim().length > 0 && gatewayApiToken.trim().length > 0

  useEffect(() => {
    if (!banner) return
    const timer = window.setTimeout(() => setBanner(null), 4000)
    return () => window.clearTimeout(timer)
  }, [banner])

  useEffect(() => {
    if (!active) return
    const offSettings = window.cosmic?.onSettingsUpdate((settings) => {
      const nextBaseUrl = String(settings?.[SETTINGS_KEYS.baseUrl] ?? cosmicAuth?.gatewayUrl ?? '')
      const nextToken = String(settings?.[SETTINGS_KEYS.apiToken] ?? cosmicAuth?.gatewayApiToken ?? '')
      setGatewayBaseUrl(nextBaseUrl)
      setGatewayApiToken(nextToken)
      setConfigLoaded(true)
    })
    window.cosmic?.getSettings()
    return () => { offSettings?.() }
  }, [active, cosmicAuth])

  useEffect(() => {
    if (!active || !configLoaded || configReady || !cosmicAuth) return
    if (!gatewayBaseUrl.trim() && cosmicAuth.gatewayUrl) {
      setGatewayBaseUrl(cosmicAuth.gatewayUrl)
    }
    if (!gatewayApiToken.trim() && cosmicAuth.gatewayApiToken) {
      setGatewayApiToken(cosmicAuth.gatewayApiToken)
    }
  }, [active, configLoaded, configReady, cosmicAuth, gatewayApiToken, gatewayBaseUrl])

  useEffect(() => {
    let cancelled = false
    if (!status?.qr) {
      setQrDataUrl('')
      return () => { cancelled = true }
    }

    QRCode.toDataURL(status.qr, {
      width: 260,
      margin: 1,
      errorCorrectionLevel: 'M',
      color: { dark: '#111827', light: '#ffffff' },
    })
      .then((url) => {
        if (!cancelled) setQrDataUrl(url)
      })
      .catch(() => {
        if (!cancelled) setQrDataUrl('')
      })

    return () => { cancelled = true }
  }, [status?.qr])

  useEffect(() => {
    const nowConnected = !!status?.connected
    if (nowConnected && !prevConnectedRef.current) {
      setBanner({ tone: 'success', message: 'WhatsApp connected successfully!' })
    }
    prevConnectedRef.current = nowConnected
  }, [status?.connected])

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
          baseUrl: gatewayBaseUrl.trim(),
          apiToken: gatewayApiToken.trim(),
        })
        if (cancelled) return
        setStatus(nextStatus ?? null)
        if (!allowedPhoneDirty && nextStatus?.bridge_config) {
          setAllowedPhone(String(nextStatus.bridge_config.allowed_phone ?? ''))
        }
        setStatusError('')
      } catch (error: unknown) {
        if (cancelled) return
        setStatus(null)
        setQrDataUrl('')
        setStatusError(getErrorMessage(error, 'Unable to reach Gateway.'))
      } finally {
        if (!cancelled) setLoadingStatus(false)
      }
    }

    void refreshStatus(false)
    const poll = window.setInterval(() => void refreshStatus(true), 5000)
    return () => {
      cancelled = true
      window.clearInterval(poll)
    }
  }, [active, allowedPhoneDirty, configLoaded, configReady, gatewayApiToken, gatewayBaseUrl])

  const persistGatewayConfig = async (message?: string) => {
    const url = gatewayBaseUrl.trim()
    const token = gatewayApiToken.trim()
    if (!url || !token) throw new Error('Gateway URL and API token are required.')
    window.cosmic?.saveSetting(SETTINGS_KEYS.baseUrl, url)
    window.cosmic?.saveSetting(SETTINGS_KEYS.apiToken, token)
    if (message) setBanner({ tone: 'success', message })
  }

  const persistBridgeConfig = async (message?: string) => {
    if (!configReady) throw new Error('Save connection first.')
    setSavingBridgeConfig(true)
    try {
      const response = await window.cosmic?.saveWhatsAppConfig({
        baseUrl: gatewayBaseUrl.trim(),
        apiToken: gatewayApiToken.trim(),
        allowedPhone: allowedPhone.trim() || null,
      })
      const phone = String(response?.allowed_phone ?? '')
      setAllowedPhone(phone)
      setAllowedPhoneDirty(false)
      setStatus((current) => (current
        ? {
          ...current,
          bridge_config: {
            ...(current.bridge_config ?? {}),
            allowed_phone: phone || null,
          },
        }
        : current
      ))
      if (message) setBanner({ tone: 'success', message })
    } finally {
      setSavingBridgeConfig(false)
    }
  }

  const handleCheckStatus = async () => {
    try {
      await persistGatewayConfig()
      setLoadingStatus(true)
      const nextStatus = await window.cosmic?.getWhatsAppStatus({
        baseUrl: gatewayBaseUrl.trim(),
        apiToken: gatewayApiToken.trim(),
      })
      setStatus(nextStatus ?? null)
      if (!allowedPhoneDirty && nextStatus?.bridge_config) {
        setAllowedPhone(String(nextStatus.bridge_config.allowed_phone ?? ''))
      }
      setStatusError('')
      setBanner({ tone: 'success', message: 'Connection verified.' })
    } catch (error: unknown) {
      const message = getErrorMessage(error, 'Unable to reach Gateway.')
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
        baseUrl: gatewayBaseUrl.trim(),
        apiToken: gatewayApiToken.trim(),
        refresh: true,
        waitTimeoutMs: 20000,
      })
      setStatus(nextStatus ?? null)
      if (nextStatus?.bridge_config) {
        setAllowedPhone(String(nextStatus.bridge_config.allowed_phone ?? ''))
        setAllowedPhoneDirty(false)
      }
      setStatusError('')
      setBanner({
        tone: nextStatus?.qr ? 'info' : 'success',
        message: nextStatus?.qr ? 'Scan the QR code with WhatsApp.' : 'Pairing request sent.',
      })
    } catch (error: unknown) {
      const message = getErrorMessage(error, 'Failed to request QR.')
      const is408 = message.includes('408') || message.includes('timeout') || message.includes('Timeout')
      if (is408) {
        setBanner({ tone: 'info', message: 'Waiting for QR code... this may take a moment.' })
      } else {
        setBanner({ tone: 'error', message })
        setStatusError(message)
      }
    } finally {
      setPairingBusy(false)
    }
  }

  const handleClearSession = async () => {
    try {
      await persistGatewayConfig()
      setDisconnecting(true)
      const nextStatus = await window.cosmic?.clearWhatsAppSession({
        baseUrl: gatewayBaseUrl.trim(),
        apiToken: gatewayApiToken.trim(),
      })
      setStatus(nextStatus ?? null)
      setStatusError('')
      setQrDataUrl('')
      setBanner({ tone: 'success', message: 'WhatsApp session cleared.' })
    } catch (error: unknown) {
      const message = getErrorMessage(error, 'Failed to clear session.')
      setBanner({ tone: 'error', message })
      setStatusError(message)
    } finally {
      setDisconnecting(false)
    }
  }

  const handleSendTest = async () => {
    const phone = allowedPhone.trim()
    const baseUrl = gatewayBaseUrl.trim()
    const apiToken = gatewayApiToken.trim()
    if (!phone || !baseUrl || !apiToken || !isConnected) return
    setSendingTest(true)
    try {
      await window.cosmic?.sendWhatsAppTest({
        baseUrl,
        apiToken,
        number: phone,
        message: 'Hey! Cosmic here - If you got this, we\'re connected. Let\'s get something epic done.',
      })
      setStatusError('')
      setBanner({ tone: 'success', message: 'Test message sent!' })
    } catch (error: unknown) {
      const message = getErrorMessage(error, 'Failed to send test message.')
      setStatusError(message)
      setBanner({ tone: 'error', message })
    } finally {
      setSendingTest(false)
    }
  }

  if (!active) return null

  const isConnected = !!status?.connected
  const hasSession = isConnected || !!status?.has_auth_state
  const phoneSet = allowedPhone.trim()
  const heroActionLabel = pairingBusy
    ? 'Connecting...'
    : loadingStatus
      ? 'Checking...'
      : isConnected
        ? 'Refresh'
        : 'Connect'
  const heroStat = isConnected
    ? (phoneSet ? `Connected to ${phoneSet}` : 'Connected')
    : configReady
      ? 'Setup in progress'
      : 'Not configured'
  const heroDescription = authManaged
    ? 'Manage the bridge session and allowed phone number for your workspace.'
    : 'Save the gateway, set your number, and pair a device.'
  const visibleStatusError = statusError && statusError !== banner?.message ? statusError : ''
  const qrRefreshLabel = qrDataUrl ? formatQrTimestamp(status?.qr_updated_at) : ''

  const phoneBadgeClass = phoneSet ? 'status-connected' : 'status-needs_auth'
  const pairBadgeClass = qrDataUrl ? 'status-connected' : hasSession ? 'status-needs_auth' : 'status-needs_auth'
  const pairBadgeLabel = qrDataUrl ? 'QR ready' : hasSession ? 'Session saved' : 'Waiting'
  const pairCardNote = qrDataUrl
    ? qrRefreshLabel
    : hasSession
      ? 'A saved bridge session exists locally. Generate a new QR only if you need to re-pair.'
      : 'Use Connect above after saving the connection and phone number.'

  return (
    <div className="setting-subpage cosmic-google-page cosmic-wa-google-page">
      <div className="cosmic-google-hero cosmic-wa-google-hero">
        <div className="cosmic-google-hero-inner">
          <div className="cosmic-google-hero-icon cosmic-wa-google-hero-icon" aria-hidden="true">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.198.297-.767.966-.94 1.164-.174.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.174-.297-.019-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.174-.008-.372-.01-.571-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347z" fill="#25D366" />
              <path d="M20.52 3.449C18.24 1.245 15.24 0 12.045 0 5.463 0 .104 5.334.101 11.893c-.001 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.579 0 11.94-5.335 11.943-11.893.002-3.176-1.233-6.165-3.473-8.452zM12.045 21.785h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.981.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.002-5.45 4.437-9.884 9.889-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.885-9.886 9.885z" fill="#25D366" />
            </svg>
          </div>
          <div className="cosmic-google-hero-text">
            <h3>WhatsApp</h3>
            <p className="cosmic-google-hero-stat">
              {isConnected
                ? <><span className="cosmic-wa-hero-stat-connected">Connected</span>{phoneSet ? ` to ${phoneSet}` : ''}</>
                : heroStat}
            </p>
            <p className="cosmic-google-hero-desc">{heroDescription}</p>
          </div>
        </div>
        <div className="cosmic-wa-hero-actions">
          {hasSession && (
            <button
              type="button"
              className="cosmic-wa-hero-unlink"
              onClick={handleClearSession}
              disabled={disconnecting}
            >
              {disconnecting ? 'Unlinking...' : 'Unlink'}
            </button>
          )}
          <button
            type="button"
            className="cosmic-google-cta"
            onClick={isConnected ? handleCheckStatus : handleRequestQr}
            disabled={pairingBusy || loadingStatus || (!configReady && !isConnected)}
          >
            {heroActionLabel}
          </button>
        </div>
      </div>

      {banner && (
        <div className={`cosmic-google-banner cosmic-wa-google-banner ${banner.tone}`} role={banner.tone === 'error' ? 'alert' : 'status'}>
          <span>{banner.message}</span>
        </div>
      )}

      {visibleStatusError && (
        <div className="cosmic-google-banner cosmic-wa-google-banner error" role="alert">
          <span>{visibleStatusError}</span>
        </div>
      )}

      <div className="cosmic-google-accounts cosmic-wa-google-sections">
        <section className="cosmic-google-card cosmic-wa-google-card" style={{ animationDelay: '0.02s' }}>
          <div className="cosmic-google-card-header">
            <div className="cosmic-google-card-profile">
              <div className="cosmic-google-avatar cosmic-wa-google-avatar connection" aria-hidden="true">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
                  <path d="M6.62 10.79a15.053 15.053 0 006.59 6.59l2.2-2.2a1.003 1.003 0 011.01-.24c1.12.37 2.33.57 3.57.57.55 0 1 .45 1 1V20c0 .55-.45 1-1 1-9.39 0-17-7.61-17-17 0-.55.45-1 1-1h3.5c.55 0 1 .45 1 1 0 1.25.2 2.45.57 3.57.11.35.03.74-.25 1.02l-2.19 2.2z" fill="currentColor" />
                </svg>
              </div>
              <div className="cosmic-google-card-info">
                <div className="cosmic-wa-card-title-row">
                  <strong>WhatsApp number</strong>
                  <span className={`cosmic-google-badge ${phoneBadgeClass}`}>{phoneSet ? 'Saved' : 'Required'}</span>
                </div>
                <span>{isConnected
                  ? (phoneSet ? `Connected — messaging ${phoneSet}` : 'Connected — set a number below')
                  : 'Set the phone number Cosmic is allowed to message.'}</span>
              </div>
            </div>
          </div>

          <div className="cosmic-google-card-body">
            <div className="cosmic-wa-google-field-stack">
              <div className="cosmic-google-field">
                <label>Cosmic texts me at</label>
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
            </div>
            {status?.last_error && (
              <div className="cosmic-wa-google-info-list">
                <div className="cosmic-wa-google-info-row cosmic-wa-google-info-row-error">
                  <span>Last error</span>
                  <strong>{status.last_error}</strong>
                </div>
              </div>
            )}
          </div>

          <div className="cosmic-google-card-footer">
            <p className="cosmic-google-card-note">
              {phoneSet ? 'Cosmic will send WhatsApp messages to this number.' : 'Save your number before pairing so Cosmic knows where to text you.'}
            </p>
            <div className="cosmic-google-card-actions">
              <button
                type="button"
                className="cosmic-google-action secondary"
                onClick={() => void persistBridgeConfig('Phone number saved.')}
                disabled={savingBridgeConfig || !configReady}
              >
                {savingBridgeConfig ? 'Saving...' : 'Save number'}
              </button>
              {isConnected && phoneSet && (
                <button
                  type="button"
                  className={`cosmic-google-action primary ${sendingTest ? 'sending-animation' : ''}`}
                  onClick={handleSendTest}
                  disabled={sendingTest}
                >
                  {sendingTest ? 'Sending...' : 'Send test'}
                </button>
              )}
            </div>
          </div>
        </section>

        {!isConnected && configReady && (
          <section className="cosmic-google-card cosmic-wa-google-card" style={{ animationDelay: '0.1s' }}>
            <div className="cosmic-google-card-header">
              <div className="cosmic-google-card-profile">
                <div className="cosmic-google-avatar cosmic-wa-google-avatar pairing" aria-hidden="true">QR</div>
                <div className="cosmic-google-card-info">
                  <strong>Pair device</strong>
                  <span>Link WhatsApp from Linked devices on your phone.</span>
                </div>
              </div>
              <div className="cosmic-google-card-badges">
                <span className={`cosmic-google-badge ${pairBadgeClass}`}>{pairBadgeLabel}</span>
              </div>
            </div>

            <div className="cosmic-google-card-body">
              <div className="cosmic-wa-google-pairing">
                <div className={`cosmic-wa-google-qr-frame ${qrDataUrl ? 'live' : ''}`}>
                  {qrDataUrl ? (
                    <img src={qrDataUrl} alt="WhatsApp pairing QR" />
                  ) : (
                    <div className="cosmic-wa-google-qr-placeholder">
                      <span className="cosmic-wa-google-qr-placeholder-title">No QR yet</span>
                      <span className="cosmic-wa-google-qr-placeholder-sub">Use Connect above to request a QR code.</span>
                    </div>
                  )}
                </div>

                <div className="cosmic-wa-google-step-list">
                  <div className="cosmic-wa-google-step">
                    <span className="cosmic-wa-google-step-index" aria-hidden="true">1</span>
                    <div className="cosmic-wa-google-step-copy"><strong>Open WhatsApp on your phone.</strong></div>
                  </div>
                  <div className="cosmic-wa-google-step">
                    <span className="cosmic-wa-google-step-index" aria-hidden="true">2</span>
                    <div className="cosmic-wa-google-step-copy"><strong>Go to Linked devices.</strong></div>
                  </div>
                  <div className="cosmic-wa-google-step">
                    <span className="cosmic-wa-google-step-index" aria-hidden="true">3</span>
                    <div className="cosmic-wa-google-step-copy"><strong>Choose Link a device.</strong></div>
                  </div>
                  <div className="cosmic-wa-google-step">
                    <span className="cosmic-wa-google-step-index" aria-hidden="true">4</span>
                    <div className="cosmic-wa-google-step-copy">
                      <strong>{qrDataUrl ? 'Scan the QR code shown here.' : 'Scan the QR once it appears.'}</strong>
                      {qrRefreshLabel && <p>{qrRefreshLabel}</p>}
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <div className="cosmic-google-card-footer">
              <p className="cosmic-google-card-note">{pairCardNote}</p>
            </div>
          </section>
        )}
      </div>

      <div className="cosmic-wa-google-footer-space" aria-hidden="true" />
    </div>
  )
}

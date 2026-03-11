import { useEffect, useMemo, useState } from 'react'

interface TelegramIntegrationSettingsProps {
  active: boolean
  cosmicAuth?: { gatewayUrl: string; gatewayApiToken: string }
}

interface TelegramBotInfo {
  id?: number
  username?: string
  first_name?: string
}

interface TelegramWebhookInfo {
  url?: string
  pending_update_count?: number
  last_error_date?: number
  last_error_message?: string
  max_connections?: number
  ip_address?: string
  has_custom_certificate?: boolean
  allowed_updates?: string[]
}

interface TelegramBotStatusPayload {
  status?: string
  bot?: TelegramBotInfo
  webhook?: TelegramWebhookInfo
  webhook_url?: string
  allowed_user_id?: number | null
  allowed_chat_id?: number | null
}

interface TelegramChannelStatus {
  platform?: string
  configured?: boolean
  healthy?: boolean
  last_error?: string | null
  bot?: TelegramBotStatusPayload
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

export default function TelegramIntegrationSettings({ active, cosmicAuth }: TelegramIntegrationSettingsProps) {
  const authManaged = !!cosmicAuth
  const [gatewayBaseUrl, setGatewayBaseUrl] = useState('')
  const [gatewayApiToken, setGatewayApiToken] = useState('')
  const [configLoaded, setConfigLoaded] = useState(false)
  const [status, setStatus] = useState<TelegramChannelStatus | null>(null)
  const [statusError, setStatusError] = useState('')
  const [banner, setBanner] = useState<{ tone: BannerTone; message: string } | null>(null)
  const [loadingStatus, setLoadingStatus] = useState(false)
  const [syncingWebhook, setSyncingWebhook] = useState(false)
  const [clearingWebhook, setClearingWebhook] = useState(false)
  const [sendingTest, setSendingTest] = useState(false)

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
    if (!active || !configLoaded || !configReady) {
      if (!configReady) {
        setStatus(null)
        setStatusError('')
      }
      return
    }

    let cancelled = false
    const refreshStatus = async (quiet = true) => {
      if (!quiet) setLoadingStatus(true)
      try {
        const nextStatus = await window.cosmic?.getTelegramStatus({
          baseUrl: gatewayBaseUrl.trim(),
          apiToken: gatewayApiToken.trim(),
        })
        if (cancelled) return
        setStatus(nextStatus ?? null)
        setStatusError('')
      } catch (error: unknown) {
        if (cancelled) return
        setStatus(null)
        setStatusError(getErrorMessage(error, 'Unable to reach Gateway.'))
      } finally {
        if (!cancelled) setLoadingStatus(false)
      }
    }

    void refreshStatus(false)
    const poll = window.setInterval(() => void refreshStatus(true), 7000)
    return () => {
      cancelled = true
      window.clearInterval(poll)
    }
  }, [active, configLoaded, configReady, gatewayApiToken, gatewayBaseUrl])

  const refreshStatusOnce = async (successMessage?: string) => {
    try {
      setLoadingStatus(true)
      const nextStatus = await window.cosmic?.getTelegramStatus({
        baseUrl: gatewayBaseUrl.trim(),
        apiToken: gatewayApiToken.trim(),
      })
      setStatus(nextStatus ?? null)
      setStatusError('')
      if (successMessage) setBanner({ tone: 'success', message: successMessage })
    } catch (error: unknown) {
      const message = getErrorMessage(error, 'Unable to reach Gateway.')
      setStatus(null)
      setStatusError(message)
      setBanner({ tone: 'error', message })
    } finally {
      setLoadingStatus(false)
    }
  }

  const handleSyncWebhook = async () => {
    try {
      setSyncingWebhook(true)
      await window.cosmic?.syncTelegramWebhook({
        baseUrl: gatewayBaseUrl.trim(),
        apiToken: gatewayApiToken.trim(),
      })
      await refreshStatusOnce('Telegram webhook synced.')
    } catch (error: unknown) {
      const message = getErrorMessage(error, 'Failed to sync Telegram webhook.')
      setStatusError(message)
      setBanner({ tone: 'error', message })
    } finally {
      setSyncingWebhook(false)
    }
  }

  const handleClearWebhook = async () => {
    try {
      setClearingWebhook(true)
      await window.cosmic?.clearTelegramWebhook({
        baseUrl: gatewayBaseUrl.trim(),
        apiToken: gatewayApiToken.trim(),
        dropPendingUpdates: false,
      })
      await refreshStatusOnce('Telegram webhook removed.')
    } catch (error: unknown) {
      const message = getErrorMessage(error, 'Failed to delete Telegram webhook.')
      setStatusError(message)
      setBanner({ tone: 'error', message })
    } finally {
      setClearingWebhook(false)
    }
  }

  const handleSendTest = async () => {
    const chatId = status?.bot?.allowed_chat_id
    const botUsername = status?.bot?.bot?.username
    if (!chatId) return
    setSendingTest(true)
    try {
      await window.cosmic?.sendTelegramTest({
        baseUrl: gatewayBaseUrl.trim(),
        apiToken: gatewayApiToken.trim(),
        chatId,
        message: `Hey! Cosmic here from ${botUsername ? `@${botUsername}` : 'Telegram'} - this private DM path is live.`,
      })
      setStatusError('')
      setBanner({ tone: 'success', message: 'Test message sent!' })
    } catch (error: unknown) {
      const message = getErrorMessage(error, 'Failed to send Telegram test message.')
      setStatusError(message)
      setBanner({ tone: 'error', message })
    } finally {
      setSendingTest(false)
    }
  }

  if (!active) return null

  const botStatus = status?.bot ?? null
  const botInfo = botStatus?.bot ?? null
  const webhook = botStatus?.webhook ?? null
  const botUsername = botInfo?.username ? `@${botInfo.username}` : ''
  const botLink = botInfo?.username ? `https://t.me/${botInfo.username}` : ''
  const linkedChatId = botStatus?.allowed_chat_id
  const linkedUserId = botStatus?.allowed_user_id
  const webhookConfigured = !!(webhook?.url || botStatus?.webhook_url)
  const isHealthy = !!status?.healthy && botStatus?.status !== 'error'
  const heroLabel = botUsername || 'Telegram bot'
  const heroStat = isHealthy
    ? (linkedChatId ? 'Linked to private DM' : 'Bot live - waiting for /start')
    : webhookConfigured
      ? 'Webhook needs attention'
      : 'Webhook not configured'
  const heroDescription = authManaged
    ? 'Telegram runs as a per-VM bot. Use this panel to verify webhook health, linked chat identity, and test outbound delivery.'
    : 'Connect the Gateway first, then manage Telegram webhook health for this VM.'
  const visibleStatusError = statusError && statusError !== banner?.message ? statusError : ''

  const identityRows = [
    { label: 'Bot username', value: botUsername || 'Unavailable' },
    { label: 'Bot ID', value: botInfo?.id ? String(botInfo.id) : 'Unavailable' },
    { label: 'Allowed user ID', value: linkedUserId ? String(linkedUserId) : 'Waiting for /start' },
    { label: 'Allowed chat ID', value: linkedChatId ? String(linkedChatId) : 'Waiting for /start' },
  ]

  const activationNote = useMemo(() => {
    if (linkedChatId && linkedUserId) {
      return 'This VM is locked to the linked private Telegram chat. Bot token, webhook secret, and allowlist are managed on the VM.'
    }
    return 'Ask the user to send /start to the bot once. After the VM captures the Telegram user/chat IDs, keep those IDs locked in gateway.env.'
  }, [linkedChatId, linkedUserId])

  const quietStatusNote = isHealthy
    ? linkedChatId
      ? 'Telegram is connected and ready to use through your private chat.'
      : 'Telegram bot is live. Send /start once to finish linking your private chat.'
    : webhookConfigured
      ? 'Telegram is configured, but it needs attention before the bot can be used normally.'
      : 'Telegram webhook has not been synced yet for this VM.'

  return (
    <div className="setting-subpage cosmic-wa-google-page cosmic-tg-google-page">
      <div className="cosmic-google-hero cosmic-wa-google-hero">
        <div className="cosmic-google-hero-inner">
          <div className="cosmic-google-hero-icon cosmic-tg-google-hero-icon" aria-hidden="true">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M21.944 4.507c.315-.982-.278-1.506-1.22-1.167L3.54 10.016c-.953.37-.939.902-.173 1.137l4.422 1.38 10.233-6.456c.483-.294.925-.136.563.186l-8.292 7.483-.311 4.469c.456 0 .657-.208.912-.454l2.21-2.148 4.598 3.395c.847.468 1.457.227 1.669-.786l2.573-12.215z" fill="#229ED9"/>
            </svg>
          </div>
          <div className="cosmic-google-hero-text">
            <h3>Telegram</h3>
            <p className="cosmic-google-hero-stat">
              {isHealthy ? <span className="cosmic-tg-hero-stat-connected">{heroStat}</span> : heroStat}
            </p>
            <p className="cosmic-google-hero-desc">{heroDescription}</p>
          </div>
        </div>
        <div className="cosmic-wa-hero-actions">
          {webhookConfigured && (
            <button
              type="button"
              className="cosmic-wa-hero-unlink"
              onClick={handleClearWebhook}
              disabled={clearingWebhook || !configReady}
            >
              {clearingWebhook ? 'Deleting...' : 'Delete webhook'}
            </button>
          )}
          <button
            type="button"
            className="cosmic-google-cta"
            onClick={handleSyncWebhook}
            disabled={syncingWebhook || loadingStatus || !configReady}
          >
            {syncingWebhook ? 'Syncing...' : loadingStatus ? 'Refreshing...' : 'Sync webhook'}
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
              <div className="cosmic-google-avatar cosmic-tg-google-avatar bot" aria-hidden="true">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
                  <path d="M21.944 4.507c.315-.982-.278-1.506-1.22-1.167L3.54 10.016c-.953.37-.939.902-.173 1.137l4.422 1.38 10.233-6.456c.483-.294.925-.136.563.186l-8.292 7.483-.311 4.469c.456 0 .657-.208.912-.454l2.21-2.148 4.598 3.395c.847.468 1.457.227 1.669-.786l2.573-12.215z" fill="currentColor" />
                </svg>
              </div>
              <div className="cosmic-google-card-info">
                <div className="cosmic-wa-card-title-row">
                  <strong>{heroLabel}</strong>
                  <span className={`cosmic-google-badge ${isHealthy ? 'status-connected' : 'status-needs_auth'}`}>
                    {isHealthy ? 'Healthy' : 'Needs attention'}
                  </span>
                </div>
                <span>{linkedChatId ? 'Private DM linked and ready for Gateway routing.' : 'Send /start to the bot once so this VM can lock your private DM.'}</span>
              </div>
            </div>
          </div>

          <div className="cosmic-google-card-body">
            <div className="cosmic-wa-google-info-list">
              {identityRows.map((row) => (
                <div key={row.label} className="cosmic-wa-google-info-row">
                  <span>{row.label}</span>
                  <strong>{row.value}</strong>
                </div>
              ))}
              {status?.last_error && (
                <div className="cosmic-wa-google-info-row cosmic-wa-google-info-row-error">
                  <span>Gateway last error</span>
                  <strong>{status.last_error}</strong>
                </div>
              )}
            </div>
          </div>

          <div className="cosmic-google-card-footer">
            <p className="cosmic-google-card-note">{activationNote}</p>
            <div className="cosmic-google-card-actions">
              <button
                type="button"
                className="cosmic-google-action secondary"
                onClick={() => void refreshStatusOnce('Telegram status refreshed.')}
                disabled={loadingStatus || !configReady}
              >
                {loadingStatus ? 'Refreshing...' : 'Refresh status'}
              </button>
              {botLink && (
                <button
                  type="button"
                  className="cosmic-google-action ghost"
                  onClick={() => window.cosmic?.openExternal(botLink)}
                >
                  Open bot
                </button>
              )}
              <button
                type="button"
                className={`cosmic-google-action primary ${sendingTest ? 'sending-animation' : ''}`}
                onClick={handleSendTest}
                disabled={sendingTest || !linkedChatId || !configReady}
              >
                {sendingTest ? 'Sending...' : 'Send test'}
              </button>
            </div>
          </div>
        </section>
      </div>

      <div className="cosmic-wa-google-inline-note">
        {quietStatusNote}
      </div>

      <div className="cosmic-wa-google-footer-space" aria-hidden="true" />
    </div>
  )
}

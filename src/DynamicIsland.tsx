import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { ArrowLeft, BellRing, Power, RefreshCw, RotateCw, Video } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import './island.css'
import Settings, { type SettingsView } from './Settings'
import WeatherAnimation from './WeatherAnimation'
import DotBurstCheckmark from './DotBurstCheckmark'
import AgentWorkSlide, { type AgentWorkPayload } from './AgentWorkSlide'
import type { SearchPosition } from './App'
import CalendarMonthView from './CalendarMonthView'
import {
  AUTH_ATTENTION_REMINDER_INTERVAL_MS,
  AUTH_ATTENTION_SNOOZE_MS,
  getCodexAuthAttentionItems,
  getCursorAuthAttentionItems,
  getOpenCodeAuthAttentionItems,
  getZcodeAuthAttentionItems,
  getGoogleAuthAttentionItems,
  loadAuthAttentionPrefs,
  mergeAuthAttentionItems,
  pruneAuthAttentionPrefs,
  saveAuthAttentionPrefs,
  type AuthAttentionItem,
  type AuthAttentionPrefs,
  type AgentGatewayStatus,
} from './authAttention'
import type { IntegrationsSnapshot } from './integrations'
import {
  getWeatherAlertInfo,
  loadWeatherAlertLog,
  noteWeatherAlertObserved,
  recordWeatherAlertShown,
  saveWeatherAlertLog,
  shouldPeekWeatherAlert,
  type WeatherAlertCategory,
  type WeatherAlertLog,
} from './weatherAlerts'
import {
  EMPTY_CALENDAR_AGENDA,
  formatCalendarTime,
  getCalendarEventEnd,
  getCalendarEventStart,
  getCalendarRelativeLabel,
  normalizeCalendarAgendaSnapshot,
  type CalendarAgendaEvent,
  type CalendarAgendaSnapshot,
} from './calendar'

type CosmicMailIslandPayload =
  | {
      kind: 'single'
      mailboxId: string
      mailboxAddress: string
      threadId: string
      messageId: string
      subject: string
      fromName: string
      fromAddress: string
      snippet: string
      receivedAt: number
    }
  | {
      kind: 'batch'
      count: number
      mailboxId: string
      mailboxAddress: string
      subject: string
      fromSummary: string
      snippet: string
      latestReceivedAt: number
    }

type CosmicMailApprovalIslandPayload =
  | {
      kind: 'single'
      approvalId: string
      subject: string
      agentName: string
      mailboxAddress: string
      recipients: string
      snippet: string
      createdAt: number
    }
  | {
      kind: 'batch'
      count: number
      subject: string
      agentSummary: string
      snippet: string
      latestCreatedAt: number
      mailboxAddress: string
    }

/** Same footprint as default calendar slide (.island.expanded height). */
const ISLAND_NOTIFICATION_DIMENSIONS: CSSProperties = {
  width: '540px',
  height: '160px',
  borderRadius: '0 0 40px 40px',
}

function formatIslandInboundRelativeTime(receivedAtMs: number): string {
  const delta = Date.now() - receivedAtMs
  if (!Number.isFinite(delta) || delta < 0) return 'Now'
  const sec = Math.floor(delta / 1000)
  if (sec < 45) return 'Just now'
  const min = Math.floor(sec / 60)
  if (min < 60) return `${min}m ago`
  const hr = Math.floor(min / 60)
  if (hr < 24) return `${hr}h ago`
  const d = Math.floor(hr / 24)
  return `${d}d ago`
}

/** Matches Spaces inbox thread avatar initials (Agent Email). */
function diNotifyInitials(fromName: string, fromAddress: string): string {
  const n = String(fromName || '').trim()
  if (n) {
    const parts = n.split(/\s+/).filter(Boolean)
    if (parts.length >= 2) {
      const a = parts[0][0] || ''
      const b = parts[parts.length - 1][0] || ''
      return (a + b).toUpperCase()
    }
    return n.slice(0, 2).toUpperCase()
  }
  const local = String(fromAddress || '').split('@')[0] || ''
  return (local.slice(0, 2) || '?').toUpperCase()
}

function gatewayMailNotifyTime(value: unknown): number {
  const parsed = new Date(String(value || '')).getTime()
  return Number.isFinite(parsed) ? parsed : Date.now()
}

interface DynamicIslandProps {
  searchActive: boolean
  hovered: boolean
  searchPosition: SearchPosition
  onPositionChange: (pos: SearchPosition) => void
  staybackTime: number
  onStaybackChange: (time: number) => void
  islandOpacity: number
  onOpacityChange: (opacity: number) => void
  debug: boolean
  keyStatus: {
    haiku: boolean
    perplexity: boolean
    deepgram?: boolean
    groq?: boolean
    anthropic?: boolean
  }
  authData?: {
    fullName?: string
    gatewayUrl?: string
    gatewayApiToken?: string
    [key: string]: unknown
  }
  gatewayConnection?: {
    state: 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'error'
    connected: boolean
    detail?: string
  }
  onLogout?: () => void
  /** Chat surface width: panel (spotlight card) or wide. */
  chatWideMode?: boolean
  onChatWideModeChange?: (wide: boolean) => void
  /** Opens Spaces → Agent Email → Inbox; `mailboxId` selects that inbox when data is loaded. */
  onOpenAgentEmailInbox?: (mailboxId: string) => void
  /** Opens Spaces → Agent Email → Approvals; optional approval id to select when the list loads. */
  onOpenAgentEmailApprovals?: (approvalId?: string | null) => void
}

interface MediaState {
  title: string
  artist: string
  source: string
  appId?: string
  thumbnail: string | null
  isPlaying: boolean
  volume: number | null
  trackKey?: string
  position?: number
  duration?: number
  device?: string
}

interface WindowInfo {
  title: string
  process: string
  appName: string
}

interface WeatherState {
  temp: number
  condition: string
  isDay: boolean
  city: string
  wmo: number
  wind?: number
  humidity?: number
  high?: number
  low?: number
  precip_prob?: number
  snowfall?: number
}

type IntegrationToastTone = 'progress' | 'success' | 'error'

interface IntegrationToastState {
  id: number
  tone: IntegrationToastTone
  provider: string
  /** Stable provider id the Cancel routing and settings reopen key off. */
  providerId?: 'google' | 'github' | 'cosmic'
  title: string
  message: string
  statusLabel: string
  /** Only browser-waiting flows (Google, GitHub) can be cancelled mid-sign-in. */
  cancelable?: boolean
  /** Epoch ms the bridge stops waiting. Drives the countdown and the stale-toast guard. */
  expiresAt?: number
}

interface IntegrationToastEvent {
  provider?: string
  type?: string
  message?: string
  account_id?: string
  account_label?: string
  email?: string
  display_name?: string
  cancelable?: boolean
  timeout_seconds?: number
}

interface IslandNotificationDetail {
  type?: 'progress' | 'success' | 'error'
  title?: string
  message?: string
  provider?: string
}

interface AuthAttentionReminderState {
  item: AuthAttentionItem
  count: number
  keys: string[]
}

type AgentWorkSmokeWindow = Window & {
  __cosmicSmokeIsland?: (payload: AgentWorkPayload | { stop: true } | null) => void
}

const DOT_PROGRESS_COLUMNS = 28
const DOT_PROGRESS_ROWS = 3
const CALENDAR_REFRESH_MS = 5 * 60 * 1000
const CALENDAR_STALE_AFTER_MS = 2 * 60 * 1000
const AUTH_ATTENTION_REFRESH_MS = 5 * 60 * 1000
const AUTH_ATTENTION_AUTO_DISMISS_MS = 10 * 1000
// A snapshot fetched moments after a successful reconnect can still race it
// (a downstream health cache, the post-event snapshot refetch, etc.) and
// briefly report the account as needing reauth again. Suppress reauth
// attention items for a recently-reconnected account for this long so that
// race can never surface as a confusing "needs reauth" notification right
// after the user was just told the account connected successfully.
const RECENT_RECONNECT_GRACE_MS = 90 * 1000

// A Google sign-in runs in the system browser, which never reports back that the
// user closed the tab. The island therefore refuses to show an open-ended wait:
// the progress panel carries the bridge's own deadline, a working Cancel, and a
// last-resort self-dismiss for the case where the bridge itself stops answering.
const INTEGRATION_PROGRESS_STALE_GRACE_MS = 8 * 1000
const INTEGRATION_CANCEL_FALLBACK_MS = 4 * 1000
/** Long enough for the success burst to land before the panel slides back in. */
const SETTINGS_REOPEN_AFTER_AUTH_MS = 1200
/** A settings close this soon after a hand-off is the hand-off, not the user. */
const SETTINGS_AUTH_HANDOFF_CLOSE_WINDOW_MS = 1500

/** Severe + advisory island alerts; used by weather slide and auto-peek scheduling. */
const WEATHER_ALERT_PEEK_MS = 5000
const WEATHER_SLIDE_INDEX = 2
/** After auto-peek ends, ignore parent/inner hover briefly so the island can collapse (cursor often sits on the expanded hit area). */
const WEATHER_PEEK_HOVER_SUPPRESS_MS = 450
/** Ignore hover-based peek cancel until this long after peek starts (avoids killing the timer when the island expands under the cursor). */
const WEATHER_PEEK_USER_CANCEL_ARM_MS = 420

function getEventDurationLabel(event: CalendarAgendaEvent) {
  if (event.isAllDay) return 'All day'
  const start = getCalendarEventStart(event)
  const end = getCalendarEventEnd(event)
  if (!start || !end) return ''
  const diffMinutes = Math.max(0, Math.round((end.getTime() - start.getTime()) / 60000))
  if (diffMinutes < 60) return `${diffMinutes} min`
  const hours = Math.floor(diffMinutes / 60)
  const minutes = diffMinutes % 60
  return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`
}

function getIntegrationAccountName(event: IntegrationToastEvent | IslandNotificationDetail) {
  if ('account_label' in event && event.account_label) return String(event.account_label).trim()
  if ('display_name' in event && event.display_name) return String(event.display_name).trim()
  if ('email' in event && event.email) return String(event.email).trim()
  return ''
}

function formatCountdown(msRemaining: number) {
  const totalSeconds = Math.max(0, Math.ceil(msRemaining / 1000))
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes}:${String(seconds).padStart(2, '0')}`
}

function compactToastMessage(message: string, fallback: string) {
  const normalized = String(message || '')
    .replace(/\s+/g, ' ')
    .trim()
  if (!normalized) return fallback
  return normalized.length > 108 ? `${normalized.slice(0, 105).trimEnd()}...` : normalized
}

function getSourceColor(source: string): string {
  const s = source.toLowerCase()
  if (s.includes('spotify')) return '#1DB954'
  if (s.includes('music') || s.includes('apple')) return '#FA243C'
  if (s.includes('youtube')) return '#FF0000'
  if (s.includes('chrome')) return '#4285F4'
  if (s.includes('edge')) return '#0078D7'
  return '#007AFF'
}

function SourceIcon({ source, color }: { source: string, color: string }) {
  const s = source.toLowerCase()
  if (s.includes('spotify')) {
    return (
      <svg width="100%" height="100%" viewBox="0 0 24 24" fill="currentColor" style={{ color }}>
        <path d="M12 2a10 10 0 1 0 .001 20.001A10 10 0 0 0 12 2zm4.6 14.5a.9.9 0 0 1-1.24.3c-2.9-1.77-6.55-2.17-10.86-1.2a.9.9 0 1 1-.4-1.76c4.76-1.07 8.86-.6 12.19 1.44.42.26.55.82.31 1.22z" />
      </svg>
    )
  }
  if (s.includes('youtube')) {
    return (
      <svg width="100%" height="100%" viewBox="0 0 24 24" fill="currentColor" style={{ color }}>
        <path d="M23.5 6.2c-.3-1-1-1.8-2-2C19.8 3.7 12 3.7 12 3.7s-7.8 0-9.5.5c-1 .3-1.8 1-2 2C0 8 0 12 0 12s0 4 .5 5.8c.3 1 1 1.8 2 2 1.7.5 9.5.5 9.5.5s7.8 0 9.5-.5c1-.3 1.8-1 2-2 .5-1.8.5-5.8.5-5.8s0-4-.5-5.8zM9.6 15.6V8.4l6.4 3.6-6.4 3.6z" />
      </svg>
    )
  }
  return (
    <svg width="100%" height="100%" viewBox="0 0 24 24" fill="currentColor" style={{ color }}>
      <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z" />
    </svg>
  )
}

function SmartDeviceIcon({ deviceName }: { deviceName: string }) {
  const name = (deviceName || "").toLowerCase()

  if (name.includes('headphone') || name.includes('airpod') || name.includes('buds') || name.includes('headset')) {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
        <path d="M12 3a9 9 0 0 0-9 9v7c0 1.1.9 2 2 2h4v-8H5v-1c0-3.87 3.13-7 7-7s7 3.13 7 7v1h-4v8h4c1.1 0 2-.9 2-2v-7a9 9 0 0 0-9-9z" />
      </svg>
    )
  }

  if (name.includes('monitor') || name.includes('tv') || name.includes('display')) {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
        <path d="M21 3H3c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h5v2h8v-2h5c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 14H3V5h18v12z" />
      </svg>
    )
  }

  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
      <path d="M7 9v6h4l5 5V4L11 9H7z" />
    </svg>
  )
}

function toDataUrlMaybe(raw: string | null | undefined): string | null {
  if (!raw) return null
  const s0 = String(raw).trim()
  if (!s0) return null
  if (s0.startsWith('data:')) return s0
  if (s0.startsWith('http')) return s0
  if (s0.includes('base64,')) return s0
  return null
}

function dataUrlToBlobUrl(dataUrl: string): string | null {
  try {
    const m = dataUrl.match(/^data:([^;]+);base64,(.+)$/)
    if (!m) return null
    const mime = m[1]
    const b64 = m[2].replace(/\s/g, '')
    const bin = atob(b64)
    const bytes = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
    const blob = new Blob([bytes], { type: mime })
    return URL.createObjectURL(blob)
  } catch {
    return null
  }
}



export default function DynamicIsland({
  searchActive,
  hovered,
  searchPosition,
  onPositionChange,
  staybackTime,
  onStaybackChange,
  islandOpacity,
  onOpacityChange,
  chatWideMode,
  onChatWideModeChange,
  keyStatus,
  authData,
  gatewayConnection,
  onLogout,
  onOpenAgentEmailInbox,
  onOpenAgentEmailApprovals,
}: DynamicIslandProps) {
  const [activeSlide, setActiveSlide] = useState(0)
  const TOTAL_SLIDES = 6

  const [showVolume, setShowVolume] = useState(false)
  const [internalHover, setInternalHover] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [settingsInitialView, setSettingsInitialView] = useState<SettingsView>('main')
  // A connect/reauth button hands the user to the browser, so the panel steps
  // aside for it and comes back once the flow has an outcome.
  const showSettingsRef = useRef(showSettings)
  showSettingsRef.current = showSettings
  const reopenSettingsAfterAuthRef = useRef(false)
  const reopenSettingsTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const authHandoffAtRef = useRef(0)
  const [isAnchored, setIsAnchored] = useState(false)

  // Voice State
  const [voiceActive, setVoiceActive] = useState(false)
  const [voiceStatus, setVoiceStatus] = useState<string>('ready')
  const [voiceTranscript, setVoiceTranscript] = useState('')
  const [voiceError, setVoiceError] = useState<string | null>(null)
  const [voiceHistory, setVoiceHistory] = useState<string[]>([])
  const [lastFinalTranscript, setLastFinalTranscript] = useState('')
  const [voiceShortcutEnabled, setVoiceShortcutEnabled] = useState(true)

  const voiceShortcutLabel = useMemo(() => {
    const isMac = /Mac|iPhone|iPod|iPad/i.test(navigator.platform)
    return isMac ? '⌘⇧V' : 'Ctrl+Shift+V'
  }, [])

  // Agent-at-work slide (orchestrator's current tool/specialist).
  // Driven by `cosmic:island-agent-work` CustomEvent — `null` clears the slide.
  const [agentWorkPayload, setAgentWorkPayload] = useState<AgentWorkPayload | null>(null)

  // New State for Notification
  const [notificationEvent, setNotificationEvent] = useState<CalendarAgendaEvent | null>(null)
  const [mailInboundNotification, setMailInboundNotification] = useState<CosmicMailIslandPayload | null>(null)
  const [approvalRequestNotification, setApprovalRequestNotification] =
    useState<CosmicMailApprovalIslandPayload | null>(null)
  // Track notified events to prevent double notification
  const notifiedEventsRef = useRef<Set<string>>(new Set())
  const hoverGateRef = useRef({ searchActive, hovered, internalHover })
  hoverGateRef.current = { searchActive, hovered, internalHover }

  // Temporary Google integration toast (connect / disconnect / remove) — show in island then auto-dismiss
  const [integrationToast, setIntegrationToast] = useState<IntegrationToastState | null>(null)
  // Accounts that just completed a successful reconnect - see
  // RECENT_RECONNECT_GRACE_MS above.
  const recentlyReconnectedRef = useRef<Map<string, number>>(new Map())
  const [authAttentionItems, setAuthAttentionItems] = useState<AuthAttentionItem[]>([])
  const [authAttentionPrefs, setAuthAttentionPrefs] = useState<AuthAttentionPrefs>(() => loadAuthAttentionPrefs())
  const [authAttentionReminder, setAuthAttentionReminder] = useState<AuthAttentionReminderState | null>(null)
  const integrationToastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const authAttentionTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const integrationToastIdRef = useRef(0)
  // Cancel routing needs the in-flight provider without rebuilding the callback
  // on every toast change.
  const integrationToastProviderIdRef = useRef<'google' | 'github' | 'cosmic'>('google')
  integrationToastProviderIdRef.current = integrationToast?.providerId ?? 'cosmic'
  const integrationDotsTransitionTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const burstPlayedRef = useRef(false)
  const previousIntegrationToastToneRef = useRef<IntegrationToastTone | null>(null)
  const integrationToastId = integrationToast?.id ?? null
  const integrationToastTone = integrationToast?.tone ?? null
  const integrationToastExpiresAt = integrationToast?.expiresAt ?? null
  const [integrationDotsTransitionToastId, setIntegrationDotsTransitionToastId] = useState<number | null>(null)
  const integrationCancelFallbackTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [integrationCountdownNow, setIntegrationCountdownNow] = useState(() => Date.now())
  const [calendarData, setCalendarData] = useState<CalendarAgendaSnapshot>(EMPTY_CALENDAR_AGENDA)
  const [calendarRefreshing, setCalendarRefreshing] = useState(false)
  const [showMonthView, setShowMonthView] = useState(false)
  const [selectedCalendarEvent, setSelectedCalendarEvent] = useState<CalendarAgendaEvent | null>(null)
  const calendarRefreshTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const calendarGeneratedAtRef = useRef(0)

  const requestCalendarAgenda = useCallback((showSpinner = false) => {
    if (!window.cosmic?.getCalendarAgenda) return
    if (showSpinner) {
      setCalendarRefreshing(true)
    }
    if (calendarRefreshTimeoutRef.current) {
      clearTimeout(calendarRefreshTimeoutRef.current)
    }
    calendarRefreshTimeoutRef.current = setTimeout(() => {
      calendarRefreshTimeoutRef.current = null
      setCalendarRefreshing(false)
    }, 8000)
    window.cosmic.getCalendarAgenda()
  }, [])

  const requestIntegrationsSnapshot = useCallback(() => {
    window.cosmic?.getIntegrations?.()
  }, [])

  const [integrationsSnapshot, setIntegrationsSnapshot] = useState<IntegrationsSnapshot | null>(null)
  const integrationsSnapshotRef = useRef<IntegrationsSnapshot | null>(null)
  integrationsSnapshotRef.current = integrationsSnapshot

  const rebuildAuthAttentionItems = useCallback(async (snapshot?: IntegrationsSnapshot | null) => {
    const now = Date.now()
    const recent = recentlyReconnectedRef.current
    for (const [accountId, reconnectedAt] of recent) {
      if (now - reconnectedAt > RECENT_RECONNECT_GRACE_MS) recent.delete(accountId)
    }
    const googleItems = getGoogleAuthAttentionItems(snapshot).filter((item) => !recent.has(item.accountId))
    let codexItems: AuthAttentionItem[] = []
    let cursorItems: AuthAttentionItem[] = []
    let opencodeItems: AuthAttentionItem[] = []
    let zcodeItems: AuthAttentionItem[] = []
    try {
      const [codexStatus, cursorStatus, opencodeStatus, zcodeStatus] = await Promise.all([
        window.cosmic?.getGatewayCodexStatus?.(),
        window.cosmic?.getGatewayCursorStatus?.(),
        window.cosmic?.getGatewayOpenCodeStatus?.(),
        window.cosmic?.getGatewayZcodeStatus?.(),
      ])
      codexItems = getCodexAuthAttentionItems(codexStatus as AgentGatewayStatus | null | undefined)
      cursorItems = getCursorAuthAttentionItems(cursorStatus as AgentGatewayStatus | null | undefined)
      opencodeItems = getOpenCodeAuthAttentionItems(opencodeStatus as AgentGatewayStatus | null | undefined)
      zcodeItems = getZcodeAuthAttentionItems(zcodeStatus as AgentGatewayStatus | null | undefined)
    } catch {
      // Keep Google-only attention items when the VM is offline.
    }
    setAuthAttentionItems(mergeAuthAttentionItems(googleItems, opencodeItems, zcodeItems, codexItems, cursorItems))
  }, [])

  const openCalendarEventDetail = useCallback((event: CalendarAgendaEvent) => {
    setSelectedCalendarEvent(event)
    setExpanded(true)
  }, [])


  const [weatherAlertPeek, setWeatherAlertPeek] = useState(false)
  const [suppressIslandHoverExpand, setSuppressIslandHoverExpand] = useState(false)
  const weatherAlertPeekRef = useRef(false)
  const weatherAlertPeekTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const peekHoverSuppressClearTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const prevHadWeatherAlertRef = useRef(false)
  /** Category of the in-flight auto-peek, so every teardown path can record it. */
  const peekActiveCategoryRef = useRef<WeatherAlertCategory | null>(null)
  const peekUserCancelArmTimestampRef = useRef(0)
  const prevHoveredDuringPeekRef = useRef(false)
  // Survives remounts and restarts, so a reload or a quick relaunch cannot
  // re-announce a standing advisory. A gap longer than EPISODE_GAP_MS is treated
  // as a genuinely new episode and does announce again.
  const weatherAlertLogRef = useRef<WeatherAlertLog | null>(null)
  if (weatherAlertLogRef.current === null) {
    weatherAlertLogRef.current = loadWeatherAlertLog()
  }

  weatherAlertPeekRef.current = weatherAlertPeek

  /**
   * Closes out the in-flight peek. `completed` false means it was cut short and
   * the user never really saw it, which earns a much shorter retry.
   */
  const recordPeekEnded = useCallback((completed: boolean) => {
    const category = peekActiveCategoryRef.current
    if (!category) return
    peekActiveCategoryRef.current = null
    const next = recordWeatherAlertShown(weatherAlertLogRef.current ?? {}, category, completed, Date.now())
    weatherAlertLogRef.current = next
    saveWeatherAlertLog(next)
  }, [])

  const clearWeatherAlertPeekTimer = useCallback(() => {
    if (weatherAlertPeekTimerRef.current) {
      clearTimeout(weatherAlertPeekTimerRef.current)
      weatherAlertPeekTimerRef.current = null
    }
  }, [])

  const clearPeekHoverSuppressTimer = useCallback(() => {
    if (peekHoverSuppressClearTimerRef.current) {
      clearTimeout(peekHoverSuppressClearTimerRef.current)
      peekHoverSuppressClearTimerRef.current = null
    }
  }, [])

  const schedulePeekHoverSuppressRelease = useCallback(() => {
    clearPeekHoverSuppressTimer()
    peekHoverSuppressClearTimerRef.current = setTimeout(() => {
      peekHoverSuppressClearTimerRef.current = null
      setSuppressIslandHoverExpand(false)
    }, WEATHER_PEEK_HOVER_SUPPRESS_MS)
  }, [clearPeekHoverSuppressTimer])

  /** User engaged the island during auto-peek: stop the timer; stayback applies when they leave (no hover suppress). */
  const cancelWeatherPeekForUserHover = useCallback(() => {
    if (!weatherAlertPeekRef.current) return
    clearWeatherAlertPeekTimer()
    weatherAlertPeekRef.current = false
    setWeatherAlertPeek(false)
    // The user engaged with the island while it was up: that counts as seen.
    recordPeekEnded(true)
    peekUserCancelArmTimestampRef.current = Number.MAX_SAFE_INTEGER
  }, [clearWeatherAlertPeekTimer, recordPeekEnded])

  const weatherAlertPeekBlocked = useMemo(
    () =>
      searchActive ||
      showSettings ||
      voiceActive ||
      !!notificationEvent ||
      !!mailInboundNotification ||
      !!approvalRequestNotification ||
      !!integrationToast ||
      !!authAttentionReminder ||
      !!selectedCalendarEvent ||
      showMonthView,
    [
      searchActive,
      showSettings,
      voiceActive,
      notificationEvent,
      mailInboundNotification,
      approvalRequestNotification,
      integrationToast,
      authAttentionReminder,
      selectedCalendarEvent,
      showMonthView,
    ],
  )

  // Full carousel / notifications / settings — never driven by Cosmic overlay
  // being open. Overlay-open uses the thin session island instead.
  const needsFullIsland =
    (!suppressIslandHoverExpand && (hovered || internalHover)) ||
    showSettings ||
    isAnchored ||
    !!notificationEvent ||
    !!mailInboundNotification ||
    !!approvalRequestNotification ||
    !!integrationToast ||
    !!authAttentionReminder ||
    !!selectedCalendarEvent ||
    showMonthView ||
    voiceActive ||
    weatherAlertPeek ||
    (!!agentWorkPayload && !searchActive)
  const sessionIslandActive = searchActive && !needsFullIsland
  const shouldExpand = needsFullIsland || searchActive
  const [expanded, setExpanded] = useState(shouldExpand)
  const fullIslandStaybackRef = useRef(false)

  useEffect(() => {
    let timer: NodeJS.Timeout
    if (shouldExpand) {
      setExpanded(true)
      fullIslandStaybackRef.current = needsFullIsland
    } else {
      // Session-only dismiss (hide Cosmic) collapses immediately so the 160px
      // home slide does not flash. Stayback still applies after a real
      // full-island interaction (hover, pin, notification, …).
      const delayMs = fullIslandStaybackRef.current ? staybackTime * 1000 : 0
      fullIslandStaybackRef.current = false
      if (delayMs > 0) {
        timer = setTimeout(() => { setExpanded(false) }, delayMs)
      } else {
        setExpanded(false)
      }
    }
    return () => clearTimeout(timer)
  }, [shouldExpand, needsFullIsland, staybackTime])

  const wasExpanded = useRef(expanded)
  useEffect(() => {
    // If we just collapsed (expanded went from true -> false)
    if (!expanded && wasExpanded.current) {
      setActiveSlide(0)
      setShowMonthView(false)
      setSelectedCalendarEvent(null)
      setNotificationEvent(null)
      setMailInboundNotification(null)
      setApprovalRequestNotification(null)
      setIntegrationToast(null)
      setAuthAttentionReminder(null)
      if (authAttentionTimerRef.current) {
        clearTimeout(authAttentionTimerRef.current)
        authAttentionTimerRef.current = null
      }
      if (integrationToastTimerRef.current) {
        clearTimeout(integrationToastTimerRef.current)
        integrationToastTimerRef.current = null
      }
      clearWeatherAlertPeekTimer()
      setWeatherAlertPeek(false)
      weatherAlertPeekRef.current = false
      recordPeekEnded(false)
      peekUserCancelArmTimestampRef.current = Number.MAX_SAFE_INTEGER
      clearPeekHoverSuppressTimer()
      setSuppressIslandHoverExpand(false)
    }
    wasExpanded.current = expanded
  }, [expanded, clearWeatherAlertPeekTimer, clearPeekHoverSuppressTimer, recordPeekEnded])

  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(id)
  }, [])

  const draggingRef = useRef(false)

  useEffect(() => {
    const handleUp = () => {
      if (draggingRef.current) {
        draggingRef.current = false;
        setTimeout(() => setShowVolume(false), 1000);
      }
    };
    window.addEventListener('pointerup', handleUp);
    window.addEventListener('pointercancel', handleUp);
    return () => {
      window.removeEventListener('pointerup', handleUp);
      window.removeEventListener('pointercancel', handleUp);
    };
  }, []);
  const [localVolume, setLocalVolume] = useState(0)

  const [media, setMedia] = useState<MediaState>({
    title: 'Not Playing',
    artist: 'System Audio',
    source: 'System',
    appId: 'System',
    thumbnail: null,
    isPlaying: false,
    volume: null,
    trackKey: 'System::Not Playing::System Audio',
    position: 0,
    duration: 0,
    device: 'System'
  })

  const [windowInfo, setWindowInfo] = useState<WindowInfo>({
    title: 'Desktop',
    process: 'explorer.exe',
    appName: 'Windows'
  })

  const [weather, setWeather] = useState<WeatherState | null>(null)

  const [isMusicActive, setIsMusicActive] = useState(false)

  useEffect(() => {
    let timer: NodeJS.Timeout

    if (media.isPlaying) {
      setIsMusicActive(true)
    } else {
      const waitTime = media.title === 'Not Playing' ? 2000 : 60000
      timer = setTimeout(() => {
        setIsMusicActive(false)
      }, waitTime)
    }

    return () => clearTimeout(timer)
  }, [media.isPlaying, media.title])

  const prevIsMusicActive = useRef(isMusicActive)

  useEffect(() => {
    if (!prevIsMusicActive.current && isMusicActive) {
      if (activeSlide === 1) {
        setActiveSlide(0)
      }
    }
    prevIsMusicActive.current = isMusicActive
  }, [isMusicActive, activeSlide])

  const slideContentMap = useMemo(() => {
    if (notificationEvent || mailInboundNotification || approvalRequestNotification) return ['notification'] as const
    if (isMusicActive) return ['music', 'home', 'weather', 'calendar', 'voice', 'utilities'] as const
    return ['home', 'music', 'weather', 'calendar', 'voice', 'utilities'] as const
  }, [isMusicActive, notificationEvent, mailInboundNotification, approvalRequestNotification])

  useEffect(() => {
    if (!window.cosmic?.onCosmicMailInbound) return
    const unsub = window.cosmic.onCosmicMailInbound((payload: CosmicMailIslandPayload) => {
      if (!payload || (payload.kind !== 'single' && payload.kind !== 'batch')) return
      setMailInboundNotification(payload)
      setExpanded(true)
    })
    return () => unsub?.()
  }, [])

  useEffect(() => {
    const listener = (raw: Event) => {
      const detail = ((raw as CustomEvent<Record<string, unknown>>).detail || {}) as Record<string, unknown>
      const kind = String(detail.kind || '').trim()
      if (kind === 'inbound') {
        setMailInboundNotification({
          kind: 'single',
          mailboxId: String(detail.mailbox_id || ''),
          mailboxAddress: String(detail.mailbox_address || 'Inbox'),
          threadId: String(detail.thread_id || ''),
          messageId: String(detail.message_id || ''),
          subject: String(detail.subject || 'New email received'),
          fromName: String(detail.from_name || ''),
          fromAddress: String(detail.from_address || ''),
          snippet: String(detail.snippet || 'New message'),
          receivedAt: gatewayMailNotifyTime(detail.received_at || detail.created_at || detail.timestamp),
        })
        setExpanded(true)
        return
      }
      if (kind === 'approval') {
        setApprovalRequestNotification({
          kind: 'single',
          approvalId: String(detail.approval_id || ''),
          subject: String(detail.subject || 'Email approval required'),
          agentName: String(detail.agent_name || 'Cosmic'),
          mailboxAddress: String(detail.mailbox_address || 'Agent Email'),
          recipients: String(detail.recipient_summary || ''),
          snippet: String(detail.snippet || 'Awaiting your review'),
          createdAt: gatewayMailNotifyTime(detail.created_at || detail.timestamp),
        })
        setExpanded(true)
      }
    }
    window.addEventListener('cosmic-mail:gateway-notification', listener)
    return () => window.removeEventListener('cosmic-mail:gateway-notification', listener)
  }, [])

  useEffect(() => {
    if (!mailInboundNotification) return
    const t = setTimeout(() => {
      setMailInboundNotification(null)
      const g = hoverGateRef.current
      if (!g.searchActive && !g.hovered && !g.internalHover) {
        setExpanded(false)
      }
    }, 10_000)
    return () => clearTimeout(t)
  }, [mailInboundNotification])

  useEffect(() => {
    if (!window.cosmic?.onCosmicMailApproval) return
    const unsub = window.cosmic.onCosmicMailApproval((payload: CosmicMailApprovalIslandPayload) => {
      if (!payload || (payload.kind !== 'single' && payload.kind !== 'batch')) return
      setApprovalRequestNotification(payload)
      setExpanded(true)
    })
    return () => unsub?.()
  }, [])

  useEffect(() => {
    if (!approvalRequestNotification) return
    const t = setTimeout(() => {
      setApprovalRequestNotification(null)
      const g = hoverGateRef.current
      if (!g.searchActive && !g.hovered && !g.internalHover) {
        setExpanded(false)
      }
    }, 10_000)
    return () => clearTimeout(t)
  }, [approvalRequestNotification])

  useEffect(() => {
    if (!window.cosmic?.onWindowUpdate) return
    const unsub = window.cosmic.onWindowUpdate((data: WindowInfo) => setWindowInfo(data))
    return () => unsub?.()
  }, [requestCalendarAgenda])

  useEffect(() => {
    if (!window.cosmic?.onMediaUpdate) return
    const unsub = window.cosmic.onMediaUpdate((data: Partial<MediaState>) => {
      setMedia((prev) => {
        const next: MediaState = { ...prev, ...data }
        if (!draggingRef.current && typeof data.volume === 'number' && Number.isFinite(data.volume)) {
          setLocalVolume(Math.max(0, Math.min(100, Math.round(data.volume))))
        }
        return next
      })
    })
    return () => unsub?.()
  }, [])

  useEffect(() => {
    if (!window.cosmic?.onWeatherUpdate) return
    const unsub = window.cosmic.onWeatherUpdate((data: WeatherState) => {
      setWeather(data)
    })
    window.cosmic?.requestWeather()
    return () => unsub?.()
  }, [])

  useEffect(() => () => {
    clearWeatherAlertPeekTimer()
    clearPeekHoverSuppressTimer()
  }, [clearWeatherAlertPeekTimer, clearPeekHoverSuppressTimer])

  /** Higher-priority island UI: cancel peek without marking the alert as "shown". */
  useEffect(() => {
    if (!weatherAlertPeek || !weatherAlertPeekBlocked) return
    clearWeatherAlertPeekTimer()
    weatherAlertPeekRef.current = false
    setWeatherAlertPeek(false)
    // Cut short by higher-priority island UI. This used to return without
    // recording anything, leaving the alert eligible to fire again on the very
    // next render — which, with `hovered` in the deps below, meant the next
    // mouse move.
    recordPeekEnded(false)
    setInternalHover(false)
    setSuppressIslandHoverExpand(true)
    schedulePeekHoverSuppressRelease()
  }, [
    weatherAlertPeek,
    weatherAlertPeekBlocked,
    clearWeatherAlertPeekTimer,
    schedulePeekHoverSuppressRelease,
    recordPeekEnded,
  ])

  /**
   * Severe or advisory weather → expand island on the weather slide for WEATHER_ALERT_PEEK_MS.
   * Waits while mail / approvals / calendar notify / Google integration / settings / voice / search / month view / event detail are active.
   *
   * Repeats are governed by the persisted ledger in `weatherAlerts.ts`, keyed on
   * the condition category rather than the display string, so the same standing
   * condition does not re-announce itself every time its wording shifts.
   * Deliberately not keyed on `hovered`: this used to re-run on every cursor
   * move over the island, which turned any missed bookkeeping into a peek storm.
   */
  useEffect(() => {
    if (!weather) {
      prevHadWeatherAlertRef.current = false
      return
    }

    const { tier, category } = getWeatherAlertInfo({
      wmo: weather.wmo,
      temp: weather.temp,
      high: weather.high,
    })

    const hadAlert = prevHadWeatherAlertRef.current
    if (hadAlert && !category && weatherAlertPeekRef.current) {
      // The condition ended mid-peek. Nothing to re-announce until it returns,
      // and the ledger keeps its cooldown so a momentary dip below a threshold
      // cannot re-arm the alert.
      clearWeatherAlertPeekTimer()
      weatherAlertPeekRef.current = false
      setWeatherAlertPeek(false)
      recordPeekEnded(false)
      setInternalHover(false)
      setSuppressIslandHoverExpand(true)
      schedulePeekHoverSuppressRelease()
    }
    prevHadWeatherAlertRef.current = !!category

    if (!category || !tier) return

    const now = Date.now()
    const observedLog = noteWeatherAlertObserved(weatherAlertLogRef.current ?? {}, category, now)
    if (observedLog !== weatherAlertLogRef.current) {
      weatherAlertLogRef.current = observedLog
      saveWeatherAlertLog(observedLog)
    }

    if (!shouldPeekWeatherAlert(observedLog, category, tier, now)) return

    if (weatherAlertPeekBlocked) return

    if (weatherAlertPeekRef.current) return

    clearWeatherAlertPeekTimer()
    clearPeekHoverSuppressTimer()
    setSuppressIslandHoverExpand(false)
    peekActiveCategoryRef.current = category
    peekUserCancelArmTimestampRef.current = now + WEATHER_PEEK_USER_CANCEL_ARM_MS
    prevHoveredDuringPeekRef.current = hoverGateRef.current.hovered
    weatherAlertPeekRef.current = true
    setWeatherAlertPeek(true)
    setActiveSlide(WEATHER_SLIDE_INDEX)

    weatherAlertPeekTimerRef.current = setTimeout(() => {
      weatherAlertPeekTimerRef.current = null
      weatherAlertPeekRef.current = false
      setWeatherAlertPeek(false)
      recordPeekEnded(true)
      setInternalHover(false)
      setSuppressIslandHoverExpand(true)
      schedulePeekHoverSuppressRelease()
      peekUserCancelArmTimestampRef.current = Number.MAX_SAFE_INTEGER
    }, WEATHER_ALERT_PEEK_MS)
  }, [
    weather,
    weatherAlertPeekBlocked,
    clearWeatherAlertPeekTimer,
    clearPeekHoverSuppressTimer,
    schedulePeekHoverSuppressRelease,
    recordPeekEnded,
  ])

  /** Parent hover becomes true during auto-peek (after arm): user took over — kill peek timer. */
  useEffect(() => {
    if (!weatherAlertPeek) {
      prevHoveredDuringPeekRef.current = hovered
      return
    }
    const armed = Date.now() >= peekUserCancelArmTimestampRef.current
    if (armed && hovered && !prevHoveredDuringPeekRef.current) {
      cancelWeatherPeekForUserHover()
    }
    prevHoveredDuringPeekRef.current = hovered
  }, [hovered, weatherAlertPeek, cancelWeatherPeekForUserHover])

  useEffect(() => {
    if (!window.cosmic?.onCalendarAgendaUpdate) return

    const offAgenda = window.cosmic.onCalendarAgendaUpdate((snapshot: unknown) => {
      const normalizedSnapshot = normalizeCalendarAgendaSnapshot(snapshot as Record<string, unknown>)
      calendarGeneratedAtRef.current = normalizedSnapshot.generated_at
      setCalendarData(normalizedSnapshot)
      if (calendarRefreshTimeoutRef.current) {
        clearTimeout(calendarRefreshTimeoutRef.current)
        calendarRefreshTimeoutRef.current = null
      }
      setCalendarRefreshing(false)
    })

    const offShown = window.cosmic.onShown(() => {
      const lastGeneratedAtMs = calendarGeneratedAtRef.current * 1000
      if (!lastGeneratedAtMs || Date.now() - lastGeneratedAtMs > CALENDAR_STALE_AFTER_MS) {
        requestCalendarAgenda(false)
      }
    })

    const offIntegration = window.cosmic.onIntegrationEvent((event: IntegrationToastEvent) => {
      if (event.provider !== 'google') return
      if (event.type === 'auth_success' || event.type === 'disconnect_success') {
        requestCalendarAgenda(true)
      }
    })

    requestCalendarAgenda(true)
    const intervalId = window.setInterval(() => requestCalendarAgenda(false), CALENDAR_REFRESH_MS)

    return () => {
      offAgenda?.()
      offShown?.()
      offIntegration?.()
      window.clearInterval(intervalId)
      if (calendarRefreshTimeoutRef.current) {
        clearTimeout(calendarRefreshTimeoutRef.current)
        calendarRefreshTimeoutRef.current = null
      }
    }
  }, [requestCalendarAgenda])

  useEffect(() => {
    saveAuthAttentionPrefs(authAttentionPrefs)
  }, [authAttentionPrefs])

  useEffect(() => {
    const activeKeys = new Set(authAttentionItems.map((item) => item.key))
    setAuthAttentionPrefs((current) => {
      const next = pruneAuthAttentionPrefs(current, activeKeys)
      return JSON.stringify(next) === JSON.stringify(current) ? current : next
    })
  }, [authAttentionItems])

  useEffect(() => {
    const offIntegrations = window.cosmic?.onIntegrationsUpdate((snapshot: IntegrationsSnapshot) => {
      setIntegrationsSnapshot(snapshot)
      void rebuildAuthAttentionItems(snapshot)
    })
    const offShown = window.cosmic?.onShown(() => {
      requestIntegrationsSnapshot()
      void rebuildAuthAttentionItems(integrationsSnapshotRef.current)
    })
    const offIntegration = window.cosmic?.onIntegrationEvent((event: IntegrationToastEvent) => {
      if (event.provider === 'google') {
        window.setTimeout(requestIntegrationsSnapshot, 700)
      }
    })

    requestIntegrationsSnapshot()
    void rebuildAuthAttentionItems(integrationsSnapshotRef.current)
    const intervalId = window.setInterval(() => {
      requestIntegrationsSnapshot()
      void rebuildAuthAttentionItems(integrationsSnapshotRef.current)
    }, AUTH_ATTENTION_REFRESH_MS)
    return () => {
      offIntegrations?.()
      offShown?.()
      offIntegration?.()
      window.clearInterval(intervalId)
    }
  }, [rebuildAuthAttentionItems, requestIntegrationsSnapshot])

  useEffect(() => {
    if (authAttentionReminder) return
    if (authAttentionItems.length === 0) return
    if (
      searchActive ||
      hovered ||
      internalHover ||
      showSettings ||
      voiceActive ||
      notificationEvent ||
      mailInboundNotification ||
      approvalRequestNotification ||
      integrationToast ||
      selectedCalendarEvent ||
      showMonthView ||
      agentWorkPayload
    ) {
      return
    }

    const nowMs = Date.now()
    const eligibleItems = authAttentionItems.filter((candidate) => {
      if (authAttentionPrefs.neverNotifyByKey[candidate.key]) return false
      const snoozedUntil = Number(authAttentionPrefs.snoozedUntilByKey[candidate.key] || 0)
      if (Number.isFinite(snoozedUntil) && snoozedUntil > nowMs) return false
      const lastNotifiedAt = Number(authAttentionPrefs.lastNotifiedAtByKey[candidate.key] || 0)
      return !Number.isFinite(lastNotifiedAt) || lastNotifiedAt <= 0 || nowMs - lastNotifiedAt >= AUTH_ATTENTION_REMINDER_INTERVAL_MS
    })
    const item = eligibleItems[0]
    if (!item) return

    const timer = window.setTimeout(() => {
      const visibleKeys = eligibleItems.map((candidate) => candidate.key)
      setAuthAttentionReminder({ item, count: authAttentionItems.length, keys: visibleKeys })
      setAuthAttentionPrefs((current) => ({
        ...current,
        lastNotifiedAtByKey: {
          ...current.lastNotifiedAtByKey,
          ...Object.fromEntries(visibleKeys.map((key) => [key, Date.now()])),
        },
      }))
    }, 1200)

    return () => window.clearTimeout(timer)
  }, [
    agentWorkPayload,
    approvalRequestNotification,
    authAttentionItems,
    authAttentionPrefs,
    authAttentionReminder,
    hovered,
    internalHover,
    integrationToast,
    mailInboundNotification,
    notificationEvent,
    searchActive,
    selectedCalendarEvent,
    showMonthView,
    showSettings,
    voiceActive,
  ])

  // Google integration events → temporary island toast (connect / disconnect status)
  useEffect(() => {
    const showToast = (
      tone: IntegrationToastTone,
      title: string,
      message: string,
      statusLabel: string,
      provider = 'Google',
      extra: Pick<IntegrationToastState, 'cancelable' | 'expiresAt' | 'providerId'> = {},
    ) => {
      if (integrationCancelFallbackTimerRef.current) {
        clearTimeout(integrationCancelFallbackTimerRef.current)
        integrationCancelFallbackTimerRef.current = null
      }
      const nextToast: IntegrationToastState = {
        id: ++integrationToastIdRef.current,
        tone,
        provider,
        providerId: extra.providerId ?? 'cosmic',
        title,
        message,
        statusLabel,
        ...extra,
      }
      setIntegrationToast(nextToast)
    }

    const scheduleSettingsReopen = (reopenProviderId: 'google' | 'github') => {
      if (!reopenSettingsAfterAuthRef.current) return
      reopenSettingsAfterAuthRef.current = false
      if (reopenSettingsTimerRef.current) clearTimeout(reopenSettingsTimerRef.current)
      reopenSettingsTimerRef.current = setTimeout(() => {
        reopenSettingsTimerRef.current = null
        setSettingsInitialView(reopenProviderId === 'github' ? 'integrations-github' : 'integrations-google')
        setShowSettings(true)
      }, SETTINGS_REOPEN_AFTER_AUTH_MS)
    }

    const off = window.cosmic?.onIntegrationEvent((event: IntegrationToastEvent) => {
      const providerId = String(event.provider || '').trim().toLowerCase()
      if (providerId !== 'google' && providerId !== 'github') return
      const accountName = getIntegrationAccountName(event)
      const provider = providerId === 'github' ? 'GitHub' : 'Google'
      const t = event.type
      if (t === 'auth_started') {
        const timeoutSeconds = Number(event.timeout_seconds)
        showToast(
          'progress',
          accountName ? `Connecting ${accountName}` : `Connecting ${provider}`,
          compactToastMessage(
            event.message || `Finish the ${provider} sign-in flow in your browser.`,
            `Finish the ${provider} sign-in flow in your browser.`,
          ),
          'In Progress',
          provider,
          {
            providerId,
            cancelable: event.cancelable !== false,
            expiresAt:
              Number.isFinite(timeoutSeconds) && timeoutSeconds > 0
                ? Date.now() + timeoutSeconds * 1000
                : undefined,
          },
        )
        authHandoffAtRef.current = Date.now()
        if (showSettingsRef.current) {
          reopenSettingsAfterAuthRef.current = true
          setShowSettings(false)
        }
      } else if (t === 'auth_success') {
        if (event.account_id) {
          recentlyReconnectedRef.current.set(event.account_id, Date.now())
        }
        showToast(
          'success',
          accountName ? `Connected ${accountName}` : `${provider} connected`,
          compactToastMessage(
            accountName ? `${accountName} is ready for Cosmic.` : `This ${provider} account is ready for Cosmic.`,
            `This ${provider} account is ready for Cosmic.`,
          ),
          'Connected',
          provider,
          { providerId },
        )
        scheduleSettingsReopen(providerId)
      } else if (t === 'auth_cancelled') {
        showToast(
          'error',
          accountName ? `Cancelled ${accountName}` : `${provider} sign-in cancelled`,
          compactToastMessage(
            event.message || `${provider} sign-in cancelled.`,
            `${provider} sign-in cancelled.`,
          ),
          'Cancelled',
          provider,
          { providerId },
        )
        scheduleSettingsReopen(providerId)
      } else if (t === 'auth_error') {
        showToast(
          'error',
          accountName ? `Could not connect ${accountName}` : `${provider} connection failed`,
          compactToastMessage(
            event.message || `We could not complete the ${provider} sign-in flow.`,
            `We could not complete the ${provider} sign-in flow.`,
          ),
          'Action Needed',
          provider,
          { providerId },
        )
        scheduleSettingsReopen(providerId)
      } else if (t === 'disconnect_started') {
        showToast(
          'progress',
          accountName ? `Disconnecting ${accountName}` : `Disconnecting ${provider}`,
          compactToastMessage(event.message || 'Removing account access from Cosmic.', 'Removing account access from Cosmic.'),
          'In Progress',
          provider,
          { providerId },
        )
      } else if (t === 'disconnect_success') {
        showToast(
          'success',
          accountName ? `Disconnected ${accountName}` : `${provider} disconnected`,
          compactToastMessage(
            accountName ? `${accountName} is no longer available to Cosmic.` : `This ${provider} account is no longer available to Cosmic.`,
            `This ${provider} account is no longer available to Cosmic.`,
          ),
          'Disconnected',
          provider,
          { providerId },
        )
      } else if (t === 'disconnect_error') {
        showToast(
          'error',
          accountName ? `Could not disconnect ${accountName}` : `${provider} disconnection failed`,
          compactToastMessage(event.message || `We could not disconnect this ${provider} account.`, `We could not disconnect this ${provider} account.`),
          'Action Needed',
          provider,
          { providerId },
        )
      }
    })

    const onCustom = (e: CustomEvent<IslandNotificationDetail>) => {
      const detail = e.detail || {}
      if (!detail.type || !detail.message) return
      showToast(
        detail.type,
        detail.title || 'Update',
        compactToastMessage(detail.message, detail.message),
        detail.type === 'error' ? 'Action Needed' : detail.type === 'progress' ? 'In Progress' : 'Complete',
        detail.provider || 'Cosmic',
      )
    }
    window.addEventListener('cosmic:island-notification', onCustom as EventListener)
    return () => {
      off?.()
      window.removeEventListener('cosmic:island-notification', onCustom as EventListener)
    }
  }, [])

  // Agent-at-work slide listener.
  // detail === null (or `{ stop: true }`) clears the slide.
  useEffect(() => {
    const apply = (detail: unknown) => {
      if (!detail || (detail as { stop?: boolean }).stop) {
        console.log('[island] agent-work cleared')
        setAgentWorkPayload(null)
        return
      }
      const payload = detail as AgentWorkPayload
      if (!payload.agentId) {
        console.warn('[island] agent-work payload missing agentId:', detail)
        return
      }
      console.log('[island] agent-work →', payload)
      setAgentWorkPayload(payload)
    }

    const onAgentWork = (e: Event) => {
      apply((e as CustomEvent).detail)
    }
    window.addEventListener('cosmic:island-agent-work', onAgentWork)

    // DevTools-friendly global helper:
    //   __cosmicSmokeIsland({ agentId: 'web-search' })
    //   __cosmicSmokeIsland(null)  // clear
    ;(window as AgentWorkSmokeWindow).__cosmicSmokeIsland = (payload: AgentWorkPayload | { stop: true } | null) => {
      apply(payload)
    }

    return () => {
      window.removeEventListener('cosmic:island-agent-work', onAgentWork)
      try { delete (window as AgentWorkSmokeWindow).__cosmicSmokeIsland } catch { /* ignore */ }
    }
  }, [])

  // Smoke-script bootstrap: if the dev build was launched with VITE_SMOKE_ISLAND_AGENT,
  // auto-fire the slide once after the island has mounted so the visual is visible
  // immediately without manual interaction.
  useEffect(() => {
    const env = import.meta.env as unknown as Record<string, string | undefined>
    const agentId = env.VITE_SMOKE_ISLAND_AGENT
    if (!agentId) return
    const label = env.VITE_SMOKE_ISLAND_LABEL
    const detail = env.VITE_SMOKE_ISLAND_DETAIL
    const t = setTimeout(() => {
      window.dispatchEvent(
        new CustomEvent('cosmic:island-agent-work', {
          detail: { agentId, label, detail } satisfies AgentWorkPayload,
        }),
      )
    }, 600)
    return () => clearTimeout(t)
  }, [])

  useEffect(() => {
    if (integrationToastTimerRef.current) {
      clearTimeout(integrationToastTimerRef.current)
      integrationToastTimerRef.current = null
    }

    if (!integrationToastId || integrationToastTone === 'progress') {
      return
    }

    integrationToastTimerRef.current = setTimeout(() => {
      integrationToastTimerRef.current = null
      setIntegrationToast((current) => (current?.id === integrationToastId ? null : current))

    }, 4200)

    return () => {
      if (integrationToastTimerRef.current) {
        clearTimeout(integrationToastTimerRef.current)
        integrationToastTimerRef.current = null
      }
    }
  }, [integrationToastId, integrationToastTone])

  // Live countdown on the progress panel. A wait the user can watch wind down
  // reads as a wait; an unmoving bar reads as a hang.
  useEffect(() => {
    if (integrationToastTone !== 'progress' || !integrationToastExpiresAt) return
    setIntegrationCountdownNow(Date.now())
    const interval = setInterval(() => setIntegrationCountdownNow(Date.now()), 1000)
    return () => clearInterval(interval)
  }, [integrationToastId, integrationToastTone, integrationToastExpiresAt])

  // Last resort. The bridge sends its own verdict when the deadline passes, so
  // this only fires if the bridge stopped answering mid-flow — the one case that
  // could still strand the island on "In Progress" indefinitely.
  useEffect(() => {
    if (integrationToastTone !== 'progress' || !integrationToastExpiresAt) return
    const timer = setTimeout(
      () => {
        setIntegrationToast((current) => (current?.id === integrationToastId ? null : current))
      },
      Math.max(0, integrationToastExpiresAt - Date.now()) + INTEGRATION_PROGRESS_STALE_GRACE_MS,
    )
    return () => clearTimeout(timer)
  }, [integrationToastId, integrationToastTone, integrationToastExpiresAt])

  useEffect(
    () => () => {
      if (reopenSettingsTimerRef.current) clearTimeout(reopenSettingsTimerRef.current)
      if (integrationCancelFallbackTimerRef.current) clearTimeout(integrationCancelFallbackTimerRef.current)
    },
    [],
  )

  useEffect(() => {
    if (authAttentionTimerRef.current) {
      clearTimeout(authAttentionTimerRef.current)
      authAttentionTimerRef.current = null
    }

    const reminderKey = authAttentionReminder?.item.key
    if (!reminderKey) return

    authAttentionTimerRef.current = setTimeout(() => {
      authAttentionTimerRef.current = null
      setAuthAttentionReminder((current) => (
        current?.item.key === reminderKey ? null : current
      ))
    }, AUTH_ATTENTION_AUTO_DISMISS_MS)

    return () => {
      if (authAttentionTimerRef.current) {
        clearTimeout(authAttentionTimerRef.current)
        authAttentionTimerRef.current = null
      }
    }
  }, [authAttentionReminder?.item.key])

  useEffect(() => {
    if (integrationDotsTransitionTimerRef.current) {
      clearTimeout(integrationDotsTransitionTimerRef.current)
      integrationDotsTransitionTimerRef.current = null
    }

    if (!integrationToast) {
      previousIntegrationToastToneRef.current = null
      setIntegrationDotsTransitionToastId(null)
      burstPlayedRef.current = false
      return
    }

    const previousTone = previousIntegrationToastToneRef.current
    if (integrationToast.tone === 'success' && previousTone === 'progress') {
      setIntegrationDotsTransitionToastId(integrationToast.id)
      burstPlayedRef.current = true
      integrationDotsTransitionTimerRef.current = setTimeout(() => {
        integrationDotsTransitionTimerRef.current = null
        setIntegrationDotsTransitionToastId((current) => (current === integrationToast.id ? null : current))
      }, 1800)
    } else if (integrationToast.tone !== 'progress') {
      setIntegrationDotsTransitionToastId(null)
    }

    previousIntegrationToastToneRef.current = integrationToast.tone

    return () => {
      if (integrationDotsTransitionTimerRef.current) {
        clearTimeout(integrationDotsTransitionTimerRef.current)
        integrationDotsTransitionTimerRef.current = null
      }
    }
  }, [integrationToast])

  useEffect(() => {
    if (!window.cosmic?.onVoiceTranscript) return
    const unsub = window.cosmic.onVoiceTranscript((data) => {
      if (data.is_final) {
        if (data.text && data.text !== lastFinalTranscript) {
          setVoiceHistory(prev => {
            const newHistory = [...prev.slice(-2), data.text]
            return newHistory
          })
          setLastFinalTranscript(data.text)
        }
        setVoiceTranscript('')
      } else {
        setVoiceTranscript(data.text)
      }
    })
    return () => unsub?.()
  }, [lastFinalTranscript])

  useEffect(() => {
    if (!window.cosmic?.onVoiceStatus) return
    const unsub = window.cosmic.onVoiceStatus((data) => {
      setVoiceStatus(data.status)
      if (data.error) {
        setVoiceError(data.error)
      } else if (data.status === 'connected' || data.status === 'listening') {
        setVoiceActive(true)
        setVoiceError(null)
      } else if (data.status === 'stopped' || data.status === 'disconnected') {
        setVoiceActive(false)
        setVoiceTranscript('')
      } else if (data.status === 'ready') {
        setVoiceHistory([])
        setVoiceTranscript('')
        setLastFinalTranscript('')
      }
    })
    return () => unsub?.()
  }, [])

  useEffect(() => {
    const parseVoiceShortcutEnabled = (settings: Record<string, unknown>) => {
      const raw = settings?.voiceTypingShortcutEnabled
      if (raw === undefined || raw === null || raw === '') return true
      const normalized = String(raw).trim().toLowerCase()
      return normalized !== 'false' && normalized !== '0' && normalized !== 'off' && normalized !== 'no'
    }

    const unsub = window.cosmic?.onSettingsUpdate?.((settings) => {
      setVoiceShortcutEnabled(parseVoiceShortcutEnabled(settings))
    })
    window.cosmic?.getSettings?.()
    return () => unsub?.()
  }, [])

  // Auto-navigate to voice slide when voice session starts
  useEffect(() => {
    if (voiceActive) {
      const voiceIdx = slideContentMap.findIndex(s => s === 'voice')
      if (voiceIdx !== -1) setActiveSlide(voiceIdx)
      setExpanded(true)
    }
  }, [voiceActive, slideContentMap])

  const [thumbSrc, setThumbSrc] = useState<string | null>(null)
  const lastObjectUrl = useRef<string | null>(null)
  const lastProcessedThumb = useRef<string | null>(null)

  useEffect(() => {
    const currentThumb = media.thumbnail
    if (currentThumb === lastProcessedThumb.current) return
    lastProcessedThumb.current = currentThumb
    if (lastObjectUrl.current) {
      URL.revokeObjectURL(lastObjectUrl.current)
      lastObjectUrl.current = null
    }
    const dataUrl = toDataUrlMaybe(currentThumb)
    if (!dataUrl) {
      setThumbSrc(null)
      return
    }

    if (dataUrl.startsWith('http')) {
      setThumbSrc(dataUrl)
      return
    }

    const blobUrl = dataUrlToBlobUrl(dataUrl)
    if (blobUrl) {
      lastObjectUrl.current = blobUrl
      setThumbSrc(blobUrl)
    } else {
      setThumbSrc(dataUrl)
    }
  }, [media.thumbnail, media.trackKey])

  const handleControl = (action: 'playpause' | 'next' | 'prev') => {
    window.cosmic?.controlMedia(action)
  }

  const switchSlide = (dir: 'next' | 'prev') => {
    if (dir === 'next' && activeSlide < TOTAL_SLIDES - 1) setActiveSlide(p => p + 1)
    if (dir === 'prev' && activeSlide > 0) setActiveSlide(p => p - 1)
  }

  const currentSlideType = slideContentMap[activeSlide]
  const lastWheel = useRef(0)
  const onWheel = (e: React.WheelEvent) => {
    if (
      sessionIslandActive ||
      showMonthView ||
      selectedCalendarEvent ||
      notificationEvent ||
      mailInboundNotification ||
      approvalRequestNotification ||
      integrationToast
    ) {
      return
    }
    const horizontalDelta = Math.abs(e.deltaX)
    const verticalDelta = Math.abs(e.deltaY)
    if (currentSlideType === 'calendar' && verticalDelta >= horizontalDelta) return
    const now = Date.now()
    if (now - lastWheel.current < 400) return
    const delta = horizontalDelta > verticalDelta ? e.deltaX : e.deltaY
    if (Math.abs(delta) > 20) {
      if (delta > 0) {
        if (activeSlide < TOTAL_SLIDES - 1) {
          setActiveSlide(s => s + 1)
          lastWheel.current = now
        }
      } else {
        if (activeSlide > 0) {
          setActiveSlide(s => s - 1)
          lastWheel.current = now
        }
      }
    }
  }

  const renderSession = () => (
    <div className="slide slide-session">
      <div className="session-wordmark" aria-label="COSMIC">COSMIC</div>
      <div className="session-time">
        {now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
      </div>
    </div>
  )

  const renderHome = () => (
    <div className="slide slide-home">
      <div className="home-left">
        <div className="home-time">{now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</div>
        <div className="home-date">{now.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })}</div>
      </div>
      <div className="home-right">
        <div className="status-label">ACTIVE</div>
        <div className="status-app">
          <span>{windowInfo.appName}</span>
          <div className="status-dot" />
        </div>
        {windowInfo.title !== windowInfo.appName && windowInfo.title !== 'Desktop' && (
          <div className="status-sub">{windowInfo.title.slice(0, 30)}</div>
        )}
      </div>

    </div>
  )


  const renderMusic = () => {
    const brandColor = getSourceColor(media.source)
    let displaySource = media.source || "System"
    if (displaySource.includes('.')) displaySource = displaySource.split('.')[0]
    displaySource = displaySource.charAt(0).toUpperCase() + displaySource.slice(1)

    const progress = (media.duration && media.duration > 0)
      ? Math.min(100, Math.max(0, ((media.position || 0) / media.duration) * 100))
      : 0

    return (
      <div className="slide slide-music">
        <div className="music-art">
          <div className="art-glow" style={{ backgroundImage: thumbSrc ? `url(${thumbSrc})` : 'none' }} />
          <div className="art-box">
            {thumbSrc ? <img src={thumbSrc} alt="" /> : <div className="art-empty">♪</div>}
            {media.isPlaying && <div className="art-viz">{[0, 1, 2, 3].map(i => <div key={i} style={{ animationDelay: `${i * 0.1}s` }} />)}</div>}
          </div>
        </div>

        <div
          className="music-info"
          onMouseLeave={() => {
            if (showVolume && !draggingRef.current) {
              setShowVolume(false)
            }
          }}
        >
          {!showVolume ? (
            <>
              <div className="music-text-row">
                <div className="music-title">{media.title}</div>
                <div className="music-artist">{media.artist}</div>
              </div>

              <div className="music-mid-row">
                <div className="music-source">
                  <div style={{ width: 10, height: 10 }}>
                    <SourceIcon source={media.source} color={brandColor} />
                  </div>
                  <span>{displaySource}</span>
                </div>

                <div className="music-device-wrapper">
                  <div className="music-device">
                    <SmartDeviceIcon deviceName={media.device || ""} />
                  </div>
                  <div className="custom-tooltip">{media.device || "Speaker"}</div>
                </div>
              </div>

              {media.duration && media.duration > 0 ? (
                <div className="music-progress-container">
                  <div className="music-progress-track">
                    <div className="music-progress-fill" style={{ width: `${progress}%` }} />
                  </div>
                  <div className="music-time-labels">
                    <span>{formatTime(media.position || 0)}</span>
                    <span>{formatTime(media.duration)}</span>
                  </div>
                </div>
              ) : (
                <div style={{ height: 14 }} />
              )}

              <div className="music-controls">
                <button onClick={() => handleControl('prev')} type="button">
                  <svg width="10" height="10" fill="currentColor" viewBox="0 0 24 24"><path d="M6 6h2v12H6zm3.5 6l8.5 6V6z" /></svg>
                </button>
                <button onClick={() => handleControl('playpause')} className="main" type="button">
                  {media.isPlaying ?
                    <svg width="10" height="10" fill="currentColor" viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z" /></svg> :
                    <svg width="10" height="10" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" /></svg>
                  }
                </button>
                <button onClick={() => handleControl('next')} type="button">
                  <svg width="10" height="10" fill="currentColor" viewBox="0 0 24 24"><path d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z" /></svg>
                </button>
                <button onClick={() => setShowVolume(true)} type="button">
                  <svg width="12" height="12" fill="currentColor" viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z" /></svg>
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="volume-header"><span>Volume</span><span>{localVolume}%</span></div>
              <input type="range" min="0" max="100" value={localVolume} onChange={(e) => { setLocalVolume(parseInt(e.target.value)); window.cosmic?.setVolume(parseInt(e.target.value)) }} onPointerDown={() => draggingRef.current = true} onPointerUp={() => { draggingRef.current = false; setTimeout(() => setShowVolume(false), 1000) }} className="volume-slider" />
            </>
          )}
        </div>
      </div>
    )
  }


  const renderWeather = () => {
    const temp = weather?.temp ?? '--'
    const condition = weather?.condition ?? 'Loading'
    const isDay = weather?.isDay ?? true
    const city = weather?.city ?? 'Locating...'
    const wind = weather?.wind ?? 0
    const humidity = weather?.humidity ?? 0
    const precip = weather?.precip_prob ?? 0
    const snowfall = weather?.snowfall ?? 0

    const { tier, alertMessage } = weather
      ? getWeatherAlertInfo({ wmo: weather.wmo, temp: weather.temp, high: weather.high })
      : { tier: null, alertMessage: '' }

    return (
      <div className="slide slide-weather-clean">
        <WeatherAnimation
          condition={condition}
          isDay={isDay}
          snowfall={snowfall}
          className="weather-particles"
        />

        <div className="weather-clean-content">
          <div className="weather-col-left">
            <div className="weather-temp-huge">{temp}°</div>
            <div className="weather-meta-row">
              <span className="weather-city-clean">{city}</span>
              <span className="weather-dot-sep">•</span>
              <span className="weather-cond-clean">{condition}</span>
            </div>
            {alertMessage ? (
              <div
                className={`weather-alert-badge ${tier === 'advisory' ? 'weather-alert-badge--advisory' : ''}`}
              >
                ⚠️ {alertMessage}
              </div>
            ) : null}
          </div>

          <div className="weather-col-right">
            {/* Wind */}
            <div className="stat-row">
              <svg className="stat-icon" viewBox="0 0 24 24" fill="currentColor">
                <path d="M12.79 20c-.11 0-.2-.09-.2-.2v-7.25l-2.07 2.07c-.08.08-.2.08-.28 0l-1.66-1.66a.19.19 0 0 1 0-.28L12 9.21l3.42 3.42c.08.08.08.2 0 .28l-1.66 1.66c-.08.08-.2.08-.28 0l-2.07-2.07v7.25c0 .11-.09.2-.2.2h-2.42z" />
              </svg>
              <div>
                <span className="stat-val">{wind}</span>
                <span className="stat-unit">km/h</span>
              </div>
            </div>

            {/* Humidity */}
            <div className="stat-row">
              <svg className="stat-icon" viewBox="0 0 24 24" fill="currentColor">
                <path d="M12 2c-5.33 4.55-8 8.48-8 11.8 0 4.98 3.8 8.2 8 8.2s8-3.22 8-8.2c0-3.32-2.67-7.25-8-11.8zm0 18c-3.31 0-6-2.63-6-6.2 0-2.61 2.43-5.98 6-9.59 3.57 3.61 6 6.98 6 9.59 0 3.57-2.69 6.2-6 6.2z" opacity="0.9" />
              </svg>
              <div>
                <span className="stat-val">{humidity}</span>
                <span className="stat-unit">%</span>
              </div>
            </div>

            {/* Rain Chance */}
            <div className="stat-row">
              <svg className="stat-icon" viewBox="0 0 24 24" fill="currentColor">
                <path d="M9 13c.55 0 1 .45 1 1s-.45 1-1 1-1-.45-1-1 .45-1 1-1zm3-3c.55 0 1 .45 1 1s-.45 1-1 1-1-.45-1-1 .45-1 1-1zm3 3c.55 0 1 .45 1 1s-.45 1-1 1-1-.45-1-1 .45-1 1-1z" />
                <path d="M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96zM19 18H6c-2.21 0-4-1.79-4-4 0-2.05 1.53-3.76 3.56-3.97l1.07-.11.5-.95C8.08 7.14 9.94 6 12 6c2.62 0 4.88 1.86 5.39 4.43l.3 1.5 1.53.11c1.56.1 2.78 1.41 2.78 2.96 0 1.65-1.35 3-3 3z" opacity="0.6" />
              </svg>
              <div>
                <span className="stat-val">{precip}</span>
                <span className="stat-unit">%</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    )
  }

  // --- NOTIFICATION LOGIC ---
  useEffect(() => {
    if (!calendarData.events.length) return

    // Check for events starting in exactly 5 minutes (approx window)
    const checkNotification = () => {
      const nowMs = Date.now()

      for (const evt of calendarData.events) {
        if (evt.isAllDay) continue
        // Already notified?
        const notificationKey = `${evt.account_id}:${evt.id}`
        if (notifiedEventsRef.current.has(notificationKey)) continue

        const startDate = getCalendarEventStart(evt)
        if (!startDate) continue
        const startMs = startDate.getTime()
        const diffMin = (startMs - nowMs) / 60000

        // Trigger if between 4.8 and 5.2 minutes away
        if (diffMin > 4.8 && diffMin < 5.2) {
          setNotificationEvent(evt)
          notifiedEventsRef.current.add(notificationKey)
          setExpanded(true)

          // Auto dismiss after 10 seconds if user doesn't interact
          setTimeout(() => {
            setNotificationEvent(null)
            // Only collapse if we are not hovering/searching
            if (!searchActive && !hovered && !internalHover) {
              setExpanded(false)
            }
          }, 10000)

          break // Only one notification at a time
        }
      }
    }

    // Check every 15 seconds
    const interval = setInterval(checkNotification, 15000)
    return () => clearInterval(interval)
  }, [calendarData, searchActive, hovered, internalHover])

  const renderCosmicMailNotification = () => {
    if (!mailInboundNotification) return null
    const p = mailInboundNotification
    const isBatch = p.kind === 'batch'
    const subject = p.subject || '(No subject)'
    const receivedAtMs = isBatch ? p.latestReceivedAt : p.receivedAt
    const timeLabel = formatIslandInboundRelativeTime(receivedAtMs)

    const fromLine = isBatch
      ? p.fromSummary
      : [p.fromName, p.fromAddress].filter(Boolean).join(' · ') || p.fromAddress || 'Unknown sender'
    const avatarText = isBatch ? String(Math.min(p.count, 99)) : diNotifyInitials(p.fromName, p.fromAddress)
    const mailboxShort =
      p.mailboxAddress.length > 36 ? `${p.mailboxAddress.slice(0, 34)}\u2026` : p.mailboxAddress

    const snippetClip = p.snippet?.trim()
      ? p.snippet.trim().length > 96
        ? `${p.snippet.trim().slice(0, 93).trimEnd()}\u2026`
        : p.snippet.trim()
      : ''

    return (
      <div className="slide slide-calendar slide-calendar--notify slide-calendar--notify-mail">
        <div className="cal-minimal">
          <div className="cal-main">
            <div className="cal-today cal-today--notify" aria-hidden>
              <div className="cal-header">
                <span>{isBatch ? 'NEW' : 'MAIL'}</span>
              </div>
              <div className={`cal-body${isBatch ? ' cal-body--notify-batch' : ''}`}>
                <span>{avatarText}</span>
              </div>
              <span className="cal-day-label">{timeLabel}</span>
            </div>

            <div className="cal-main-copy">
              <div className="cal-main-kicker-row">
                <span className="cal-status tone-busy">Inbound</span>
                <span className="cal-main-date" title={p.mailboxAddress}>
                  {mailboxShort}
                </span>
              </div>

              <div className="cal-main-focus">
                <span className="cal-main-title">{subject}</span>
              </div>

              <div className="cal-main-meta">
                <span>{fromLine}</span>
                {snippetClip ? (
                  <>
                    <span>{'\u00B7'}</span>
                    <span>{snippetClip}</span>
                  </>
                ) : null}
                {isBatch ? (
                  <>
                    <span>{'\u00B7'}</span>
                    <span>{p.count} new</span>
                  </>
                ) : null}
              </div>
            </div>
          </div>

          <div className="cal-side">
            <div className="cal-side-head">
              <span>Actions</span>
            </div>
            <div className="cal-list">
              {onOpenAgentEmailInbox && p.mailboxId ? (
                <button
                  type="button"
                  className="cal-row"
                  onClick={(e) => {
                    e.stopPropagation()
                    onOpenAgentEmailInbox(p.mailboxId)
                  }}
                >
                  <span className="cal-row-accent" style={{ backgroundColor: '#007AFF' }} />
                  <span className="cal-row-time">{'\u2192'}</span>
                  <div className="cal-row-copy">
                    <strong>Open inbox</strong>
                  </div>
                </button>
              ) : null}
              <button
                type="button"
                className="cal-row cal-row--notify-muted"
                onClick={(e) => {
                  e.stopPropagation()
                  setMailInboundNotification(null)
                }}
              >
                <span className="cal-row-accent cal-row-accent--muted" />
                <span className="cal-row-time"> </span>
                <div className="cal-row-copy">
                  <strong>Dismiss</strong>
                </div>
              </button>
            </div>
          </div>
        </div>
      </div>
    )
  }

  const renderCosmicMailApprovalNotification = () => {
    if (!approvalRequestNotification) return null
    const p = approvalRequestNotification
    const isBatch = p.kind === 'batch'
    const subject = p.subject || '(No subject)'
    const atMs = isBatch ? p.latestCreatedAt : p.createdAt
    const timeLabel = formatIslandInboundRelativeTime(atMs)
    const agentLine = isBatch ? p.agentSummary : p.agentName
    const mailboxShort =
      p.mailboxAddress.length > 36 ? `${p.mailboxAddress.slice(0, 34)}\u2026` : p.mailboxAddress
    const openApprovalId = !isBatch ? p.approvalId : undefined

    const approvalAvatar = isBatch ? String(Math.min(p.count, 99)) : diNotifyInitials(p.agentName, '')
    const toSuffix =
      !isBatch && p.recipients && p.recipients !== '—' ? ` · To ${p.recipients}` : ''

    const snippetClipApproval = p.snippet?.trim()
      ? p.snippet.trim().length > 96
        ? `${p.snippet.trim().slice(0, 93).trimEnd()}\u2026`
        : p.snippet.trim()
      : ''

    const metaPrimary = `${agentLine}${toSuffix}`

    return (
      <div className="slide slide-calendar slide-calendar--notify slide-calendar--notify-approval">
        <div className="cal-minimal">
          <div className="cal-main">
            <div className="cal-today cal-today--notify" aria-hidden>
              <div className="cal-header">
                <span>{isBatch ? 'NEW' : 'OUT'}</span>
              </div>
              <div className={`cal-body${isBatch ? ' cal-body--notify-batch' : ''}`}>
                <span>{approvalAvatar}</span>
              </div>
              <span className="cal-day-label">{timeLabel}</span>
            </div>

            <div className="cal-main-copy">
              <div className="cal-main-kicker-row">
                <span className="cal-status tone-warning">Approval</span>
                <span className="cal-main-date" title={p.mailboxAddress}>
                  {mailboxShort}
                </span>
              </div>

              <div className="cal-main-focus">
                <span className="cal-main-title">{subject}</span>
              </div>

              <div className="cal-main-meta">
                <span>{metaPrimary}</span>
                {snippetClipApproval ? (
                  <>
                    <span>{'\u00B7'}</span>
                    <span>{snippetClipApproval}</span>
                  </>
                ) : null}
                {isBatch ? (
                  <>
                    <span>{'\u00B7'}</span>
                    <span>{p.count} drafts</span>
                  </>
                ) : null}
              </div>
            </div>
          </div>

          <div className="cal-side">
            <div className="cal-side-head">
              <span>Actions</span>
            </div>
            <div className="cal-list">
              {onOpenAgentEmailApprovals ? (
                <button
                  type="button"
                  className="cal-row"
                  onClick={(e) => {
                    e.stopPropagation()
                    onOpenAgentEmailApprovals(openApprovalId ?? null)
                  }}
                >
                  <span
                    className="cal-row-accent"
                    style={{ backgroundColor: 'rgba(255, 190, 100, 0.95)' }}
                  />
                  <span className="cal-row-time">{'\u2192'}</span>
                  <div className="cal-row-copy">
                    <strong>Review</strong>
                  </div>
                </button>
              ) : null}
              <button
                type="button"
                className="cal-row cal-row--notify-muted"
                onClick={(e) => {
                  e.stopPropagation()
                  setApprovalRequestNotification(null)
                }}
              >
                <span className="cal-row-accent cal-row-accent--muted" />
                <span className="cal-row-time"> </span>
                <div className="cal-row-copy">
                  <strong>Dismiss</strong>
                </div>
              </button>
            </div>
          </div>
        </div>
      </div>
    )
  }

  const renderNotification = () => {
    if (!notificationEvent) return null
    const startTime = formatCalendarTime(notificationEvent.start, notificationEvent.isAllDay)
    const calLabel = notificationEvent.calendar_name || notificationEvent.account_label || 'Calendar'
    const place = (notificationEvent.location || '').trim()
    const organizer = (notificationEvent.organizer || '').trim()
    const metaParts = [calLabel, place].filter(Boolean)
    if (organizer && organizer !== (notificationEvent.email || '').trim()) {
      metaParts.push(organizer)
    }
    const metaText = metaParts.join(' · ')

    return (
      <div className="slide slide-di-notify slide-di-notify--calendar">
        <div className="di-notify-card di-notify-card--calendar">
          <div className="di-notify-strip">
            <span className="di-notify-strip-label">Calendar</span>
            <span className="di-notify-strip-mono">{startTime}</span>
            <button
              type="button"
              className="di-notify-strip-action"
              aria-label="Close notification"
              onClick={(e) => {
                e.stopPropagation()
                setNotificationEvent(null)
              }}
            >
              Close
            </button>
          </div>

          <div className="di-notify-thread-row">
            <div className="di-notify-avatar di-notify-avatar--calendar" aria-hidden>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" className="di-notify-avatar-svg">
                <path d="M19 3h-1V1h-2v2H8V1H6v2H5c-1.11 0-2 .9-2 2v14c0 1.1.89 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16H5V8h14v11z" />
              </svg>
            </div>
            <div className="di-notify-thread-main">
              <h3 className="di-notify-subject di-notify-subject--emphasis">{notificationEvent.summary}</h3>
              {metaText ? <p className="di-notify-from">{metaText}</p> : null}
              <div className="di-notify-foot">
                <span className="di-notify-pill di-notify-pill--warm">Upcoming · 5 min</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    )
  }

  const renderCalendarDetail = () => {
    if (!selectedCalendarEvent) return null

    const start = getCalendarEventStart(selectedCalendarEvent)
    const end = getCalendarEventEnd(selectedCalendarEvent)
    const dateLabel = start
      ? start.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })
      : ''
    const timeLabel = selectedCalendarEvent.isAllDay
      ? 'All day'
      : start && end
        ? `${formatCalendarTime(selectedCalendarEvent.start, false)} \u2013 ${formatCalendarTime(selectedCalendarEvent.end, false)}`
        : formatCalendarTime(selectedCalendarEvent.start, selectedCalendarEvent.isAllDay)
    const attendees = selectedCalendarEvent.attendees
    const attendeePreview = attendees.slice(0, 3)
    const attendeeOverflow = Math.max(0, attendees.length - attendeePreview.length)
    const durationLabel = getEventDurationLabel(selectedCalendarEvent)

    const relativeLabel = getCalendarRelativeLabel(selectedCalendarEvent, now)
    const isPast = relativeLabel.startsWith('Ended') || relativeLabel === 'Just ended'

    return (
      <div className="slide slide-calendar-detail">
        <div className="cal-detail">
          {/* Left: Back + Title */}
          <div className="cal-detail-main">
            <div className="cal-detail-head">
              <button
                type="button"
                className="cal-detail-back"
                onClick={() => setSelectedCalendarEvent(null)}
                aria-label={showMonthView ? 'Back to month view' : 'Back to schedule'}
              >
                <ArrowLeft size={14} />
              </button>
              <span className={`cal-detail-kicker ${isPast ? 'past' : ''}`}>{relativeLabel}</span>
              <span className="cal-detail-support">{selectedCalendarEvent.calendar_name}</span>
            </div>

            <h3 className="cal-detail-title" title={selectedCalendarEvent.summary}>{selectedCalendarEvent.summary}</h3>

            <div className="cal-detail-when" title={`${dateLabel} · ${timeLabel}${durationLabel ? ` (${durationLabel})` : ''}`}>
              {dateLabel} {'\u00B7'} {timeLabel}{durationLabel ? ` (${durationLabel})` : ''}
            </div>

            {selectedCalendarEvent.location && (
              <div className="cal-detail-location" title={selectedCalendarEvent.location}>{selectedCalendarEvent.location}</div>
            )}
          </div>

          {/* Right: Attendees + Actions */}
          <div className="cal-detail-side">
            {selectedCalendarEvent.organizer && (
              <div className="cal-detail-host">
                <span className="cal-detail-meta-label">Host</span>
                <strong title={selectedCalendarEvent.organizer}>{selectedCalendarEvent.organizer}</strong>
              </div>
            )}

            {attendees.length > 0 && (
              <div className="cal-detail-attendees">
                <span className="cal-detail-meta-label">{attendees.length} attendee{attendees.length !== 1 ? 's' : ''}</span>
                <div className="cal-detail-attendee-list">
                  {attendeePreview.map((attendee) => (
                    <span
                      key={`${attendee.email}-${attendee.response_status}`}
                      className={`cal-attendee-pill status-${attendee.response_status}`}
                      title={attendee.email || attendee.display_name}
                    >
                      {attendee.display_name || attendee.email || 'Guest'}
                    </span>
                  ))}
                  {attendeeOverflow > 0 && (
                    <span className="cal-attendee-pill overflow">+{attendeeOverflow}</span>
                  )}
                </div>
              </div>
            )}

            <div className="cal-detail-actions">
              {selectedCalendarEvent.meetingLink && (
                <button
                  type="button"
                  className="cal-detail-action join-btn"
                  onClick={() => window.cosmic?.openExternal(selectedCalendarEvent.meetingLink)}
                >
                  <Video size={13} />
                  Join
                </button>
              )}
              {selectedCalendarEvent.htmlLink && (
                <button
                  type="button"
                  className="cal-detail-action"
                  onClick={() => window.cosmic?.openExternal(selectedCalendarEvent.htmlLink)}
                >
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" style={{ flexShrink: 0 }}>
                    <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 01-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" fill="#4285F4" />
                    <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853" />
                    <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05" />
                    <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335" />
                  </svg>
                  Open
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
    )
  }

  const authAttentionCount = authAttentionItems.length

  const openAuthAttentionSettings = useCallback(() => {
    if (authAttentionTimerRef.current) {
      clearTimeout(authAttentionTimerRef.current)
      authAttentionTimerRef.current = null
    }
    const targetView = authAttentionReminder?.item.settingsView || 'integrations-google'
    setAuthAttentionReminder(null)
    setSettingsInitialView(targetView)
    setShowSettings(true)
  }, [authAttentionReminder])

  const snoozeAuthAttentionReminder = useCallback(() => {
    const item = authAttentionReminder?.item
    if (!item) return
    const keys = authAttentionReminder.keys.length > 0 ? authAttentionReminder.keys : [item.key]
    const snoozedUntil = Date.now() + AUTH_ATTENTION_SNOOZE_MS
    setAuthAttentionPrefs((current) => ({
      ...current,
      snoozedUntilByKey: {
        ...current.snoozedUntilByKey,
        ...Object.fromEntries(keys.map((key) => [key, snoozedUntil])),
      },
    }))
    setAuthAttentionReminder(null)
  }, [authAttentionReminder])

  const neverShowAuthAttentionReminder = useCallback(() => {
    const item = authAttentionReminder?.item
    if (!item) return
    const keys = authAttentionReminder.keys.length > 0 ? authAttentionReminder.keys : [item.key]
    setAuthAttentionPrefs((current) => ({
      ...current,
      neverNotifyByKey: {
        ...current.neverNotifyByKey,
        ...Object.fromEntries(keys.map((key) => [key, true])),
      },
    }))
    setAuthAttentionReminder(null)
  }, [authAttentionReminder])

  const renderAuthAttentionReminder = () => {
    if (!authAttentionReminder) return null
    const { item, count } = authAttentionReminder
    const providerLabel = item.provider === 'codex'
      ? 'Alpha Codex'
      : item.provider === 'cursor'
        ? 'Alpha Cursor'
        : item.provider === 'opencode'
          ? 'Alpha OpenCode'
          : item.provider === 'zcode'
            ? 'Alpha ZCode'
            : 'Google Workspace'
    return (
      <motion.div
        key={item.key}
        className="auth-attention-shell"
        initial={{ opacity: 0, y: 10, scale: 0.985, filter: 'blur(10px)' }}
        animate={{ opacity: 1, y: 0, scale: 1, filter: 'blur(0px)' }}
        transition={{ duration: 0.36, ease: [0.22, 1, 0.36, 1] }}
      >
        <div className="auth-attention-header">
          <div className="auth-attention-mark" aria-hidden="true">
            <BellRing size={15} strokeWidth={2.2} />
          </div>
          <div className="auth-attention-copy">
            <span className="auth-attention-provider">{providerLabel}</span>
            <strong>{item.title}</strong>
            <span className="auth-attention-account">
              {item.email ? item.email : item.accountLabel}
            </span>
            <p>{item.detail || item.message}</p>
          </div>
          <span className="auth-attention-status">
            {count > 1 ? `${count} items` : 'Reauth'}
          </span>
        </div>
        <div className="auth-attention-actions">
          <button type="button" className="auth-attention-action primary" onClick={openAuthAttentionSettings}>
            Auth now
          </button>
          <button type="button" className="auth-attention-action secondary" onClick={snoozeAuthAttentionReminder}>
            Snooze
          </button>
          <button type="button" className="auth-attention-action quiet" onClick={neverShowAuthAttentionReminder}>
            Never show
          </button>
        </div>
      </motion.div>
    )
  }

  // The system browser never tells us the sign-in tab was closed, so this is the
  // user saying it on its behalf — and it really does end the provider's wait.
  const cancelIntegrationAuth = useCallback(() => {
    if (integrationToastProviderIdRef.current === 'github') {
      window.cosmic?.cancelGitHubConnect?.()
    } else {
      window.cosmic?.cancelGoogleAccountConnect?.()
    }
    setIntegrationToast((current) =>
      current && current.tone === 'progress'
        ? { ...current, statusLabel: 'Cancelling', cancelable: false, expiresAt: undefined }
        : current,
    )
    if (integrationCancelFallbackTimerRef.current) {
      clearTimeout(integrationCancelFallbackTimerRef.current)
    }
    // The provider answers within a beat. If it cannot, the panel still leaves.
    integrationCancelFallbackTimerRef.current = setTimeout(() => {
      integrationCancelFallbackTimerRef.current = null
      setIntegrationToast((current) => (current?.tone === 'progress' ? null : current))
    }, INTEGRATION_CANCEL_FALLBACK_MS)
  }, [])

  const renderIntegrationToast = () => {
    if (!integrationToast) return null
    const remainingMs =
      integrationToast.tone === 'progress' && integrationToast.expiresAt
        ? integrationToast.expiresAt - integrationCountdownNow
        : 0
    const countdownLabel = remainingMs > 0 ? formatCountdown(remainingMs) : ''
    const dotsAreTransitioningToSuccess =
      integrationToast.tone === 'success' && integrationDotsTransitionToastId === integrationToast.id
    // Keep dot grid in DOM during burst so canvas can measure its position
    const showDotProgress = integrationToast.tone === 'progress' || dotsAreTransitioningToSuccess
    // Show the burst canvas for the entire success lifetime when burst played
    const showBurstCanvas = integrationToast.tone === 'success' && burstPlayedRef.current
    // Show the SVG checkmark only if success arrived without a burst (e.g. direct success tone)
    const showStaticCheck = integrationToast.tone === 'success' && !burstPlayedRef.current

    return (
      <motion.div
        key={integrationToast.id}
        className={`it-shell tone-${integrationToast.tone} ${dotsAreTransitioningToSuccess ? 'dots-transitioning-success' : ''}`}
        initial={{ opacity: 0, y: 10, scale: 0.985, filter: 'blur(10px)' }}
        animate={{ opacity: 1, y: 0, scale: 1, filter: 'blur(0px)' }}
        transition={{ duration: 0.38, ease: [0.22, 1, 0.36, 1] }}
        style={{ position: 'relative' }}
      >
        <div className="it-header">
          <div className="it-provider-cluster">
            <div className={`it-mark tone-${integrationToast.tone} ${showBurstCanvas ? 'burst-active' : ''}`} aria-hidden="true">
              {integrationToast.tone === 'progress' ? (
                <div className="it-spinner">
                  <span />
                  <span />
                </div>
              ) : integrationToast.tone === 'success' ? (
                // When burst played: canvas stays alive as the checkmark — no SVG needed.
                // When success arrived without burst: show the normal SVG draw animation.
                showStaticCheck ? (
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.8" strokeLinecap="round" strokeLinejoin="round">
                    <path className="it-check-short" d="M6.7 12.5l3.05 3.15" />
                    <path className="it-check-long" d="M9.75 15.65 17.7 7.85" />
                  </svg>
                ) : null
              ) : (
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                  <path className="it-error-1" d="M17.5 6.5L6.5 17.5" />
                  <path className="it-error-2" d="M6.5 6.5l11 11" />
                </svg>
              )}
            </div>

            <div className="it-copy">
              <span className="it-provider">{integrationToast.provider}</span>
              <strong className="it-title">{integrationToast.title}</strong>
            </div>
          </div>

          <div className="it-actions">
            <span className={`it-status tone-${integrationToast.tone}`}>
              {integrationToast.statusLabel}
              {countdownLabel && <span className="it-countdown">{countdownLabel}</span>}
            </span>
            {integrationToast.tone === 'progress' && integrationToast.cancelable && (
              <button type="button" className="it-cancel" onClick={cancelIntegrationAuth}>
                Cancel
              </button>
            )}
          </div>
        </div>

        {showDotProgress && (
          <div className="it-footer" style={showBurstCanvas ? { opacity: 0, pointerEvents: 'none' } : undefined}>
            <div className="it-dot-progress" aria-hidden="true">
              {Array.from({ length: DOT_PROGRESS_ROWS * DOT_PROGRESS_COLUMNS }).map((_, index) => {
                const row = Math.floor(index / DOT_PROGRESS_COLUMNS) + 1
                const column = (index % DOT_PROGRESS_COLUMNS) + 1
                return (
                  <span
                    key={`${row}-${column}`}
                    className="it-dot-progress-dot"
                    style={{
                      gridRow: row,
                      gridColumn: column,
                      animationDelay: `${(column - 1) * 0.08}s`,
                    }}
                  />
                )
              })}
            </div>
          </div>
        )}

        {/* Dot-burst canvas: particles burst from grid, converge to form checkmark */}
        {showBurstCanvas && (
          <DotBurstCheckmark />
        )}
      </motion.div>
    )
  }

  // --- OPACITY STATE ---
  // Managed by App.tsx now


  const notificationIslandActive = !!(notificationEvent || mailInboundNotification || approvalRequestNotification)

  // Override 'expanded' style if Month View or integration / auth attention toast is open
  const islandStyle = selectedCalendarEvent
    ? {}
    : showMonthView
      ? { width: '400px', height: '360px', borderRadius: '0 0 36px 36px' }
      : authAttentionReminder
        ? { width: '500px', height: '156px', borderRadius: '0 0 34px 34px' }
      : integrationToast
        ? { width: '456px', height: '136px', borderRadius: '0 0 30px 30px' }
        : notificationIslandActive
          ? ISLAND_NOTIFICATION_DIMENSIONS
          : {}

  const dynamicBgStyle = { background: `rgba(0, 0, 0, ${islandOpacity})` }

  // ... renderCalendar function
  const renderCalendar = () => {
    if (showMonthView) {
      return (
        <CalendarMonthView
          currentDate={now}
          events={calendarData.events}
          accounts={calendarData.accounts}
          onEventSelect={openCalendarEventDetail}
        />
      )
    }

    const calendarAccounts = calendarData.accounts.filter((account) => account.tool_enabled)
    const activeAccounts = calendarAccounts.filter((account) => !account.needs_reconnect)
    const reconnectAccounts = calendarAccounts.filter((account) => account.needs_reconnect)
    const upcoming = calendarData.events
      .filter((event) => {
        const end = getCalendarEventEnd(event)
        return !end || end.getTime() >= now.getTime() - 60_000
      })
    const nextEvent = upcoming[0] ?? null
    const nextEventAttendees = nextEvent?.attendees.filter((attendee) => !attendee.self) ?? []
    const todayDateLabel = now.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' })

    const statusTone = calendarRefreshing
      ? 'syncing'
      : calendarData.state === 'error'
        ? 'warning'
        : nextEvent
          ? 'busy'
          : activeAccounts.length > 0
            ? 'clear'
            : reconnectAccounts.length > 0
              ? 'warning'
              : 'idle'

    const statusLabel = calendarRefreshing
      ? 'Syncing'
      : calendarData.state === 'error'
        ? 'Sync issue'
        : nextEvent
          ? 'Next up'
          : activeAccounts.length > 0
            ? 'All clear'
            : reconnectAccounts.length > 0
              ? 'Reconnect'
              : 'Setup'

    const headline = nextEvent
      ? getCalendarRelativeLabel(nextEvent, now)
      : calendarData.state === 'error'
        ? 'Calendar sync needs attention'
        : activeAccounts.length > 0
          ? 'Nothing urgent on deck'
          : reconnectAccounts.length > 0
            ? 'Reconnect Google Calendar'
            : 'Connect Google Calendar'

    const support = nextEvent
      ? nextEvent.location || nextEvent.calendar_name || nextEvent.account_label
      : calendarData.message
      || (activeAccounts.length > 0
        ? 'No upcoming events in the current sync window.'
        : 'Use Settings > Integrations to enable Calendar on a Google account.')

    return (
      <div className="slide slide-calendar">
        <div className="cal-minimal">
          <div className="cal-main">
            {/* Date chip — minimal, borderless */}
            <button type="button" className="cal-today" onClick={() => setShowMonthView(true)} aria-label="Open month calendar">
              <div className="cal-header">
                <span>{now.toLocaleDateString([], { month: 'short' }).toUpperCase()}</span>
              </div>
              <div className="cal-body">
                <span>{now.getDate()}</span>
              </div>
              <span className="cal-day-label">{now.toLocaleDateString([], { weekday: 'short' })}</span>
            </button>

            {/* Main content */}
            <div className="cal-main-copy">
              <div className="cal-main-kicker-row">
                <span className={`cal-status tone-${statusTone}`}>{statusLabel}</span>
                <span className="cal-main-date">{todayDateLabel}</span>
                <button
                  type="button"
                  className={`cal-refresh-btn ${calendarRefreshing ? 'spinning' : ''}`}
                  onClick={() => requestCalendarAgenda(true)}
                  aria-label="Refresh calendar agenda"
                >
                  <RefreshCw size={10} />
                </button>
              </div>

              {nextEvent ? (
                <>
                  <div className="cal-main-focus">
                    <span className="cal-main-time">{formatCalendarTime(nextEvent.start, nextEvent.isAllDay)}</span>
                    <span className="cal-main-sep" />
                    <span className="cal-main-title">{nextEvent.summary}</span>
                  </div>
                  <div className="cal-main-meta">
                    <span>{getCalendarRelativeLabel(nextEvent, now)}</span>
                    <span>{'\u00B7'}</span>
                    <span>{getEventDurationLabel(nextEvent)}</span>
                    {nextEventAttendees.length > 0 && <><span>{'\u00B7'}</span><span>{nextEventAttendees.length} guest{nextEventAttendees.length !== 1 ? 's' : ''}</span></>}
                    {nextEvent.meetingLink && <><span>{'\u00B7'}</span><span>Join ready</span></>}
                  </div>
                </>
              ) : (
                <>
                  <div className="cal-main-focus empty">{headline}</div>
                  <div className="cal-main-meta single">
                    <span>{support}</span>
                  </div>
                </>
              )}
            </div>
          </div>

          {/* Right sidebar — compact agenda */}
          <div className="cal-side">
            <div className="cal-side-head">
              <span>Agenda</span>
            </div>

            {upcoming.length > 0 ? (
              <div className="cal-list">
                {upcoming.slice(0, 4).map((event, idx) => {
                  const accentColor = idx % 2 === 0 ? '#007AFF' : 'rgba(255, 255, 255, 0.65)'
                  return (
                    <button
                      key={`${event.account_id}-${event.id}-${event.start}`}
                      type="button"
                      className="cal-row"
                      onClick={() => openCalendarEventDetail(event)}
                    >
                      <span className="cal-row-accent" style={{ backgroundColor: accentColor }} />
                      <span className="cal-row-time">
                        {formatCalendarTime(event.start, event.isAllDay)}
                      </span>
                      <div className="cal-row-copy">
                        <strong>{event.summary}</strong>
                      </div>
                    </button>
                  )
                })}
              </div>
            ) : (
              <button type="button" className="cal-side-empty" onClick={() => setShowMonthView(true)}>
                <strong>{activeAccounts.length > 0 ? 'No events' : 'Connect'}</strong>
              </button>
            )}
          </div>
        </div>
      </div>
    )
  }

  const renderVoice = () => {
    const isError = voiceStatus === 'error'
    const isListening = voiceActive && !isError

    const handleToggleVoice = () => {
      if (voiceActive) {
        window.cosmic?.stopVoice()
      } else {
        window.cosmic?.startVoice()
      }
    }

    const handleVoiceShortcutToggle = () => {
      const next = !voiceShortcutEnabled
      setVoiceShortcutEnabled(next)
      window.cosmic?.setVoiceTypingShortcutEnabled?.(next)?.catch(() => {
        setVoiceShortcutEnabled(!next)
      })
    }

    const historyItems = voiceHistory.slice(-3)
    const totalHistory = voiceHistory.length

    return (
      <div className="slide slide-voice">
        <div className="voice-teleprompter">
          <div className="voice-history">
            <AnimatePresence mode="popLayout">
              {historyItems.map((text, i) => {
                const globalIndex = totalHistory - historyItems.length + i
                return (
                  <motion.div
                    key={`hist-${globalIndex}`}
                    className="voice-history-line"
                    initial={{ opacity: 0, y: 28 }}
                    animate={{ opacity: [0.3, 0.45, 0.6][i] || 0.6, y: 0 }}
                    exit={{ opacity: 0, y: -28 }}
                    transition={{ duration: 0.35, ease: 'easeOut' }}
                    layout
                  >
                    {text}
                  </motion.div>
                )
              })}
            </AnimatePresence>
          </div>
          <motion.div
            className={`voice-current ${voiceTranscript ? 'active' : ''}`}
            layout
            transition={{ duration: 0.25, ease: 'easeOut' }}
          >
            {isError ? (
              <span className="voice-error-text">{voiceError}</span>
            ) : voiceTranscript ? (
              <span className="voice-highlight">{voiceTranscript}</span>
            ) : isListening ? (
              <span className="voice-placeholder">Listening...</span>
            ) : (
              <span className="voice-placeholder">Click mic to start</span>
            )}
          </motion.div>
        </div>

        <div className="voice-mic-container">
          <button
            onClick={handleToggleVoice}
            className={`voice-mic-btn ${isListening ? 'listening' : ''} ${isError ? 'error' : ''}`}
            type="button"
            aria-label={isListening ? 'Stop listening' : 'Start listening'}
          >
            {isListening ? (
              <div className="voice-bars">
                {[0, 1, 2, 3].map(i => (
                  <div key={i} className="vbar" style={{ animationDelay: `${i * 0.15}s` }} />
                ))}
              </div>
            ) : (
              <svg viewBox="0 0 24 24" fill="currentColor" width="24" height="24">
                {isError ? (
                  <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z" />
                ) : (
                  <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm-1 1.93c-3.94-.49-7-3.85-7-7.93h2c0 3.31 2.69 6 6 6s6-2.69 6-6h2c0 4.08-3.06 7.44-7 7.93V20h4v2H8v-2h4v-4.07z" />
                )}
              </svg>
            )}
          </button>
          <div className="voice-status-label">
            {isError ? 'ERROR' : isListening ? 'LISTENING' : 'READY'}
          </div>
          <button
            type="button"
            className="voice-shortcut-toggle"
            onClick={handleVoiceShortcutToggle}
            title={
              voiceShortcutEnabled
                ? `Stop ${voiceShortcutLabel} from opening voice typing`
                : `Allow ${voiceShortcutLabel} to open voice typing`
            }
          >
            {voiceShortcutEnabled ? `Disable ${voiceShortcutLabel}` : `Enable ${voiceShortcutLabel}`}
          </button>
        </div>
      </div>
    )
  }

  const renderUtilities = () => {
    return (
      <div className="slide slide-utilities">
        <div className="utility-wrapper">
          <div className="utility-item restart" onClick={() => window.cosmic?.restartApp()}>
            <div className="utility-circle">
              <RotateCw size={24} strokeWidth={2} />
            </div>
            <span className="utility-label">Restart</span>
          </div>

          <div className="utility-item shutdown" onClick={() => window.cosmic?.quitApp()}>
            <div className="utility-circle">
              <Power size={24} strokeWidth={2} />
            </div>
            <span className="utility-label">Shut Down</span>
          </div>
        </div>
      </div>
    )
  }

  const renderContent = () => {
    if (authAttentionReminder) return renderAuthAttentionReminder()
    if (integrationToast) return renderIntegrationToast()
    if (agentWorkPayload) return <AgentWorkSlide payload={agentWorkPayload} />
    if (notificationEvent) return renderNotification()
    if (selectedCalendarEvent) return renderCalendarDetail()
    if (mailInboundNotification) return renderCosmicMailNotification()
    if (approvalRequestNotification) return renderCosmicMailApprovalNotification()
    const type = slideContentMap[activeSlide]
    if (type === 'home') return renderHome()
    if (type === 'music') return renderMusic()
    if (type === 'weather') return renderWeather()
    if (type === 'voice') return renderVoice()
    if (type === 'utilities') return renderUtilities()
    return renderCalendar()
  }

  return (
    <>
      <div
        className={`island ${sessionIslandActive ? 'session' : expanded ? 'expanded' : ''} ${authAttentionReminder ? 'auth-attention-open' : ''} ${integrationToast ? `integration-open tone-${integrationToast.tone}` : ''} ${expanded && !sessionIslandActive && notificationIslandActive ? 'island-notification-slide' : ''} ${!sessionIslandActive && agentWorkPayload ? 'agent-work-active' : ''}`}
        onMouseEnter={() => {
          if (weatherAlertPeekRef.current && Date.now() >= peekUserCancelArmTimestampRef.current) {
            cancelWeatherPeekForUserHover()
          }
          setInternalHover(true)
        }}
        onMouseLeave={() => setInternalHover(false)}
        onWheel={onWheel}
        style={{
          ...dynamicBgStyle, // Apply background opacity here
          ...(expanded && (showMonthView || selectedCalendarEvent || notificationEvent || mailInboundNotification || approvalRequestNotification || integrationToast || authAttentionReminder)
            ? islandStyle
            : {}),
          pointerEvents: 'auto'
        }}
      >
        {!expanded && <div className="notch"><div className="notch-bar" /></div>}

        {expanded && sessionIslandActive && (
          <div className="island-content island-content-session">
            {renderSession()}
          </div>
        )}

        {expanded && !sessionIslandActive && (
          <>
            {!showMonthView && !selectedCalendarEvent && !notificationEvent && !mailInboundNotification && !approvalRequestNotification && !integrationToast && !authAttentionReminder && !agentWorkPayload && (
              <>
                <div style={{ position: 'absolute', top: 0, bottom: '50px', left: 0, width: '40px', zIndex: 50, cursor: activeSlide > 0 ? 'w-resize' : 'default' }} onMouseEnter={() => switchSlide('prev')} />
                <div style={{ position: 'absolute', top: 0, bottom: '50px', right: 0, width: '40px', zIndex: 50, cursor: activeSlide < TOTAL_SLIDES - 1 ? 'e-resize' : 'default' }} onMouseEnter={() => switchSlide('next')} />
              </>
            )}

            <div className="island-content">
              {renderContent()}
            </div>

            {showMonthView && !selectedCalendarEvent && (
              <button
                onClick={() => setShowMonthView(false)}
                style={{ position: 'absolute', top: '20px', right: '20px', background: 'none', border: 'none', color: 'rgba(255,255,255,0.3)', cursor: 'pointer', zIndex: 100 }}
              >
                ✕
              </button>
            )}

            {!showMonthView && !selectedCalendarEvent && !notificationEvent && !mailInboundNotification && !approvalRequestNotification && !integrationToast && !authAttentionReminder && !agentWorkPayload && (
              <>
                <div className="island-anchor-container">
                  <button className={`anchor-btn ${isAnchored ? 'active' : ''}`} onClick={(e) => { e.stopPropagation(); setIsAnchored(!isAnchored) }}>
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d={isAnchored ? "M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z" : "M16 9h-1V7c0-1.66-1.34-3-3-3S9 5.34 9 7v2H8c-1.1 0-2 .9-2 2v8c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2v-8c0-1.1-.9-2-2-2zm-1 0H9V7c0-1.66 1.34-3 3-3s3 1.34 3 3v2z"} /></svg>
                  </button>
                </div>

                <div className="island-settings-container">
                  <button
                    className={`settings-btn ${showSettings ? 'active' : ''} ${authAttentionCount > 0 ? 'needs-attention' : ''}`}
                    aria-label={authAttentionCount > 0 ? 'Open settings. Integrations need attention.' : 'Open settings'}
                    onClick={(e) => {
                      e.stopPropagation()
                      // Driving the panel by hand outranks any pending
                      // auto-restore from an auth hand-off.
                      reopenSettingsAfterAuthRef.current = false
                      if (reopenSettingsTimerRef.current) {
                        clearTimeout(reopenSettingsTimerRef.current)
                        reopenSettingsTimerRef.current = null
                      }
                      if (showSettings) {
                        setShowSettings(false)
                      } else {
                        setSettingsInitialView('main')
                        setShowSettings(true)
                      }
                    }}
                  >
                    <svg width="12" height="12" fill="currentColor" viewBox="0 0 24 24"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32a.488.488 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 0 0-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 0 0-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z" /></svg>
                    {authAttentionCount > 0 && <span className="settings-btn-attention-dot" aria-hidden="true" />}
                  </button>
                </div>

                <div className="island-dots">
                  {Array.from({ length: TOTAL_SLIDES }).map((_, idx) => (
                    <button key={idx} className={`dot ${activeSlide === idx ? 'active' : ''}`} onClick={(e) => { e.stopPropagation(); setActiveSlide(idx) }} type="button" />
                  ))}
                </div>
              </>
            )}
          </>
        )}
      </div>

      {showSettings && (
        <Settings
          isOpen={showSettings}
          searchPosition={searchPosition}
          onPositionChange={onPositionChange}
          staybackTime={staybackTime}
          onStaybackChange={onStaybackChange}
          onClose={() => {
            // The hand-off closes this panel too. Only a close the user drove
            // themselves — well after the browser opened — cancels the restore.
            if (Date.now() - authHandoffAtRef.current > SETTINGS_AUTH_HANDOFF_CLOSE_WINDOW_MS) {
              reopenSettingsAfterAuthRef.current = false
            }
            setShowSettings(false)
          }}
          keyStatus={keyStatus}
          islandOpacity={islandOpacity}
          onOpacityChange={onOpacityChange}
          chatWideMode={chatWideMode ?? false}
          onChatWideModeChange={onChatWideModeChange ?? (() => {})}
          authData={authData}
          gatewayConnection={gatewayConnection}
          onLogout={onLogout}
          initialView={settingsInitialView}
          authAttentionCount={authAttentionCount}
        />
      )}
    </>
  )
}

function formatTime(seconds: number) {
  const mins = Math.floor(seconds / 60)
  const secs = Math.floor(seconds % 60)
  return `${mins}:${secs.toString().padStart(2, '0')}`
}

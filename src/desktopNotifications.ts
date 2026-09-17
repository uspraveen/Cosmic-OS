/**
 * Desktop overlay notices: Prophet, heartbeat, cron, and "response ready".
 *
 * These cards live outside the hidden chat surface so they can appear on the
 * desktop. Prophet is durable (gateway is source of truth). Chat-consumed
 * notices (response / cron / heartbeat) only stay up while Cosmic is not the
 * active chat surface — same idea as a Claude-in-Chrome tab notification.
 */

export type DesktopNoticeKind = 'cron' | 'heartbeat' | 'prophet' | 'response'

export type ChatNoticeSurfaceState = {
  searchState: 'hidden' | 'visible' | 'hiding'
  mode: string
  showLauncherTray: boolean
}

export type ProphetGatewayNotification = {
  notification_id?: string
  notificationId?: string
  slot?: string
  story_count?: number
  storyCount?: number
  headline?: string
  created_at?: string
  createdAt?: string
}

export type LocalProphetSettlement = {
  state: 'opened' | 'snoozed'
  snoozedUntil?: string
}

export const PROPHET_CRON_SOURCE_IDS = new Set(['prophet.morning', 'prophet.evening'])

export const isChatInactiveForNotices = (state: ChatNoticeSurfaceState): boolean => (
  state.searchState !== 'visible' ||
  state.mode !== 'chat' ||
  Boolean(state.showLauncherTray)
)

export const noticeSurvivesActiveChat = (kind?: string | null): boolean => kind === 'prophet'

export const shouldDisplayNotice = (
  kind: string | null | undefined,
  chatInactive: boolean,
): boolean => noticeSurvivesActiveChat(kind) || chatInactive

export const isProphetCronSource = (
  source?: string | null,
  sourceId?: string | null,
): boolean => (
  String(source || '').trim() === 'cron' &&
  PROPHET_CRON_SOURCE_IDS.has(String(sourceId || '').trim())
)

export const classifyAssistantNoticeKind = (
  source?: string | null,
  sourceId?: string | null,
): DesktopNoticeKind | null => {
  if (isProphetCronSource(source, sourceId)) {
    return null
  }
  const normalizedSource = String(source || '').trim()
  const normalizedSourceId = String(sourceId || '').trim()
  if (normalizedSource === 'heartbeat' || normalizedSourceId.startsWith('heartbeat:')) {
    return 'heartbeat'
  }
  if (normalizedSource === 'cron') {
    return 'cron'
  }
  return 'response'
}

export const previewNoticeContent = (content: string, limit = 220): string => {
  const text = String(content || '').replace(/\s+/g, ' ').trim()
  if (!text) {
    return ''
  }
  if (text.length <= limit) {
    return text
  }
  return `${text.slice(0, Math.max(1, limit - 1)).trim()}…`
}

export const buildProphetNoticeContent = (item: {
  slot?: string | null
  storyCount?: number | null
  headline?: string | null
}): string => {
  const slot = String(item.slot || '').trim().toLowerCase()
  const storyCount = Number(item.storyCount) || 0
  const headline = String(item.headline || '').trim()
  const storyLabel = `${storyCount} ${storyCount === 1 ? 'story' : 'stories'}`
  return [
    slot === 'evening' ? 'Evening edition ready' : 'Morning edition ready',
    storyLabel,
    headline,
  ]
    .filter(Boolean)
    .join(' · ')
}

export const prophetNoticeIdFromGateway = (item: ProphetGatewayNotification | null | undefined): string => {
  return String(item?.notification_id || item?.notificationId || '').trim()
}

export const buildProphetNoticeFromGateway = (
  item: ProphetGatewayNotification | null | undefined,
): {
  id: string
  kind: 'prophet'
  content: string
  createdAt: string
  prophetNotificationId: string
} | null => {
  if (!item || typeof item !== 'object') {
    return null
  }
  const notificationId = prophetNoticeIdFromGateway(item)
  if (!notificationId) {
    return null
  }
  const storyCount = Number(item.story_count ?? item.storyCount) || 0
  const headline = String(item.headline || '').trim()
  const slot = String(item.slot || '').trim()
  return {
    id: `prophet_edition_${notificationId}`,
    kind: 'prophet',
    content: buildProphetNoticeContent({ slot, storyCount, headline }),
    createdAt: String(item.created_at || item.createdAt || new Date().toISOString()),
    prophetNotificationId: notificationId,
  }
}

export const shouldSurfacePendingProphet = (
  notificationId: string,
  settled: Map<string, LocalProphetSettlement>,
  nowMs: number = Date.now(),
): boolean => {
  const local = settled.get(notificationId)
  if (!local) {
    return true
  }
  if (local.state === 'opened') {
    return false
  }
  if (local.state === 'snoozed') {
    const until = Date.parse(local.snoozedUntil || '')
    if (Number.isFinite(until) && until > nowMs) {
      return false
    }
    settled.delete(notificationId)
    return true
  }
  return true
}

export const pendingProphetIds = (
  items: Array<ProphetGatewayNotification | null | undefined>,
): Set<string> => {
  const ids = new Set<string>()
  for (const item of items) {
    const id = prophetNoticeIdFromGateway(item)
    if (id) {
      ids.add(id)
    }
  }
  return ids
}

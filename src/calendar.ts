export type CalendarAgendaState = 'idle' | 'ready' | 'error'

export interface CalendarAgendaAccount {
  account_id: string
  account_label: string
  email: string
  display_name: string
  status: string
  is_primary: boolean
  tool_enabled: boolean
  has_refresh_token: boolean
  needs_reconnect: boolean
  needs_scope_upgrade: boolean
  last_error: string
  upcoming_count: number
  calendar_count: number
}

export interface CalendarAgendaAttendee {
  email: string
  display_name: string
  response_status: string
  self: boolean
}

export interface CalendarAgendaEvent {
  id: string
  account_id: string
  account_label: string
  email: string
  calendar_id: string
  calendar_name: string
  calendar_color: string
  calendar_primary: boolean
  summary: string
  description: string
  organizer: string
  location: string
  start: string
  end: string
  htmlLink: string
  meetingLink: string
  status: string
  colorId: string
  isAllDay: boolean
  attendees: CalendarAgendaAttendee[]
}

export interface CalendarAgendaSnapshot {
  state: CalendarAgendaState
  generated_at: number
  message: string
  accounts: CalendarAgendaAccount[]
  events: CalendarAgendaEvent[]
}

export const EMPTY_CALENDAR_AGENDA: CalendarAgendaSnapshot = {
  state: 'idle',
  generated_at: 0,
  message: '',
  accounts: [],
  events: [],
}

function toString(value: unknown) {
  return String(value ?? '').trim()
}

function toBoolean(value: unknown) {
  return Boolean(value)
}

function toNumber(value: unknown) {
  const next = Number(value)
  return Number.isFinite(next) ? next : 0
}

export function normalizeCalendarAgendaAccount(raw: Partial<CalendarAgendaAccount> | Record<string, unknown>): CalendarAgendaAccount {
  return {
    account_id: toString(raw.account_id),
    account_label: toString(raw.account_label) || toString(raw.display_name) || toString(raw.email) || 'Google account',
    email: toString(raw.email),
    display_name: toString(raw.display_name),
    status: toString(raw.status) || 'needs_auth',
    is_primary: toBoolean(raw.is_primary),
    tool_enabled: toBoolean(raw.tool_enabled),
    has_refresh_token: toBoolean(raw.has_refresh_token),
    needs_reconnect: toBoolean(raw.needs_reconnect),
    needs_scope_upgrade: toBoolean(raw.needs_scope_upgrade),
    last_error: toString(raw.last_error),
    upcoming_count: Math.max(0, Math.round(toNumber(raw.upcoming_count))),
    calendar_count: Math.max(0, Math.round(toNumber(raw.calendar_count))),
  }
}

export function normalizeCalendarAgendaEvent(raw: Partial<CalendarAgendaEvent> | Record<string, unknown>): CalendarAgendaEvent {
  const attendees = Array.isArray(raw.attendees)
    ? raw.attendees.map((attendee) => {
      const item = attendee as Record<string, unknown>
      return {
        email: toString(item.email),
        display_name: toString(item.display_name) || toString(item.email) || 'Guest',
        response_status: toString(item.response_status) || 'needsAction',
        self: toBoolean(item.self),
      }
    })
    : []
  return {
    id: toString(raw.id) || `${toString(raw.account_id)}:${toString(raw.start)}`,
    account_id: toString(raw.account_id),
    account_label: toString(raw.account_label) || toString(raw.email) || 'Google account',
    email: toString(raw.email),
    calendar_id: toString(raw.calendar_id) || 'primary',
    calendar_name: toString(raw.calendar_name) || 'Primary',
    calendar_color: toString(raw.calendar_color),
    calendar_primary: toBoolean(raw.calendar_primary),
    summary: toString(raw.summary) || 'Untitled event',
    description: toString(raw.description),
    organizer: toString(raw.organizer),
    location: toString(raw.location),
    start: toString(raw.start),
    end: toString(raw.end) || toString(raw.start),
    htmlLink: toString(raw.htmlLink),
    meetingLink: toString(raw.meetingLink),
    status: toString(raw.status) || 'confirmed',
    colorId: toString(raw.colorId),
    isAllDay: toBoolean(raw.isAllDay),
    attendees,
  }
}

export function normalizeCalendarAgendaSnapshot(raw: Partial<CalendarAgendaSnapshot> | Record<string, unknown> | undefined | null): CalendarAgendaSnapshot {
  if (!raw || typeof raw !== 'object') return EMPTY_CALENDAR_AGENDA
  const state = toString(raw.state)
  const normalizedState: CalendarAgendaState = state === 'error' ? 'error' : state === 'ready' ? 'ready' : 'idle'
  const accounts = Array.isArray(raw.accounts) ? raw.accounts.map((account) => normalizeCalendarAgendaAccount(account as Record<string, unknown>)) : []
  const events = Array.isArray(raw.events) ? raw.events.map((event) => normalizeCalendarAgendaEvent(event as Record<string, unknown>)) : []
  return {
    state: normalizedState,
    generated_at: toNumber(raw.generated_at),
    message: toString(raw.message),
    accounts,
    events,
  }
}

function parseDateOnly(value: string) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) return null
  const [, year, month, day] = match
  return new Date(Number(year), Number(month) - 1, Number(day))
}

export function parseCalendarDate(value: string, isAllDay = false) {
  const text = toString(value)
  if (!text) return null
  if (isAllDay || text.length === 10) {
    return parseDateOnly(text)
  }
  const date = new Date(text)
  return Number.isNaN(date.getTime()) ? null : date
}

export function getCalendarEventStart(event: CalendarAgendaEvent) {
  return parseCalendarDate(event.start, event.isAllDay)
}

export function getCalendarEventEnd(event: CalendarAgendaEvent) {
  return parseCalendarDate(event.end, event.isAllDay) ?? getCalendarEventStart(event)
}

export function isSameCalendarDay(a: Date, b: Date) {
  return (
    a.getFullYear() === b.getFullYear()
    && a.getMonth() === b.getMonth()
    && a.getDate() === b.getDate()
  )
}

export function isCalendarEventOnDate(event: CalendarAgendaEvent, date: Date) {
  const start = getCalendarEventStart(event)
  if (!start) return false
  return isSameCalendarDay(start, date)
}

export function formatCalendarTime(value: string, isAllDay = false) {
  if (isAllDay) return 'All day'
  const date = parseCalendarDate(value, isAllDay)
  if (!date) return '--'
  return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }).toLowerCase()
}

export function formatCalendarRange(event: CalendarAgendaEvent) {
  if (event.isAllDay) return 'All day'
  const start = formatCalendarTime(event.start, event.isAllDay)
  const endDate = getCalendarEventEnd(event)
  if (!endDate) return start
  return `${start} - ${endDate.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }).toLowerCase()}`
}

export function getCalendarRelativeLabel(event: CalendarAgendaEvent, now = new Date()) {
  if (event.isAllDay) return 'All day'
  const start = getCalendarEventStart(event)
  const end = getCalendarEventEnd(event) ?? start
  if (!start || !end) return ''

  const diffMs = start.getTime() - now.getTime()
  if (start <= now && end >= now) return 'In progress'

  // Event already ended — show how long ago
  if (end < now) {
    const agoMs = now.getTime() - end.getTime()
    const agoMinutes = Math.round(agoMs / 60000)
    if (agoMinutes < 1) return 'Just ended'
    if (agoMinutes < 60) return `Ended ${agoMinutes}m ago`
    const agoHours = Math.floor(agoMinutes / 60)
    return `Ended ${agoHours}h ago`
  }

  const diffMinutes = Math.round(diffMs / 60000)
  if (diffMinutes <= 0) return 'Starting now'
  if (diffMinutes < 60) return `In ${diffMinutes} min`
  if (diffMinutes < 24 * 60) {
    const hours = Math.floor(diffMinutes / 60)
    const minutes = diffMinutes % 60
    return minutes > 0 ? `In ${hours}h ${minutes}m` : `In ${hours}h`
  }

  const tomorrow = new Date(now)
  tomorrow.setDate(now.getDate() + 1)
  if (isSameCalendarDay(start, tomorrow)) {
    return `Tomorrow, ${formatCalendarTime(event.start, event.isAllDay)}`
  }

  return start.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

export function formatCalendarSyncLabel(timestamp: number) {
  if (!timestamp) return 'Not synced yet'
  const ageSeconds = Math.max(0, Math.round(Date.now() / 1000 - timestamp))
  if (ageSeconds < 30) return 'Synced just now'
  if (ageSeconds < 60) return `Synced ${ageSeconds}s ago`
  const ageMinutes = Math.round(ageSeconds / 60)
  if (ageMinutes < 60) return `Synced ${ageMinutes}m ago`
  const ageHours = Math.round(ageMinutes / 60)
  return `Synced ${ageHours}h ago`
}

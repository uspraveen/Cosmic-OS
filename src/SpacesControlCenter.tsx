import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { ComponentPropsWithoutRef, CSSProperties, RefObject } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'
import LiquidGlass from './LiquidGlass'
import './spaces-control.css'
import type { CalendarAgendaEvent, CalendarAgendaSnapshot } from './calendar'
import {
  EMPTY_CALENDAR_AGENDA,
  formatCalendarRange,
  formatCalendarTime,
  getCalendarEventEnd,
  getCalendarEventStart,
  getCalendarRelativeLabel,
  isSameCalendarDay,
  normalizeCalendarAgendaSnapshot,
} from './calendar'

type SpacesPageId = 'command' | 'tools' | 'calendar' | 'prophet' | 'autopilot' | 'pulse' | 'manage' | 'agents' | 'sessions' | 'agent-email' | 'gmail'
type AgentEmailViewId = 'overview' | 'agents' | 'inboxes' | 'approvals' | 'settings'
type AccentTone = 'azure' | 'gold' | 'mint' | 'rose' | 'slate'
type MetricTone = 'good' | 'warm' | 'cool' | 'muted'

interface SpacesControlCenterProps {
  active: boolean
  gatewayState: string
  gatewayConnected: boolean
  gatewayDetail?: string
  pendingTaskCount: number
  pendingCronCount: number
  selectedModelLabel: string
  onBackToChat: () => void
  onPromptChat: (prompt: string) => void
  onMinimize: () => void
  onClose: () => void
  onShowTooltip?: (label: string, element: HTMLElement) => void
  onHideTooltip?: () => void
  containerRef?: RefObject<HTMLDivElement | null>
  containerClassName?: string
  containerStyle?: CSSProperties
  /** Increment (e.g. from Dynamic Island) to open Agent Email on the Inbox tab; optional mailbox id to select. */
  agentEmailNavigateInboxSignal?: number
  agentEmailNavigateInboxMailboxId?: string | null
  /** Increment to open Agent Email on Approvals; optional approval id to select when the list loads. */
  agentEmailNavigateApprovalsSignal?: number
  agentEmailNavigateApprovalsId?: string | null
}

interface SpacePageDef {
  id: SpacesPageId
  label: string
  kicker: string
  countLabel: string
  accent: AccentTone
}

interface SpaceMetric {
  label: string
  value: string
  note: string
  tone: MetricTone
}

interface ToolOpportunity {
  opportunity_id: string
  title: string
  tool_type: string
  goal: string
  reasoning: string
  proposed_features: string[]
  helpful_materials: string[]
  required_inputs: string[]
  status: string
  expected_value?: string | null
  alpha_project_id?: string | null
  deployment_url?: string | null
  repo_url?: string | null
}

interface OperationItem {
  title: string
  owner: string
  status: string
  channel: string
  note: string
  accent: AccentTone
}

interface CronCard {
  label: string
  schedule: string
  channel: string
  timezone: string
  note: string
  state: 'live' | 'queued' | 'draft'
}

interface MeshEvent {
  from: string
  to: string
  type: string
  note: string
  tone: 'flow' | 'wait' | 'observe'
}

interface ObservatoryCard {
  label: string
  value: string
  detail: string
  accent: AccentTone
}

interface CalendarMonthCell {
  key: string
  label: string
  muted: boolean
  isToday: boolean
  hasEvent: boolean
  date: Date | null
}

interface WeekEvent {
  id: string
  title: string
  dayIndex: number
  startHour: number
  startMinute: number
  durationMinutes: number
  accent: AccentTone
}

interface BackgroundProcess {
  title: string
  owner: string
  status: string
  channel: string
  note: string
  accent: AccentTone
}

type ProphetSection = 'breaking' | 'tech' | 'markets' | 'social' | 'science'

interface ProphetArticle {
  id: string
  section: ProphetSection
  headline: string
  summary: string
  source: string
  timeAgo: string
  accent: AccentTone
  featured?: boolean
}

interface ManageProviderDatum {
  name: string
  role: string
  tokens: string
  cost: string
  pct: number
  accent: AccentTone
}

interface ManageUsageDatum {
  label: string
  calls: string
  pct: number
  accent: AccentTone
}

interface ManageServiceDatum {
  name: string
  status: 'live' | 'idle' | 'down'
  mem: string
}

interface GatewaySystemMetrics {
  sourceEndpoint?: string
  source?: string
  fetched_at?: number | string
  fetchedAt?: number
  timestamp?: number | string
  cost?: Record<string, unknown> | null
  ledger?: Record<string, unknown> | null
  budget?: Record<string, unknown> | null
  instance?: Record<string, unknown> | null
  system?: Record<string, unknown> | null
  cpu?: Record<string, unknown> | null
  memory?: Record<string, unknown> | null
  disk?: Record<string, unknown> | null
  network?: Record<string, unknown> | null
  providers?: unknown[] | null
  services?: unknown[] | null
  usage?: Record<string, unknown> | null
  usage_by_feature?: unknown[] | null
  /** Server MIN/MAX llm_call_placed_at for limiting custom date range */
  usage_bounds?: Record<string, unknown> | null
  vm?: Record<string, unknown> | null
}

interface GatewaySessionRow {
  id: string
  title: string
  created_at: string | null
  updated_at: string | null
  message_count: number
  first_message_at: string | null
  last_message_at: string | null
}

interface GatewaySessionHistoryMessage {
  id: string
  role: string
  content: string
  route: string | null
  request_id: string | null
  in_reply_to_request_id: string | null
  channel: string | null
  created_at: string | null
  metadata: Record<string, unknown> | null
}

interface GatewayRequestTraceEvent {
  at: string | null
  event_type: string
  stage: string
  status: string
  title: string
  detail: string | null
  metadata: Record<string, unknown> | null
}

interface GatewayRequestTrace {
  request_id: string
  session_id: string
  channel: string
  route: string
  source: string | null
  source_id: string | null
  task_id: string | null
  user_query_excerpt: string | null
  status: string
  final_event_type: string | null
  final_message: string | null
  specialist_receipts: Array<Record<string, unknown>>
  delivery: Record<string, unknown> | null
  events: GatewayRequestTraceEvent[]
  created_at: string | null
  updated_at: string | null
  completed_at: string | null
}

interface GatewayAgentEmailStatus {
  configured: boolean
  connected: boolean
  adapter_registered: boolean
  healthy: boolean
  last_error: string | null
  base_url: string
  api_token: string
  primary_mailbox_address: string | null
  trusted_senders: string[]
  config_source: string | null
  explicitly_disconnected: boolean
  mail?: Record<string, unknown> | null
}

function gatewaySessionRecencyMs(session: Pick<GatewaySessionRow, 'last_message_at' | 'updated_at' | 'created_at'>): number {
  const raw = session.last_message_at || session.updated_at || session.created_at
  if (!raw) return 0
  const t = new Date(raw).getTime()
  return Number.isFinite(t) ? t : 0
}

function gatewayDailySessionDate(sessionId: string): { key: string; date: Date; sortMs: number } | null {
  const match = /^sess_(\d{4})(\d{2})(\d{2})$/.exec(String(sessionId || '').trim())
  if (!match) return null
  const year = Number(match[1])
  const monthIndex = Number(match[2]) - 1
  const day = Number(match[3])
  if (!Number.isInteger(year) || !Number.isInteger(monthIndex) || !Number.isInteger(day)) return null
  const date = new Date(year, monthIndex, day, 12, 0, 0)
  if (!Number.isFinite(date.getTime())) return null
  return {
    key: `${match[1]}-${match[2]}-${match[3]}`,
    date,
    sortMs: date.getTime(),
  }
}

function gatewaySessionGroupInfo(session: GatewaySessionRow): { key: string; label: string; sortMs: number } {
  const daily = gatewayDailySessionDate(session.id)
  if (daily) {
    return { key: daily.key, label: formatSessionDayDate(daily.date), sortMs: daily.sortMs }
  }
  const raw = session.last_message_at || session.first_message_at || session.created_at || session.updated_at
  const date = raw ? new Date(raw) : null
  if (date && Number.isFinite(date.getTime())) {
    return {
      key: `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`,
      label: formatSessionDayDate(date),
      sortMs: date.getTime(),
    }
  }
  return { key: session.id, label: 'Unknown date', sortMs: 0 }
}

function gatewaySessionMessageMs(message: Pick<GatewaySessionHistoryMessage, 'created_at'>): number {
  const raw = message.created_at
  if (!raw) return 0
  const t = new Date(raw).getTime()
  return Number.isFinite(t) ? t : 0
}

function timestampMs(value: string | null | undefined): number {
  if (!value) return 0
  const t = new Date(value).getTime()
  return Number.isFinite(t) ? t : 0
}

type AgentEmailBannerTone = 'success' | 'error' | 'info'

interface AgentEmailChecklistItem {
  label: string
  state: string
  note: string
  complete: boolean
}

interface AgentEmailAgent {
  id: string
  name: string
  role: string
  address: string
  status: string
  domain: string
  inboxes: string[]
  approvalMode: string
  lastActivity: string
  note: string
  accent: AccentTone
}

interface AgentEmailInbox {
  id: string
  address: string
  type: string
  status: string
  sync: string
  queue: string
  linkedAgents: string[]
  lastSync: string
  note: string
  accent: AccentTone
}

interface AgentEmailThreadMessage {
  id: string
  direction: 'inbound' | 'outbound'
  author: string
  address: string
  time: string
  body: string
  isRead: boolean
  attachments: CosmicMailAttachmentRead[]
}

interface AgentEmailThread {
  id: string
  subject: string
  fromName: string
  fromAddress: string
  time: string
  unread: boolean
  state: string
  snippet: string
  lastMessageAt: string
  threadSnapshot: CosmicMailThreadRead
  messagesSource: CosmicMailMessageRead[]
  messages: AgentEmailThreadMessage[]
  messagesLoaded: boolean
}

interface AgentEmailDomainRecord {
  label: string
  status: string
  value: string
}

interface AgentEmailDomain {
  id: string
  name: string
  status: string
  dns: string
  mailboxes: string
  provider: string
  reputation: string
  note: string
  records: AgentEmailDomainRecord[]
  accent: AccentTone
}

interface AgentEmailApproval {
  id: string
  subject: string
  agent: string
  mailbox: string
  recipients: string
  cc: string
  bcc: string
  state: string
  reason: string
  time: string
  summary: string
  excerpt: string
  accent: AccentTone
}

interface GmailApproval {
  id: string
  subject: string
  account: string
  recipients: string
  cc: string
  bcc: string
  state: string
  status: string
  time: string
  reviewedAt: string
  summary: string
  excerpt: string
  notes: string
  draftId: string
  threadId: string
  accent: AccentTone
}

interface CosmicMailAuthContextRead {
  is_admin: boolean
  organization_id: string | null
  api_key_id: string | null
  api_key_name: string | null
}

interface CosmicMailOrganizationRead {
  id: string
  name: string
  slug: string
  created_at: string
}

interface CosmicMailAgentMailboxBindingRead {
  mailbox_id: string
  address: string
  display_name: string | null
  domain_id: string
  domain_name: string
  label: string | null
  is_primary: boolean
  inbound_sync_enabled: boolean
  last_synced_at: string | null
  last_sync_error: string | null
}

interface CosmicMailAgentRead {
  id: string
  organization_id: string
  default_domain_id: string | null
  default_domain_name: string | null
  name: string
  slug: string
  title: string | null
  persona_summary: string | null
  system_prompt: string | null
  signature: string | null
  accent_color: string
  avatar_url: string | null
  signature_graphic_url: string | null
  approval_required: boolean
  status: string
  created_at: string
  updated_at: string
  mailboxes: CosmicMailAgentMailboxBindingRead[]
}

interface CosmicMailMailboxRead {
  id: string
  organization_id: string
  domain_id: string
  local_part: string
  address: string
  display_name: string | null
  status: string
  james_user_created: boolean
  quota_mb: number
  quota_messages: number
  inbound_sync_enabled: boolean
  last_synced_at: string | null
  last_sync_error: string | null
  created_at: string
}

interface CosmicMailDNSRecord {
  type: 'MX' | 'TXT'
  host: string
  value: string
  priority?: number | null
  ttl: number
}

interface CosmicMailDomainRead {
  id: string
  organization_id: string
  name: string
  status: string
  james_domain_created: boolean
  created_at: string
  updated_at: string
  dns_records: CosmicMailDNSRecord[]
}

interface CosmicMailDomainDeliverabilityRead {
  domain_id: string
  status: string
  james_domain_created: boolean
  mx_target: string
  mx_priority: number
  spf_value: string
  dmarc_value: string
  dkim_selector: string
  dkim_public_key: string
  dns_records: CosmicMailDNSRecord[]
}

interface CosmicMailDomainVerificationCheck {
  type: 'MX' | 'TXT'
  host: string
  expected: string
  observed: string[]
  matched: boolean
}

interface CosmicMailDomainVerificationRead {
  domain_id: string
  status: string
  all_records_present: boolean
  james_domain_created: boolean
  checks: CosmicMailDomainVerificationCheck[]
}

interface CosmicMailMailContact {
  email: string
  name: string | null
}

interface CosmicMailAttachmentRead {
  id: string
  message_id: string | null
  draft_id: string | null
  filename: string
  content_type: string
  size_bytes: number
  created_at: string
}

interface CosmicMailDraftRead {
  id: string
  organization_id: string
  mailbox_id: string
  thread_id: string | null
  reply_to_message_id: string | null
  subject: string
  to_recipients: CosmicMailMailContact[]
  cc_recipients: CosmicMailMailContact[]
  bcc_recipients: CosmicMailMailContact[]
  text_body: string | null
  html_body: string | null
  status: string
  sent_message_id: string | null
  last_error: string | null
  created_at: string
  updated_at: string
  sent_at: string | null
}

interface CosmicMailThreadRead {
  id: string
  organization_id: string
  mailbox_id: string
  subject: string
  normalized_subject: string
  snippet: string | null
  message_count: number
  last_message_at: string
  created_at: string
  updated_at: string
}

interface CosmicMailMessageRead {
  id: string
  organization_id: string
  mailbox_id: string
  thread_id: string
  draft_id: string | null
  internet_message_id: string
  source_uid: number | null
  direction: 'inbound' | 'outbound'
  folder_name: string
  subject: string
  normalized_subject: string
  in_reply_to: string | null
  references: string[]
  from_name: string | null
  from_address: string
  to_recipients: CosmicMailMailContact[]
  cc_recipients: CosmicMailMailContact[]
  bcc_recipients: CosmicMailMailContact[]
  reply_to_recipients: CosmicMailMailContact[]
  text_body: string | null
  html_body: string | null
  preview_text: string | null
  is_read: boolean
  is_bounce: boolean
  bounce_type: string | null
  sent_at: string | null
  received_at: string | null
  created_at: string
  attachments?: CosmicMailAttachmentRead[]
}

interface CosmicMailDraftSendResult {
  draft: CosmicMailDraftRead
  thread?: CosmicMailThreadRead | null
  message?: CosmicMailMessageRead | null
  queued_for_approval?: boolean
  approval_id?: string | null
}

interface CosmicMailApprovalRead {
  id: string
  organization_id: string
  agent_id: string | null
  agent_name: string | null
  mailbox_id: string
  mailbox_address: string
  draft_id: string | null
  draft: CosmicMailDraftRead | null
  status: string
  reviewer_note: string | null
  created_at: string
  reviewed_at: string | null
}

interface CosmicMailMailboxSyncResult {
  mailbox_id: string
  imported_count: number
  skipped_count: number
  last_inbound_uid: number
  synced_at: string
}

const AGENT_EMAIL_SETTINGS_KEYS = {
  baseUrl: 'cosmicMailBaseUrl',
  apiToken: 'cosmicMailApiToken',
  trustedSenders: 'cosmicMailTrustedSenders',
} as const

function parseAgentEmailTrustedSendersSetting(raw: unknown): string[] {
  if (raw == null) return []
  let list: unknown[] = []
  if (typeof raw === 'string') {
    const t = raw.trim()
    if (!t) return []
    try {
      const parsed = JSON.parse(t) as unknown
      if (!Array.isArray(parsed)) return []
      list = parsed
    } catch {
      return []
    }
  } else if (Array.isArray(raw)) {
    list = raw
  } else {
    return []
  }
  const out: string[] = []
  const seen = new Set<string>()
  for (const item of list) {
    const s = String(item).trim()
    if (!s) continue
    const key = s.toLowerCase()
    if (seen.has(key)) continue
    seen.add(key)
    out.push(s)
  }
  return out
}

function trustedSenderListSignature(value: unknown): string {
  return JSON.stringify(parseAgentEmailTrustedSendersSetting(value))
}

function isPlausibleTrustedSenderEmail(value: string): boolean {
  const t = value.trim()
  if (t.length < 5 || /\s/.test(t)) return false
  const at = t.indexOf('@')
  if (at < 1 || at === t.length - 1) return false
  const host = t.slice(at + 1)
  return host.includes('.')
}

const MANAGE_REFRESH_MS = 30_000
const MANAGE_REFRESH_TIMEOUT_MS = 8_000

type ManageUsageMode = '24h' | '7d' | '30d' | 'custom'

type ManageUsagePeriodPayload = {
  usage_days?: number
  usage_hours?: number
  usage_start?: string
  usage_end?: string
}

/** HTML date input value (yyyy-mm-dd, local calendar day) → UTC ISO at start of that local day. */
function localDateYmdToUtcRangeStart(ymd: string): string {
  const p = ymd.trim()
  if (!/^\d{4}-\d{2}-\d{2}$/.test(p)) return ''
  const [y, m, d] = p.split('-').map((x) => Number.parseInt(x, 10))
  if (!y || !m || !d) return ''
  const local = new Date(y, m - 1, d, 0, 0, 0, 0)
  return local.toISOString().replace(/\.\d{3}Z$/, 'Z')
}

/** End of local calendar day → UTC ISO (inclusive window on gateway). */
function localDateYmdToUtcRangeEnd(ymd: string): string {
  const p = ymd.trim()
  if (!/^\d{4}-\d{2}-\d{2}$/.test(p)) return ''
  const [y, m, d] = p.split('-').map((x) => Number.parseInt(x, 10))
  if (!y || !m || !d) return ''
  const local = new Date(y, m - 1, d, 23, 59, 59, 999)
  return local.toISOString().replace(/\.\d{3}Z$/, 'Z')
}

function formatYmdLocaleLong(ymd: string): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(ymd.trim())) return ''
  const [y, m, d] = ymd.split('-').map((x) => Number.parseInt(x, 10))
  return new Date(y, m - 1, d).toLocaleDateString(undefined, {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  })
}

function isValidYmd(ymd: string): boolean {
  return /^\d{4}-\d{2}-\d{2}$/.test(ymd.trim())
}

/** yyyy-mm-dd for min= on date inputs: UTC month containing earliest stored usage row. */
function utcMonthStartYmdFromIso(iso: string | null | undefined): string {
  if (!iso || typeof iso !== 'string' || !iso.trim()) return ''
  const raw = iso.trim()
  const t = Date.parse(raw.endsWith('Z') ? raw : `${raw.replace(/\.\d+$/, '')}Z`)
  if (Number.isNaN(t)) return ''
  const d = new Date(t)
  const y = d.getUTCFullYear()
  const m = d.getUTCMonth()
  return `${y}-${String(m + 1).padStart(2, '0')}-01`
}

function offsetYmdLocal(dayOffset: number): string {
  const n = new Date()
  n.setDate(n.getDate() + dayOffset)
  return `${n.getFullYear()}-${String(n.getMonth() + 1).padStart(2, '0')}-${String(n.getDate()).padStart(2, '0')}`
}

function todayYmdLocal(): string {
  const n = new Date()
  return `${n.getFullYear()}-${String(n.getMonth() + 1).padStart(2, '0')}-${String(n.getDate()).padStart(2, '0')}`
}

function clampYmdToBounds(ymd: string, minYmd: string, maxYmd: string): string {
  if (!isValidYmd(ymd)) return ''
  let next = ymd.trim()
  if (minYmd && next < minYmd) next = minYmd
  if (maxYmd && next > maxYmd) next = maxYmd
  return next
}

function defaultCustomUsageStartYmd(minYmd: string, maxYmd: string): string {
  return clampYmdToBounds(offsetYmdLocal(-29), minYmd, maxYmd)
}

function manageUsageOptionsForMode(
  mode: ManageUsageMode,
  customStart: string,
  customEnd: string,
  fallback: ManageUsagePeriodPayload,
): ManageUsagePeriodPayload {
  switch (mode) {
    case '24h':
      return { usage_hours: 24 }
    case '7d':
      return { usage_days: 7 }
    case '30d':
      return { usage_days: 30 }
    case 'custom': {
      const startIso = localDateYmdToUtcRangeStart(customStart)
      if (!startIso) return fallback
      const endIso = localDateYmdToUtcRangeEnd(customEnd)
      return endIso ? { usage_start: startIso, usage_end: endIso } : fallback
    }
    default:
      return { usage_days: 30 }
  }
}

const MANAGE_PROVIDER_ACCENTS: AccentTone[] = ['azure', 'gold', 'mint', 'rose', 'slate']

const MANAGE_PROVIDER_FALLBACK: ManageProviderDatum[] = [
  { name: 'Anthropic', role: 'Orchestration', tokens: '1.82M tokens', cost: '$14.20', pct: 70, accent: 'azure' },
  { name: 'Fireworks', role: 'Kimi path', tokens: '0 tokens', cost: '$0.00', pct: 0, accent: 'slate' },
  { name: 'Perplexity', role: 'Search & vectors', tokens: '420K tokens', cost: '$3.60', pct: 18, accent: 'gold' },
  { name: 'Deepgram', role: 'Voice', tokens: '6.2 hrs audio', cost: '$2.10', pct: 10, accent: 'mint' },
  { name: 'Groq', role: 'Fast inference', tokens: '310K tokens', cost: '$0.45', pct: 2, accent: 'rose' },
]

const MANAGE_USAGE_FALLBACK: ManageUsageDatum[] = [
  { label: 'Chat', calls: '1,240', pct: 48, accent: 'azure' },
  { label: 'Tasks', calls: '386', pct: 22, accent: 'mint' },
  { label: 'Cron jobs', calls: '214', pct: 16, accent: 'gold' },
  { label: 'Voice', calls: '78', pct: 9, accent: 'rose' },
  { label: 'Heartbeats', calls: '52', pct: 4, accent: 'rose' },
  { label: 'Search', calls: '38', pct: 1, accent: 'slate' },
]

const MANAGE_SERVICES_FALLBACK: ManageServiceDatum[] = [
  { name: 'Gateway', mem: '142 MB', status: 'live' },
  { name: 'Orchestrator', mem: '98 MB', status: 'live' },
  { name: 'Redis', mem: '64 MB', status: 'live' },
  { name: 'Qdrant', mem: '210 MB', status: 'live' },
  { name: 'Meeting bridge', mem: '18 MB', status: 'idle' },
]

function toRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

function toArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function toNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string') {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

function pickString(container: unknown, keys: string[]): string | null {
  const source = toRecord(container)
  if (!source) return null
  for (const key of keys) {
    const value = source[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return null
}

function pickNumber(container: unknown, keys: string[]): number | null {
  const source = toRecord(container)
  if (!source) return null
  for (const key of keys) {
    const value = toNumber(source[key])
    if (value !== null) return value
  }
  return null
}

function normalizePercent(value: number | null): number {
  if (value === null || !Number.isFinite(value)) return 0
  const normalized = value <= 1 ? value * 100 : value
  return Math.max(0, Math.min(100, Math.round(normalized)))
}

function formatCurrency(value: number | null, currency = 'USD'): string {
  if (value === null || !Number.isFinite(value)) return '$0.00'
  return `${currency === 'USD' ? '$' : currency}${value.toFixed(2)}`
}

function formatBytes(bytes: unknown): string {
  const value = toNumber(bytes)
  if (value === null) return '—'
  if (!Number.isFinite(value) || value <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let index = 0
  let current = value
  while (current >= 1024 && index < units.length - 1) {
    current /= 1024
    index += 1
  }
  const decimals = current >= 10 || index === 0 ? 0 : 1
  return `${current.toFixed(decimals)} ${units[index]}`
}

function toErrorMessage(error: unknown, fallbackMessage = 'Unable to fetch live VM metrics.'): string {
  if (error instanceof Error && typeof error.message === 'string' && error.message.trim()) {
    return error.message
      .replace(/^Error invoking remote method '[^']+':\s*/i, '')
      .replace(/^Error:\s*/i, '')
  }
  if (typeof error === 'string' && error.trim()) {
    return error
      .replace(/^Error invoking remote method '[^']+':\s*/i, '')
      .replace(/^Error:\s*/i, '')
  }
  return fallbackMessage
}

interface RegistryAgentRow {
  agent_id: string
  display_name: string
  description: string
  status: string
  intents: string[]
  healthy_instance: boolean
  instance_id: string | null
}

function normalizeRegistryAgentsPayload(raw: unknown): { agents: RegistryAgentRow[]; fetchedAtMs: number | null } {
  const rec = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
  const agentsRaw = rec.agents
  const list = Array.isArray(agentsRaw) ? agentsRaw : []
  const agents: RegistryAgentRow[] = []
  for (const item of list) {
    if (!item || typeof item !== 'object') continue
    const a = item as Record<string, unknown>
    const agentId = String(a.agent_id || '').trim()
    if (!agentId) continue
    const displayName = String(a.display_name || agentId).trim() || agentId
    const intentsRaw = a.intents
    const intents = Array.isArray(intentsRaw)
      ? intentsRaw.map((x) => String(x || '').trim()).filter(Boolean)
      : []
    agents.push({
      agent_id: agentId,
      display_name: displayName,
      description: typeof a.description === 'string' ? a.description.trim() : '',
      status: typeof a.status === 'string' ? a.status.trim() : '',
      intents,
      healthy_instance: Boolean(a.healthy_instance),
      instance_id: a.instance_id != null && String(a.instance_id).trim() ? String(a.instance_id).trim() : null,
    })
  }
  const fetchedAtMs =
    typeof rec.fetched_at_ms === 'number' && Number.isFinite(rec.fetched_at_ms) ? rec.fetched_at_ms : null
  return { agents, fetchedAtMs }
}

const SPACES_REGISTRY_REFRESH_MS = 90 * 1000
const SPACES_SESSIONS_REFRESH_MS = 120 * 1000

function normalizeGatewaySessionsPayload(raw: unknown): GatewaySessionRow[] {
  const source = toRecord(raw)
  const sessionsRaw = toArray(source?.sessions)
  const sessions: GatewaySessionRow[] = []
  for (const item of sessionsRaw) {
    const row = toRecord(item)
    if (!row) continue
    const id = String(row.id || '').trim()
    if (!id) continue
    const title = String(row.title || id).trim() || id
    const createdAt = typeof row.created_at === 'string' && row.created_at.trim() ? row.created_at.trim() : null
    const updatedAt = typeof row.updated_at === 'string' && row.updated_at.trim() ? row.updated_at.trim() : null
    const firstMessageAt =
      typeof row.first_message_at === 'string' && row.first_message_at.trim() ? row.first_message_at.trim() : null
    const lastMessageAt =
      typeof row.last_message_at === 'string' && row.last_message_at.trim() ? row.last_message_at.trim() : null
    const rawMessageCount = typeof row.message_count === 'number' ? row.message_count : Number(row.message_count ?? 0)
    sessions.push({
      id,
      title,
      created_at: createdAt,
      updated_at: updatedAt,
      message_count: Number.isFinite(rawMessageCount) ? Math.max(0, rawMessageCount) : 0,
      first_message_at: firstMessageAt,
      last_message_at: lastMessageAt,
    })
  }
  return sessions
}

function normalizeGatewaySessionHistoryPayload(raw: unknown): { sessionId: string | null; messages: GatewaySessionHistoryMessage[] } {
  const source = toRecord(raw)
  const sessionId = typeof source?.session_id === 'string' && source.session_id.trim() ? source.session_id.trim() : null
  const messagesRaw = toArray(source?.messages)
  const messages: GatewaySessionHistoryMessage[] = []
  for (const item of messagesRaw) {
    const row = toRecord(item)
    if (!row) continue
    const createdAt = typeof row.created_at === 'string' && row.created_at.trim() ? row.created_at.trim() : null
    const contentValue = row.content
    let content = ''
    if (typeof contentValue === 'string') {
      content = contentValue
    } else if (contentValue != null) {
      try {
        content = JSON.stringify(contentValue)
      } catch {
        content = String(contentValue)
      }
    }
    messages.push({
      id: String(row.id || `${row.role || 'message'}:${createdAt || messages.length}`).trim(),
      role: String(row.role || 'unknown').trim() || 'unknown',
      content: content.trim(),
      route: typeof row.route === 'string' && row.route.trim() ? row.route.trim() : null,
      request_id: typeof row.request_id === 'string' && row.request_id.trim() ? row.request_id.trim() : null,
      in_reply_to_request_id:
        typeof row.in_reply_to_request_id === 'string' && row.in_reply_to_request_id.trim()
          ? row.in_reply_to_request_id.trim()
          : null,
      channel: typeof row.channel === 'string' && row.channel.trim() ? row.channel.trim() : null,
      created_at: createdAt,
      metadata: toRecord(row.metadata),
    })
  }
  return { sessionId, messages }
}

function normalizeGatewayRequestTracePayload(raw: unknown): { sessionId: string | null; requestTraces: GatewayRequestTrace[] } {
  const source = toRecord(raw)
  const sessionId = typeof source?.session_id === 'string' && source.session_id.trim() ? source.session_id.trim() : null
  const tracesRaw = toArray(source?.request_traces)
  const requestTraces: GatewayRequestTrace[] = []
  for (const item of tracesRaw) {
    const row = toRecord(item)
    if (!row) continue
    const requestId = String(row.request_id || '').trim()
    if (!requestId) continue
    const eventsRaw = toArray(row.events)
    const events: GatewayRequestTraceEvent[] = []
    for (const eventItem of eventsRaw) {
      const event = toRecord(eventItem)
      if (!event) continue
      events.push({
        at: typeof event.at === 'string' && event.at.trim() ? event.at.trim() : null,
        event_type: String(event.event_type || '').trim() || 'event',
        stage: String(event.stage || '').trim() || 'event',
        status: String(event.status || '').trim() || 'unknown',
        title: String(event.title || event.event_type || '').trim() || 'Event',
        detail: typeof event.detail === 'string' && event.detail.trim() ? event.detail.trim() : null,
        metadata: toRecord(event.metadata),
      })
    }
    events.sort((a, b) => timestampMs(a.at) - timestampMs(b.at))
    requestTraces.push({
      request_id: requestId,
      session_id: typeof row.session_id === 'string' && row.session_id.trim() ? row.session_id.trim() : sessionId || '',
      channel: String(row.channel || '').trim() || 'unknown',
      route: String(row.route || '').trim() || 'opus',
      source: typeof row.source === 'string' && row.source.trim() ? row.source.trim() : null,
      source_id: typeof row.source_id === 'string' && row.source_id.trim() ? row.source_id.trim() : null,
      task_id: typeof row.task_id === 'string' && row.task_id.trim() ? row.task_id.trim() : null,
      user_query_excerpt: typeof row.user_query_excerpt === 'string' && row.user_query_excerpt.trim() ? row.user_query_excerpt.trim() : null,
      status: String(row.status || '').trim() || 'unknown',
      final_event_type: typeof row.final_event_type === 'string' && row.final_event_type.trim() ? row.final_event_type.trim() : null,
      final_message: typeof row.final_message === 'string' && row.final_message.trim() ? row.final_message.trim() : null,
      specialist_receipts: toArray(row.specialist_receipts).map((receipt) => toRecord(receipt)).filter(Boolean) as Array<Record<string, unknown>>,
      delivery: toRecord(row.delivery),
      events,
      created_at: typeof row.created_at === 'string' && row.created_at.trim() ? row.created_at.trim() : null,
      updated_at: typeof row.updated_at === 'string' && row.updated_at.trim() ? row.updated_at.trim() : null,
      completed_at: typeof row.completed_at === 'string' && row.completed_at.trim() ? row.completed_at.trim() : null,
    })
  }
  requestTraces.sort(
    (a, b) =>
      timestampMs(a.created_at || a.completed_at || a.updated_at) -
      timestampMs(b.created_at || b.completed_at || b.updated_at),
  )
  return { sessionId, requestTraces }
}

function normalizeGatewayAgentEmailStatus(raw: unknown): GatewayAgentEmailStatus {
  const source = toRecord(raw)
  return {
    configured: Boolean(source?.configured),
    connected: Boolean(source?.connected),
    adapter_registered: Boolean(source?.adapter_registered),
    healthy: Boolean(source?.healthy),
    last_error: typeof source?.last_error === 'string' && source.last_error.trim() ? source.last_error.trim() : null,
    base_url: typeof source?.base_url === 'string' ? source.base_url.trim() : '',
    api_token: typeof source?.api_token === 'string' ? source.api_token.trim() : '',
    primary_mailbox_address:
      typeof source?.primary_mailbox_address === 'string' && source.primary_mailbox_address.trim()
        ? source.primary_mailbox_address.trim()
        : null,
    trusted_senders: parseAgentEmailTrustedSendersSetting(source?.trusted_senders),
    config_source: typeof source?.config_source === 'string' && source.config_source.trim() ? source.config_source.trim() : null,
    explicitly_disconnected: Boolean(source?.explicitly_disconnected),
    mail: toRecord(source?.mail),
  }
}

function normalizeGmailApprovals(raw: unknown): GmailApproval[] {
  const source = toRecord(raw)
  const list = Array.isArray(source?.approvals) ? source.approvals : Array.isArray(raw) ? raw : []
  return list
    .map((item): GmailApproval | null => {
      const row = toRecord(item)
      if (!row) return null
      const status = typeof row.status === 'string' ? row.status.trim() : ''
      const to = Array.isArray(row.to) ? row.to.map((v) => String(v || '').trim()).filter(Boolean) : []
      const cc = Array.isArray(row.cc) ? row.cc.map((v) => String(v || '').trim()).filter(Boolean) : []
      const bcc = Array.isArray(row.bcc) ? row.bcc.map((v) => String(v || '').trim()).filter(Boolean) : []
      const body = typeof row.body_text === 'string' && row.body_text.trim()
        ? row.body_text.trim()
        : typeof row.body_preview === 'string'
          ? row.body_preview.trim()
          : ''
      const account =
        (typeof row.account_label === 'string' && row.account_label.trim()) ||
        (typeof row.account_email === 'string' && row.account_email.trim()) ||
        (typeof row.account_id === 'string' && row.account_id.trim()) ||
        'Gmail account'
      return {
        id: typeof row.approval_id === 'string' ? row.approval_id.trim() : '',
        subject: typeof row.subject === 'string' && row.subject.trim() ? row.subject.trim() : '(No subject)',
        account,
        recipients: to.join(', ') || '—',
        cc: cc.join(', '),
        bcc: bcc.join(', '),
        state: humanizeAgentEmailValue(status || 'pending'),
        status: status || 'pending',
        time: formatAgentEmailRelative(typeof row.created_at === 'string' ? row.created_at : ''),
        reviewedAt: formatAgentEmailAbsolute(typeof row.reviewed_at === 'string' ? row.reviewed_at : ''),
        summary: body || 'No draft body available.',
        excerpt: body || 'No draft body available.',
        notes:
          (typeof row.reviewer_note === 'string' && row.reviewer_note.trim()) ||
          (typeof row.notes === 'string' && row.notes.trim()) ||
          'Waiting for review',
        draftId: typeof row.draft_id === 'string' ? row.draft_id.trim() : '',
        threadId: typeof row.thread_id === 'string' ? row.thread_id.trim() : '',
        accent: mapAgentEmailAccent(status || 'pending', 'approval'),
      }
    })
    .filter((item): item is GmailApproval => Boolean(item?.id))
}

function formatSessionDayDate(date: Date | null): string {
  if (!date || !Number.isFinite(date.getTime())) return 'Unknown date'
  const today = new Date()
  const todayMidnight = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime()
  const targetMidnight = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()
  const diffDays = Math.round((todayMidnight - targetMidnight) / 86400000)
  if (diffDays === 0) return 'Today'
  if (diffDays === 1) return 'Yesterday'
  return date.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' })
}

function formatSessionRowTime(value: string | null): string {
  if (!value) return 'Unknown'
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return 'Unknown'
  return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

function formatSessionAbsolute(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return '—'
  return date.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

/** Short date/time for compact session header meta (no shouting labels). */
function formatSessionMetaStamp(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return '—'
  return date.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

const SESSION_MARKDOWN_REMARK = [remarkGfm, remarkMath]
const SESSION_MARKDOWN_REHYPE = [rehypeKatex]
type MarkdownElementProps<Tag extends keyof HTMLElementTagNameMap> = ComponentPropsWithoutRef<Tag> & { node?: unknown }
type MarkdownCodeProps = MarkdownElementProps<'code'> & { inline?: boolean }

function SessionMessageMarkdown({ source }: { source: string }) {
  const text = String(source ?? '').trim()
  return (
    <div className="response-content spaces-sessions-msg-markdown-host">
      <ReactMarkdown
        remarkPlugins={SESSION_MARKDOWN_REMARK}
        rehypePlugins={SESSION_MARKDOWN_REHYPE}
        components={{
          table: ({ node, ...props }: MarkdownElementProps<'table'>) => {
            void node
            return (
              <div className="table-wrapper">
                <table {...props} />
              </div>
            )
          },
          pre: ({ node, className, ...props }: MarkdownElementProps<'pre'>) => {
            void node
            return <pre className={['code-block', className].filter(Boolean).join(' ')} {...props} />
          },
          code: ({ node, inline, className, children, ...props }: MarkdownCodeProps) => {
            void node
            if (inline) {
              return (
                <code className="inline-code" {...props}>
                  {children}
                </code>
              )
            }
            return <code className={className} {...props}>{children}</code>
          },
          a: ({ node, ...props }: MarkdownElementProps<'a'>) => {
            void node
            return <a target="_blank" rel="noopener noreferrer" {...props} />
          },
        }}
      >
        {text || '*No readable content.*'}
      </ReactMarkdown>
    </div>
  )
}

function sessionRoleLabel(role: string): string {
  const normalized = String(role || '').trim().toLowerCase()
  if (!normalized) return 'Unknown'
  if (normalized === 'assistant') return 'Assistant'
  if (normalized === 'user') return 'You'
  if (normalized === 'system') return 'System'
  if (normalized === 'tool') return 'Tool'
  return normalized.charAt(0).toUpperCase() + normalized.slice(1)
}

function sessionChannelLabel(channel: string | null | undefined): string {
  const raw = String(channel || '').trim()
  if (!raw) return 'Desktop'
  const lower = raw.toLowerCase()
  if (lower === '__session_rollover__') return 'Rollover'
  if (lower.startsWith('desktop:')) return 'Desktop'
  if (lower.startsWith('mobile:')) return 'Mobile'
  if (lower.startsWith('whatsapp:')) return 'WhatsApp'
  if (lower.startsWith('telegram:')) return 'Telegram'
  if (lower === 'agent-email' || lower.startsWith('agent-email:')) return 'Agent Email'
  const prefix = raw.split(':', 1)[0]?.trim()
  return prefix
    ? prefix.replace(/[-_]+/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase())
    : 'Gateway'
}

function isRenderableSessionConversationMessage(message: GatewaySessionHistoryMessage): boolean {
  const role = String(message.role || '').trim().toLowerCase()
  if (role !== 'user' && role !== 'assistant') return false
  if (message.metadata?.compacted_summary) return false
  if (String(message.channel || '').trim() === '__session_rollover__') return false

  const content = String(message.content || '').trim()
  if (!content) return false

  const lowerContent = content.toLowerCase()
  if (
    lowerContent === 'channel delivery completed' ||
    lowerContent.startsWith('gateway delivery=') ||
    lowerContent.includes('\ngateway delivery=')
  ) {
    return false
  }
  if (message.metadata?.gateway_delivery_status && lowerContent.includes('gateway delivery=')) {
    return false
  }
  return true
}

function humanizeAgentEmailValue(value: unknown): string {
  const source = String(value || '').trim()
  if (!source) return 'Unknown'
  return source
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, (char) => char.toUpperCase())
}

function formatAgentEmailRelative(value: string | null | undefined): string {
  if (!value) return '—'
  const timestamp = new Date(value).getTime()
  if (!Number.isFinite(timestamp)) return '—'
  const diffMs = Date.now() - timestamp
  const diffMinutes = Math.round(diffMs / 60000)
  if (Math.abs(diffMinutes) < 1) return 'now'
  if (Math.abs(diffMinutes) < 60) return `${Math.abs(diffMinutes)}m ago`
  const diffHours = Math.round(diffMinutes / 60)
  if (Math.abs(diffHours) < 24) return `${Math.abs(diffHours)}h ago`
  const diffDays = Math.round(diffHours / 24)
  if (Math.abs(diffDays) < 7) return `${Math.abs(diffDays)}d ago`
  return new Date(value).toLocaleDateString([], { month: 'short', day: 'numeric' })
}

function formatAgentEmailAbsolute(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return '—'
  return date.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

function getAgentEmailInitials(name: string, maxChars = 2): string {
  const trimmed = String(name || '').trim()
  if (!trimmed) return '?'
  const parts = trimmed.split(/\s+/).filter(Boolean)
  if (parts.length === 1) {
    return parts[0].slice(0, maxChars).toUpperCase()
  }
  const a = parts[0][0] || ''
  const b = parts[parts.length - 1][0] || ''
  return `${a}${b}`.toUpperCase()
}

function stripAgentEmailHtml(value: string | null | undefined): string {
  return String(value || '')
    .replace(/<style[\s\S]*?<\/style>/gi, ' ')
    .replace(/<script[\s\S]*?<\/script>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

function buildAgentEmailMessageBody(message: CosmicMailMessageRead): string {
  return String(message.text_body || '').trim() || stripAgentEmailHtml(message.html_body) || String(message.preview_text || '').trim() || 'No readable message body.'
}

function buildAgentEmailSnippet(message: CosmicMailMessageRead | null | undefined, fallback?: string | null): string {
  if (message) {
    const preview = String(message.preview_text || '').trim() || buildAgentEmailMessageBody(message)
    if (preview) return preview.slice(0, 180)
  }
  return String(fallback || '').trim() || 'No preview available.'
}

function normalizeAgentEmailAddr(value: string | null | undefined): string {
  return String(value || '').trim().toLowerCase()
}

function formatAgentEmailAttachmentSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  if (bytes >= 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`
  return `${Math.max(1, Math.round(bytes))} B`
}

function getElectronLocalFilePath(file: File): string | null {
  const extended = file as File & { path?: string }
  if (extended.path && typeof extended.path === 'string') {
    return extended.path
  }
  return null
}

function buildAgentEmailThreadReplyDraftPayload(
  thread: CosmicMailThreadRead,
  mailboxAddress: string,
  messages: CosmicMailMessageRead[],
  textBody: string,
): {
  mailbox_id: string
  thread_id: string
  reply_to_message_id: string | null
  subject: string
  to_recipients: CosmicMailMailContact[]
  cc_recipients: CosmicMailMailContact[]
  bcc_recipients: CosmicMailMailContact[]
  text_body: string | null
  html_body: string | null
} {
  const sorted = [...messages].sort((a, b) => {
    const aTime = new Date(a.received_at || a.sent_at || a.created_at).getTime()
    const bTime = new Date(b.received_at || b.sent_at || b.created_at).getTime()
    return aTime - bTime
  })
  const lastMessage = sorted[sorted.length - 1]
  if (!lastMessage) {
    throw new Error('Cannot reply: thread has no messages.')
  }
  const mbox = normalizeAgentEmailAddr(mailboxAddress)
  const replyTo = lastMessage.reply_to_recipients?.length
    ? lastMessage.reply_to_recipients
    : lastMessage.to_recipients
  let to_recipients: CosmicMailMailContact[]
  if (normalizeAgentEmailAddr(lastMessage.from_address) !== mbox) {
    to_recipients = [{ email: lastMessage.from_address, name: lastMessage.from_name }]
  } else {
    to_recipients = (replyTo && replyTo.length > 0)
      ? replyTo
      : [{ email: lastMessage.from_address, name: lastMessage.from_name }]
  }
  const subjectLower = (thread.subject || '').toLowerCase()
  const subject = subjectLower.startsWith('re:') ? thread.subject : `Re: ${thread.subject}`
  const trimmed = textBody.trim()
  return {
    mailbox_id: thread.mailbox_id,
    thread_id: thread.id,
    reply_to_message_id: lastMessage.internet_message_id || null,
    subject,
    to_recipients,
    cc_recipients: [],
    bcc_recipients: [],
    text_body: trimmed ? trimmed : null,
    html_body: null,
  }
}

function mapAgentEmailAccent(value: string, category: 'agent' | 'inbox' | 'domain' | 'approval' | 'thread'): AccentTone {
  const normalized = value.toLowerCase()
  if (category === 'approval') {
    if (normalized.includes('pending')) return 'gold'
    if (normalized.includes('reject') || normalized.includes('error')) return 'rose'
    if (normalized.includes('approve') || normalized.includes('ready')) return 'mint'
    return 'azure'
  }
  if (category === 'thread') {
    if (normalized.includes('escalat')) return 'rose'
    if (normalized.includes('reply') || normalized.includes('unread')) return 'gold'
    if (normalized.includes('wait')) return 'azure'
    return 'mint'
  }
  if (normalized.includes('active') || normalized.includes('healthy') || normalized.includes('verified') || normalized.includes('connected')) return 'mint'
  if (normalized.includes('pending') || normalized.includes('review') || normalized.includes('warm') || normalized.includes('queue')) return 'gold'
  if (normalized.includes('error') || normalized.includes('block') || normalized.includes('reject') || normalized.includes('attention')) return 'rose'
  if (normalized.includes('pilot') || normalized.includes('draft')) return 'azure'
  return category === 'agent' ? 'gold' : 'slate'
}

function formatNumberShort(value: number | null, unit = 'items'): string {
  if (value === null || !Number.isFinite(value)) return `— ${unit}`
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M ${unit}`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K ${unit}`
  return `${Math.round(value)} ${unit}`
}

function formatTokenLabel(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '—'
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M tokens`
  if (value >= 1000) return `${(value / 1000).toFixed(1)}K tokens`
  return `${Math.round(value)} tokens`
}

function pickTimestamp(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value !== 'string' || !value.trim()) return null
  const numeric = Number(value)
  if (Number.isFinite(numeric)) return numeric
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : null
}

function parseServiceStatus(value: unknown): ManageServiceDatum['status'] {
  const state = typeof value === 'string' ? value.toLowerCase() : ''
  if (!state) return 'idle'
  if (state === 'running' || state === 'active' || state === 'up') return 'live'
  if (state === 'down' || state === 'stopped' || state === 'inactive' || state === 'error') return 'down'
  return 'idle'
}

function normalizeServiceMemory(mem: unknown): string {
  if (typeof mem === 'string' && mem.trim()) return mem.trim()
  const inBytes = toNumber(mem)
  if (inBytes === null) return '—'
  return `${formatBytes(inBytes)}`
}

function parseLiveProviders(source: unknown): ManageProviderDatum[] {
  const top = toRecord(source)
  const list = top?.providers
  if (Array.isArray(list)) {
    if (list.length === 0) return []
    return list.slice(0, 5).map((record, index) => {
    const raw = toRecord(record)
    const name = pickString(raw, ['name', 'provider', 'model']) || `Provider ${index + 1}`
    const role = pickString(raw, ['role', 'type']) || 'External model'
    const cost = pickNumber(raw, ['cost', 'cost_usd', 'monthly_cost', 'spend'])
    const tokens = pickNumber(raw, ['tokens', 'token_count', 'usage']) || 0
    const pct = pickNumber(raw, ['percent', 'ratio', 'usage_percent', 'cost_percent']) || 0
    return {
      name,
      role,
      cost: cost === null ? '$0.00' : formatCurrency(cost, 'USD'),
      tokens: formatTokenLabel(tokens),
      pct: normalizePercent(pct),
      accent: MANAGE_PROVIDER_ACCENTS[index % MANAGE_PROVIDER_ACCENTS.length],
    }
    })
  }
  return MANAGE_PROVIDER_FALLBACK
}

function parseLiveUsage(source: unknown): ManageUsageDatum[] {
  const top = toRecord(source)
  const rowsField = top?.usage_by_feature
  if (Array.isArray(rowsField)) {
    if (rowsField.length === 0) return []
    return rowsField.slice(0, 8).map((record, index) => {
    const sourceRecord = toRecord(record)
    const calls = pickNumber(sourceRecord, ['count', 'calls', 'requests']) || 0
    const pct = pickNumber(sourceRecord, ['percent', 'share', 'ratio']) || 0
    const label = pickString(sourceRecord, ['label', 'feature', 'name']) || `Feature ${index + 1}`
    return {
      label,
      calls: formatNumberShort(calls, 'calls'),
      pct: normalizePercent(pct),
      accent: manageUsageAccentForLabel(label, index),
    }
    })
  }
  return MANAGE_USAGE_FALLBACK
}

function manageUsageAccentForLabel(label: string, index: number): AccentTone {
  const normalized = label.trim().toLowerCase()
  if (normalized.includes('heartbeat')) return 'rose'
  if (normalized.includes('research') || normalized.includes('search')) return 'gold'
  if (normalized.includes('memory')) return 'mint'
  if (normalized.includes('routing')) return 'slate'
  if (normalized.includes('gateway')) return 'slate'
  if (normalized.includes('scheduling') || normalized.includes('cron') || normalized.includes('reminder')) return 'mint'
  return MANAGE_PROVIDER_ACCENTS[index % MANAGE_PROVIDER_ACCENTS.length]
}

function parseLiveServices(source: unknown): ManageServiceDatum[] {
  const rows = toArray(toRecord(source)?.services || source)
  if (!rows.length) return MANAGE_SERVICES_FALLBACK
  return rows.slice(0, 6).map((record, index) => {
    const sourceRecord = toRecord(record)
    return {
      name: pickString(sourceRecord, ['name', 'service', 'id']) || `Service ${index + 1}`,
      status: parseServiceStatus(pickString(sourceRecord, ['status', 'state'])),
      mem: normalizeServiceMemory(
        pickString(sourceRecord, ['memory_label', 'summary', 'meta']) ??
        pickNumber(sourceRecord, ['memory_bytes', 'memory', 'ram_bytes']) ??
        toRecord(sourceRecord)?.memory_bytes ??
        '',
      ),
    }
  })
}

function secondsToLabel(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return '—'
  const rounded = Math.max(0, Math.round(seconds))
  const days = Math.floor(rounded / 86_400)
  const hours = Math.floor((rounded % 86_400) / 3600)
  const mins = Math.floor((rounded % 3600) / 60)
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${mins}m`
  return `${mins}m`
}

function buildManageSnapshot(metrics: GatewaySystemMetrics | null) {
  if (!metrics) {
    return {
      instanceTitle: 'VM metrics unavailable',
      instanceMeta: 'Connect gateway to view live hardware metrics.',
      cycleLabel: '—',
      region: '—',
      os: '—',
      uptime: '—',
      cpuPercent: 0,
      memoryPercent: 0,
      diskPercent: 0,
      networkThroughput: '—',
      memoryUsedText: '—',
      memoryTotalText: '—',
      diskUsedText: '—',
      diskTotalText: '—',
      budgetUsed: 20.35,
      budgetTotal: 50,
      budgetCurrency: 'USD',
      sourceEndpoint: 'not-configured',
      providers: MANAGE_PROVIDER_FALLBACK,
      usage: MANAGE_USAGE_FALLBACK,
      services: MANAGE_SERVICES_FALLBACK,
      hasLiveData: false,
    }
  }

  const system = toRecord(metrics.system) || {}
  const budget = toRecord(metrics.budget) || toRecord(metrics.cost) || toRecord(metrics.ledger) || {}
  const instance = toRecord(metrics.instance) || toRecord(metrics.vm) || {}
  const cpu = toRecord(metrics.cpu) || toRecord(system.cpu) || {}
  const memory = toRecord(metrics.memory) || toRecord(system.memory) || {}
  const disk = toRecord(metrics.disk) || toRecord(system.disk) || {}
  const network = toRecord(metrics.network) || toRecord(system.network) || {}
  const runtimeSeconds = pickNumber(metrics, ['uptime_seconds', 'uptimeSeconds']) || pickNumber(system, ['uptime_seconds', 'uptimeSeconds'])

  const budgetUsed = pickNumber(budget, ['used_usd', 'used', 'cost_total', 'spent']) || 0
  const budgetTotal = pickNumber(budget, ['limit_usd', 'total_budget', 'budget', 'allocated']) || 50
  const budgetCurrency = pickString(budget, ['currency']) || 'USD'

  const cpuUsed = pickNumber(cpu, ['percent', 'cpu_percent', 'usage']) || 0
  const memUsed = pickNumber(memory, ['used', 'used_bytes', 'memory_used']) || 0
  const memTotal = pickNumber(memory, ['total', 'total_bytes', 'memory_total']) || 0
  const memPct = memTotal > 0 ? normalizePercent((memUsed / memTotal) * 100) : normalizePercent(pickNumber(memory, ['percent', 'usage_percent']))
  const diskUsed = pickNumber(disk, ['used', 'used_bytes']) || 0
  const diskTotal = pickNumber(disk, ['total', 'total_bytes']) || 0
  const diskPct = diskTotal > 0 ? normalizePercent((diskUsed / diskTotal) * 100) : normalizePercent(pickNumber(disk, ['percent', 'usage_percent']))
  const netInput = pickNumber(network, ['throughput_mbps', 'mbps', 'rx_mbps', 'inbound_mbps'])
  const netOutput = pickNumber(network, ['tx_mbps', 'egress_mbps'])
  const networkThroughput = netInput === null && netOutput === null
    ? '—'
    : `${netInput !== null ? `${netInput.toFixed(1)} in` : '--'} / ${netOutput !== null ? `${netOutput.toFixed(1)} out` : '--'} Mbps`

  return {
    instanceTitle: `${pickString(instance, ['name', 'id']) || 'Unknown instance'} · ${pickString(instance, ['type', 'flavor']) || 'Unknown type'}`,
    instanceMeta: `${pickString(instance, ['region', 'zone']) || 'Unknown region'} · ${pickString(instance, ['provider', 'platform', 'cloud']) || 'Unknown provider'}`,
    cycleLabel: `${pickString(budget, ['period', 'cycle']) || 'Current cycle'}`,
    region: pickString(instance, ['region', 'zone']) || 'Unknown region',
    os: pickString(instance, ['os', 'image', 'imageName']) || 'Linux',
    uptime: secondsToLabel(runtimeSeconds),
    cpuPercent: normalizePercent(cpuUsed),
    memoryPercent: memPct,
    diskPercent: diskPct,
    networkThroughput,
    memoryUsedText: formatBytes(memUsed),
    memoryTotalText: memTotal > 0 ? formatBytes(memTotal) : '—',
    diskUsedText: formatBytes(diskUsed),
    diskTotalText: diskTotal > 0 ? formatBytes(diskTotal) : '—',
    budgetUsed,
    budgetTotal,
    budgetCurrency,
    sourceEndpoint: metrics.sourceEndpoint || metrics.source || 'gateway-metrics',
    providers: parseLiveProviders(toRecord(metrics) ?? {}),
    usage: parseLiveUsage(toRecord(metrics) ?? {}),
    services: parseLiveServices(toRecord(metrics) ?? {}),
    hasLiveData: true,
  }
}

const PROPHET_SECTION_LABELS: Record<ProphetSection, string> = {
  breaking: 'Breaking Dispatch',
  tech: 'Technology & Innovation',
  markets: 'Markets & Industry',
  social: 'Social Feed',
  science: 'Science & Discovery',
}

const PROPHET_ARTICLES: ProphetArticle[] = [
  {
    id: 'p1',
    section: 'breaking',
    headline: 'Anthropic Unveils Claude 4.5 Opus With Autonomous Agent Capabilities',
    summary: 'The latest model demonstrates sustained reasoning over multi-hour tasks, marking a significant leap in AI-assisted software engineering and research workflows.',
    source: 'The Verge',
    timeAgo: '23m ago',
    accent: 'rose',
    featured: true,
  },
  {
    id: 'p2',
    section: 'breaking',
    headline: 'EU Parliament Passes Landmark AI Governance Framework',
    summary: 'New regulations establish tiered oversight for foundation models, with stricter requirements for systems capable of autonomous action.',
    source: 'Reuters',
    timeAgo: '1h ago',
    accent: 'azure',
  },
  {
    id: 'p3',
    section: 'tech',
    headline: 'Apple Previews On-Device LLM Integration Across iOS 20',
    summary: 'Siri gains persistent memory and tool-use capabilities, running a distilled model entirely on the Neural Engine.',
    source: 'Bloomberg',
    timeAgo: '2h ago',
    accent: 'slate',
  },
  {
    id: 'p4',
    section: 'tech',
    headline: 'WebGPU Adoption Crosses 80% of Desktop Browsers',
    summary: 'The milestone enables a new class of in-browser ML inference and real-time 3D applications without plugins.',
    source: 'Chrome Blog',
    timeAgo: '3h ago',
    accent: 'mint',
  },
  {
    id: 'p5',
    section: 'tech',
    headline: 'GitHub Copilot Workspace Enters General Availability',
    summary: 'Full-repository reasoning and multi-file editing are now accessible to all Teams and Enterprise subscribers.',
    source: 'GitHub',
    timeAgo: '4h ago',
    accent: 'azure',
  },
  {
    id: 'p6',
    section: 'markets',
    headline: 'NVIDIA Surpasses $5T Market Cap on Data Centre Demand',
    summary: 'The chipmaker\u2019s Blackwell Ultra architecture drives another record quarter as sovereign AI spending accelerates globally.',
    source: 'Financial Times',
    timeAgo: '2h ago',
    accent: 'gold',
  },
  {
    id: 'p7',
    section: 'markets',
    headline: 'YC W26 Batch Shows Record AI-Native Startup Density',
    summary: 'Over 70% of the cohort is building on top of foundation model APIs, with developer tools and vertical agents dominating.',
    source: 'TechCrunch',
    timeAgo: '5h ago',
    accent: 'mint',
  },
  {
    id: 'p8',
    section: 'social',
    headline: 'Your X Timeline: AI Discourse Peaks After Claude 4.5 Launch',
    summary: 'Trending threads debate autonomous coding assistants, with engineers sharing benchmark comparisons and workflow integrations.',
    source: 'X / Twitter',
    timeAgo: '45m ago',
    accent: 'azure',
  },
  {
    id: 'p9',
    section: 'social',
    headline: 'Hacker News Front Page: Show HN \u2014 Open-Source Agent Framework',
    summary: 'A community-built orchestration layer for multi-model agent pipelines gains 400+ points overnight.',
    source: 'Hacker News',
    timeAgo: '3h ago',
    accent: 'gold',
  },
  {
    id: 'p10',
    section: 'science',
    headline: 'DeepMind Solves New Class of Partial Differential Equations',
    summary: 'AlphaFold\u2019s successor architecture generalises to fluid dynamics and climate modelling, accelerating simulation by three orders of magnitude.',
    source: 'Nature',
    timeAgo: '6h ago',
    accent: 'mint',
  },
  {
    id: 'p11',
    section: 'science',
    headline: 'JWST Confirms New Biosignature in Trappist-1e Atmosphere',
    summary: 'Spectral analysis reveals phosphine alongside methane, reigniting debate about potential biological sources on rocky exoplanets.',
    source: 'NASA',
    timeAgo: '8h ago',
    accent: 'rose',
  },
]

const SPACE_PAGES: SpacePageDef[] = [
  { id: 'command', label: 'Command', kicker: 'Live operating picture', countLabel: '03 zones', accent: 'azure' },
  { id: 'tools', label: 'My Tools', kicker: 'Sites, dashboards & utilities', countLabel: 'Build', accent: 'azure' },
  { id: 'calendar', label: 'My Calendar', kicker: 'Your schedule at a glance', countLabel: '07 days', accent: 'gold' },
  { id: 'prophet', label: 'My Prophet', kicker: 'Your curated daily edition', countLabel: 'Live', accent: 'rose' },
  { id: 'autopilot', label: 'Autopilot', kicker: 'Autonomous routines', countLabel: '04 routines', accent: 'mint' },
  { id: 'pulse', label: 'Pulse', kicker: 'Health and usage', countLabel: '04 signals', accent: 'rose' },
  { id: 'manage', label: 'Manage', kicker: 'Resources & billing', countLabel: 'Live', accent: 'slate' },
  { id: 'agents', label: 'Agents', kicker: 'Registered specialists', countLabel: 'Registry', accent: 'azure' },
  { id: 'sessions', label: 'Sessions', kicker: 'Daily memory lanes', countLabel: 'Archive', accent: 'mint' },
  { id: 'agent-email', label: 'Agent Email', kicker: 'Mail control for agents', countLabel: 'Mail ops', accent: 'gold' },
  { id: 'gmail', label: 'Gmail', kicker: 'User inbox approvals', countLabel: 'Review', accent: 'mint' },
]

const CALENDAR_WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

function addDays(base: Date, days: number): Date {
  const next = new Date(base)
  next.setDate(next.getDate() + days)
  return next
}

function buildCalendarMonthCells(anchor: Date, markers: Set<number>): CalendarMonthCell[] {
  const year = anchor.getFullYear()
  const month = anchor.getMonth()
  const firstWeekday = new Date(year, month, 1).getDay()
  const daysInMonth = new Date(year, month + 1, 0).getDate()
  const previousMonthDays = new Date(year, month, 0).getDate()
  const cells: CalendarMonthCell[] = []

  for (let index = 0; index < 42; index += 1) {
    const currentDay = index - firstWeekday + 1

    if (currentDay < 1) {
      const d = previousMonthDays + currentDay
      cells.push({
        key: `prev-${index}`,
        label: String(d),
        muted: true,
        isToday: false,
        hasEvent: false,
        date: new Date(year, month - 1, d),
      })
      continue
    }

    if (currentDay > daysInMonth) {
      const d = currentDay - daysInMonth
      cells.push({
        key: `next-${index}`,
        label: String(d),
        muted: true,
        isToday: false,
        hasEvent: false,
        date: new Date(year, month + 1, d),
      })
      continue
    }

    cells.push({
      key: `current-${currentDay}`,
      label: String(currentDay),
      muted: false,
      isToday: currentDay === anchor.getDate(),
      hasEvent: markers.has(currentDay),
      date: new Date(year, month, currentDay),
    })
  }

  return cells
}

const CAL_HOUR_HEIGHT = 48
const CAL_FIRST_HOUR = 6
const CAL_LAST_HOUR = 22
const CAL_REFRESH_MS = 5 * 60 * 1000
const CAL_STALE_AFTER_MS = 2 * 60 * 1000

function googleColorToAccent(colorId: string, calendarColor: string): AccentTone {
  const src = colorId || calendarColor
  if (!src) return 'azure'
  const id = src.toLowerCase()
  if (id === '1' || id.includes('lavender')) return 'slate'
  if (id === '2' || id.includes('sage')) return 'mint'
  if (id === '3' || id.includes('grape')) return 'slate'
  if (id === '4' || id.includes('flamingo') || id.includes('tomato')) return 'rose'
  if (id === '5' || id.includes('banana')) return 'gold'
  if (id === '6' || id.includes('tangerine')) return 'gold'
  if (id === '7' || id.includes('peacock') || id.includes('teal')) return 'mint'
  if (id === '8' || id.includes('graphite') || id.includes('grey')) return 'slate'
  if (id === '9' || id.includes('blueberry') || id.includes('blue')) return 'azure'
  if (id === '10' || id.includes('basil') || id.includes('green')) return 'mint'
  if (id === '11' || id.includes('tomato') || id.includes('red')) return 'rose'
  if (id.includes('#')) {
    const hex = id.replace('#', '')
    const r = parseInt(hex.slice(0, 2), 16)
    const g = parseInt(hex.slice(2, 4), 16)
    const b = parseInt(hex.slice(4, 6), 16)
    if (r > g && r > b) return 'rose'
    if (g > r && g > b) return 'mint'
    if (b > r && b > g) return 'azure'
    return 'slate'
  }
  return 'azure'
}

function getEventDurationLabel(event: CalendarAgendaEvent): string {
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

function agendaEventToWeekEvent(event: CalendarAgendaEvent, weekStart: Date): WeekEvent | null {
  if (event.isAllDay) return null
  const start = getCalendarEventStart(event)
  const end = getCalendarEventEnd(event)
  if (!start || !end) return null
  const weekEnd = new Date(weekStart)
  weekEnd.setDate(weekStart.getDate() + 7)
  if (start < weekStart || start >= weekEnd) return null
  const startHour = start.getHours()
  const startMinute = start.getMinutes()
  const durationMinutes = Math.max(15, Math.round((end.getTime() - start.getTime()) / 60000))
  return {
    id: event.id,
    title: event.summary,
    dayIndex: start.getDay(),
    startHour,
    startMinute,
    durationMinutes,
    accent: googleColorToAccent(event.colorId, event.calendar_color),
  }
}

function formatHour(hour: number): string {
  if (hour === 0) return '12 AM'
  if (hour < 12) return `${hour} AM`
  if (hour === 12) return '12 PM'
  return `${hour - 12} PM`
}

function normalizeGatewayState(state: string): { label: string; tone: 'good' | 'warm' | 'muted' } {
  if (state === 'connected') {
    return { label: 'Gateway live', tone: 'good' }
  }
  if (state === 'connecting' || state === 'reconnecting') {
    return { label: 'Re-linking', tone: 'warm' }
  }
  if (state === 'error') {
    return { label: 'Needs attention', tone: 'warm' }
  }
  return { label: 'Idle', tone: 'muted' }
}

function SpacesNavIcon({ page }: { page: SpacesPageId }) {
  if (page === 'command') {
    return (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="8.25" />
        <circle cx="12" cy="12" r="3" />
        <path d="M12 3.75v2" />
        <path d="M12 18.25v2" />
        <path d="M3.75 12h2" />
        <path d="M18.25 12h2" />
      </svg>
    )
  }
  if (page === 'tools') {
    return (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <path d="M5 6.5h14" />
        <path d="M7.5 4v5" />
        <path d="M16.5 4v5" />
        <rect x="4.5" y="6.5" width="15" height="13" rx="2.5" />
        <path d="M8 12h3.5" />
        <path d="M8 15.5h7.5" />
      </svg>
    )
  }
  if (page === 'calendar') {
    return (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <rect x="4.5" y="5" width="15" height="14" rx="2.5" />
        <path d="M8 3v4" />
        <path d="M16 3v4" />
        <path d="M4.5 9.5h15" />
        <path d="M8.5 13h2.5" />
        <path d="M13 13h2.5" />
        <path d="M8.5 16.5h2.5" />
      </svg>
    )
  }
  if (page === 'prophet') {
    return (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <rect x="4" y="4" width="16" height="16" rx="2" />
        <path d="M4 9h16" />
        <path d="M12 9v11" />
        <path d="M7.5 12.5h2" />
        <path d="M7.5 15h2" />
        <path d="M14.5 12.5h2" />
        <path d="M14.5 15h2" />
        <path d="M8 6.5h8" />
      </svg>
    )
  }
  if (page === 'autopilot') {
    return (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <path d="M17.5 6.5h-11a3 3 0 0 0-3 3v1" />
        <path d="M6.5 17.5h11a3 3 0 0 0 3-3v-1" />
        <path d="M14.5 3.5l3 3-3 3" />
        <path d="M9.5 20.5l-3-3 3-3" />
      </svg>
    )
  }
  if (page === 'pulse') {
    return (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3.5 12h3.5l2-5.5 3 11 2.5-5.5h6" />
      </svg>
    )
  }
  if (page === 'manage') {
    return (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <rect x="4" y="4" width="16" height="16" rx="2.5" />
        <path d="M4 9h16" />
        <path d="M9 9v11" />
        <circle cx="6.5" cy="6.5" r="0.75" fill="currentColor" stroke="none" />
        <circle cx="9" cy="6.5" r="0.75" fill="currentColor" stroke="none" />
        <circle cx="11.5" cy="6.5" r="0.75" fill="currentColor" stroke="none" />
        <path d="M12 14h5" />
        <path d="M12 17h3.5" />
      </svg>
    )
  }
  if (page === 'agents') {
    return (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="7" r="2.75" />
        <circle cx="7" cy="16.5" r="2.75" />
        <circle cx="17" cy="16.5" r="2.75" />
        <path d="M12 9.75v2.2" />
        <path d="M9.2 14.6l-1.1 1" />
        <path d="M14.8 14.6l1.1 1" />
      </svg>
    )
  }
  if (page === 'sessions') {
    return (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <rect x="5" y="4" width="14" height="16" rx="2.25" />
        <path d="M8.5 9h7.5" />
        <path d="M8.5 12.25h5.5" />
        <path d="M8.5 15.5h7" />
      </svg>
    )
  }
  if (page === 'agent-email') {
    return (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <rect x="4" y="6" width="16" height="12" rx="2.5" />
        <path d="m5.5 8.25 6.5 5 6.5-5" />
        <path d="M8 10.5h8" opacity="0.42" />
      </svg>
    )
  }
  if (page === 'gmail') {
    return (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4.5 7.5h15v9a2 2 0 0 1-2 2h-11a2 2 0 0 1-2-2v-9Z" />
        <path d="m5.25 8.25 6.75 5 6.75-5" />
        <path d="M8.75 16.25h6.5" />
      </svg>
    )
  }
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="7.5" />
      <path d="M12 8.25v4.25l2.75 1.75" />
    </svg>
  )
}

function SpacesActionIcon({ kind }: { kind: 'chat' | 'minimize' | 'close' }) {
  if (kind === 'chat') {
    return (
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M6.25 6.75h11.5A1.75 1.75 0 0 1 19.5 8.5v6a1.75 1.75 0 0 1-1.75 1.75H11l-3.9 3.08A.65.65 0 0 1 6 18.82v-2.57A1.75 1.75 0 0 1 4.5 14.5v-6A1.75 1.75 0 0 1 6.25 6.75Z" />
        <path d="M9 11.5h6" />
        <path d="M9 14h3.5" />
      </svg>
    )
  }
  if (kind === 'minimize') {
    return (
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
        <path d="M6 12h12" />
      </svg>
    )
  }
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M7 7 17 17" />
      <path d="M17 7 7 17" />
    </svg>
  )
}

export default function SpacesControlCenter({
  active,
  gatewayState,
  gatewayConnected,
  gatewayDetail,
  pendingTaskCount,
  pendingCronCount,
  selectedModelLabel,
  onBackToChat,
  onPromptChat,
  onMinimize,
  onClose,
  onShowTooltip,
  onHideTooltip,
  containerRef,
  containerClassName,
  containerStyle,
  agentEmailNavigateInboxSignal = 0,
  agentEmailNavigateInboxMailboxId = null,
  agentEmailNavigateApprovalsSignal = 0,
  agentEmailNavigateApprovalsId = null,
}: SpacesControlCenterProps) {
  const [page, setPage] = useState<SpacesPageId>('command')
  const [railCollapsed, setRailCollapsed] = useState(false)
  const [agentEmailView, setAgentEmailView] = useState<AgentEmailViewId>('overview')
  const [agentEmailSelectedAgentId, setAgentEmailSelectedAgentId] = useState('')
  const [agentEmailSelectedInboxId, setAgentEmailSelectedInboxId] = useState('')
  const [agentEmailSelectedDomainId, setAgentEmailSelectedDomainId] = useState('')
  const [agentEmailSelectedApprovalId, setAgentEmailSelectedApprovalId] = useState('')
  const [agentEmailSelectedThreadId, setAgentEmailSelectedThreadId] = useState('')
  const [agentEmailSettingsSection, setAgentEmailSettingsSection] = useState<'connection' | 'trusted-senders' | 'domains' | 'inboxes' | 'agents'>('connection')

  /* ── Live calendar state ─────────────────────────────── */
  const [calendarData, setCalendarData] = useState<CalendarAgendaSnapshot>(EMPTY_CALENDAR_AGENDA)
  const [calendarRefreshing, setCalendarRefreshing] = useState(false)
  const [calWeekOffset, setCalWeekOffset] = useState(0)
  const [calView, setCalView] = useState<'day' | 'week'>('week')
  const [calDayOffset, setCalDayOffset] = useState(0) // day offset for day view
  const [selectedCalEvent, setSelectedCalEvent] = useState<CalendarAgendaEvent | null>(null)
  const [now, setNow] = useState(() => new Date())
  const [manageMetrics, setManageMetrics] = useState<GatewaySystemMetrics | null>(null)
  const [manageMetricsRefreshing, setManageMetricsRefreshing] = useState(false)
  const [manageMetricsError, setManageMetricsError] = useState<string | null>(null)
  const [manageLastUpdatedAt, setManageLastUpdatedAt] = useState<number | null>(null)
  const manageMetricsRefreshRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const manageMetricsIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const manageMetricsValueRef = useRef<GatewaySystemMetrics | null>(null)
  const manageLastUpdatedAtRef = useRef<number | null>(null)
  const manageMetricsRequestRef = useRef(0)
  const [manageUsageMode, setManageUsageMode] = useState<ManageUsageMode>('30d')
  const [manageUsageCustomStart, setManageUsageCustomStart] = useState('')
  const [manageUsageCustomEnd, setManageUsageCustomEnd] = useState('')
  const manageUsageAppliedOptionsRef = useRef<ManageUsagePeriodPayload>({ usage_days: 30 })

  const [registryAgents, setRegistryAgents] = useState<RegistryAgentRow[]>([])
  const [registryAgentsError, setRegistryAgentsError] = useState<string | null>(null)
  const [registryAgentsRefreshing, setRegistryAgentsRefreshing] = useState(false)
  const [registryAgentsFetchedAt, setRegistryAgentsFetchedAt] = useState<number | null>(null)
  const registryAgentsRequestRef = useRef(0)
  const registryAgentsIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const [sessionsList, setSessionsList] = useState<GatewaySessionRow[]>([])
  const [sessionsError, setSessionsError] = useState<string | null>(null)
  const [sessionsRefreshing, setSessionsRefreshing] = useState(false)
  const [sessionsFetchedAt, setSessionsFetchedAt] = useState<number | null>(null)
  const [selectedSessionId, setSelectedSessionId] = useState('')
  const [selectedSessionMessages, setSelectedSessionMessages] = useState<GatewaySessionHistoryMessage[]>([])
  const [selectedSessionRequestTraces, setSelectedSessionRequestTraces] = useState<GatewayRequestTrace[]>([])
  const [selectedSessionLoading, setSelectedSessionLoading] = useState(false)
  const [selectedSessionError, setSelectedSessionError] = useState<string | null>(null)
  const [selectedSessionFetchedAt, setSelectedSessionFetchedAt] = useState<number | null>(null)
  const [selectedSessionTracesLoading, setSelectedSessionTracesLoading] = useState(false)
  const [selectedSessionTraceLoadedForId, setSelectedSessionTraceLoadedForId] = useState<string | null>(null)
  const [selectedSessionTraceError, setSelectedSessionTraceError] = useState<string | null>(null)
  const [sessionsListCollapsed, setSessionsListCollapsed] = useState(false)
  const [sessionsDiagnosticsOpen, setSessionsDiagnosticsOpen] = useState(false)
  const [sessionsJumpToBottomVisible, setSessionsJumpToBottomVisible] = useState(false)
  const sessionsDetailScrollRef = useRef<HTMLDivElement>(null)
  const sessionsDetailAnchorRef = useRef<HTMLDivElement>(null)
  const sessionsRequestRef = useRef(0)
  const sessionsDetailRequestRef = useRef(0)
  const sessionsTraceRequestRef = useRef(0)
  const sessionsIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const [toolOpportunities, setToolOpportunities] = useState<ToolOpportunity[]>([])
  const [toolOpportunitiesRefreshing, setToolOpportunitiesRefreshing] = useState(false)
  const [toolOpportunitiesError, setToolOpportunitiesError] = useState<string | null>(null)
  const [toolOpportunityActionId, setToolOpportunityActionId] = useState<string | null>(null)

  const refreshToolOpportunities = useCallback(async () => {
    if (!window.cosmic?.listGatewayToolOpportunities) {
      setToolOpportunitiesError('My Tools is unavailable in this desktop build.')
      return
    }
    setToolOpportunitiesRefreshing(true)
    try {
      const result = await window.cosmic.listGatewayToolOpportunities()
      setToolOpportunities(Array.isArray(result?.items) ? result.items as ToolOpportunity[] : [])
      setToolOpportunitiesError(null)
    } catch (error) {
      setToolOpportunitiesError(toErrorMessage(error))
    } finally {
      setToolOpportunitiesRefreshing(false)
    }
  }, [])

  useEffect(() => {
    if (!active || page !== 'tools') return
    void refreshToolOpportunities()
  }, [active, page, refreshToolOpportunities])

  const buildToolOpportunity = useCallback(async (opportunityId: string) => {
    if (!window.cosmic?.buildGatewayToolOpportunity) return
    setToolOpportunityActionId(opportunityId)
    try {
      const result = await window.cosmic.buildGatewayToolOpportunity(opportunityId)
      await refreshToolOpportunities()
      if (String(result?.prompt || '').trim()) onPromptChat(String(result.prompt))
    } catch (error) {
      setToolOpportunitiesError(toErrorMessage(error))
    } finally {
      setToolOpportunityActionId(null)
    }
  }, [onPromptChat, refreshToolOpportunities])

  const updateToolOpportunityStatus = useCallback(async (opportunityId: string, status: string) => {
    if (!window.cosmic?.updateGatewayToolOpportunity) return
    setToolOpportunityActionId(opportunityId)
    try {
      await window.cosmic.updateGatewayToolOpportunity({ opportunityId, changes: { status } })
      await refreshToolOpportunities()
    } catch (error) {
      setToolOpportunitiesError(toErrorMessage(error))
    } finally {
      setToolOpportunityActionId(null)
    }
  }, [refreshToolOpportunities])

  useEffect(() => {
    manageMetricsValueRef.current = manageMetrics
  }, [manageMetrics])

  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 60000)
    return () => clearInterval(timer)
  }, [])
  const calendarRefreshTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const calendarGeneratedAtRef = useRef(0)

  const requestCalendarAgenda = useCallback((showSpinner = false) => {
    if (!window.cosmic?.getCalendarAgenda) return
    if (showSpinner) setCalendarRefreshing(true)
    if (calendarRefreshTimeoutRef.current) clearTimeout(calendarRefreshTimeoutRef.current)
    calendarRefreshTimeoutRef.current = setTimeout(() => {
      calendarRefreshTimeoutRef.current = null
      setCalendarRefreshing(false)
    }, 8000)
    window.cosmic.getCalendarAgenda()
  }, [])

  useEffect(() => {
    if (!active || page !== 'calendar') {
      return
    }
    if (!window.cosmic?.onCalendarAgendaUpdate) return
    const offAgenda = window.cosmic.onCalendarAgendaUpdate((snapshot: unknown) => {
      const normalized = normalizeCalendarAgendaSnapshot(snapshot as Record<string, unknown>)
      calendarGeneratedAtRef.current = normalized.generated_at
      setCalendarData(normalized)
      if (calendarRefreshTimeoutRef.current) {
        clearTimeout(calendarRefreshTimeoutRef.current)
        calendarRefreshTimeoutRef.current = null
      }
      setCalendarRefreshing(false)
    })
    const offShown = window.cosmic.onShown?.(() => {
      const lastMs = calendarGeneratedAtRef.current * 1000
      if (!lastMs || Date.now() - lastMs > CAL_STALE_AFTER_MS) requestCalendarAgenda(false)
    })
    const offIntegration = window.cosmic.onIntegrationEvent?.((event: { provider: string; type: string }) => {
      if (event.provider !== 'google') return
      if (event.type === 'auth_success' || event.type === 'disconnect_success') requestCalendarAgenda(true)
    })
    requestCalendarAgenda(true)
    const intervalId = window.setInterval(() => requestCalendarAgenda(false), CAL_REFRESH_MS)
    return () => {
      offAgenda?.()
      offShown?.()
      offIntegration?.()
      window.clearInterval(intervalId)
      if (calendarRefreshTimeoutRef.current) {
        clearTimeout(calendarRefreshTimeoutRef.current)
        calendarRefreshTimeoutRef.current = null
      }
      setCalendarRefreshing(false)
    }
  }, [active, page, requestCalendarAgenda])

  const requestManageMetrics = useCallback(async (
    showSpinner = false,
    forceRefresh = false,
    usageModeOverride?: ManageUsageMode,
  ) => {
    if (!gatewayConnected) {
      setManageMetrics(null)
      setManageLastUpdatedAt(null)
      manageLastUpdatedAtRef.current = null
      setManageMetricsError(String(gatewayDetail || 'The desktop app is not connected to your VM yet.'))
      if (window.cosmic?.requestGatewayResume) {
        window.cosmic.requestGatewayResume().catch(() => { })
      }
      return
    }
    if (!window.cosmic?.getGatewaySystemMetrics) {
      setManageMetricsError('Gateway transport bridge is unavailable.')
      return
    }

    const requestId = ++manageMetricsRequestRef.current
    if (showSpinner) setManageMetricsRefreshing(true)
    if (manageMetricsRefreshRef.current) {
      clearTimeout(manageMetricsRefreshRef.current)
      manageMetricsRefreshRef.current = null
    }
    manageMetricsRefreshRef.current = setTimeout(() => {
      if (requestId === manageMetricsRequestRef.current) {
        setManageMetricsRefreshing(false)
      }
    }, MANAGE_REFRESH_TIMEOUT_MS)

    const mode = usageModeOverride ?? manageUsageMode
    const usageOpts = manageUsageOptionsForMode(
      mode,
      manageUsageCustomStart,
      manageUsageCustomEnd,
      manageUsageAppliedOptionsRef.current,
    )

    try {
      const snapshot = await window.cosmic.getGatewaySystemMetrics({
        forceRefresh,
        usage: usageOpts,
      })
      if (requestId !== manageMetricsRequestRef.current) {
        return
      }
      setManageMetrics(snapshot ? (snapshot as GatewaySystemMetrics) : null)
      setManageMetricsError(null)
      manageUsageAppliedOptionsRef.current = usageOpts
      const snapshotRecord = toRecord(snapshot)
      const updatedAt =
        pickTimestamp(snapshotRecord?.fetchedAt) ??
        pickTimestamp(snapshotRecord?.fetched_at) ??
        pickTimestamp(snapshotRecord?.timestamp) ??
        Date.now()
      setManageLastUpdatedAt(updatedAt)
      manageLastUpdatedAtRef.current = updatedAt
    } catch (error: unknown) {
      if (requestId !== manageMetricsRequestRef.current) {
        return
      }
      setManageMetricsError(toErrorMessage(error))
      if (!manageMetricsValueRef.current) {
        manageLastUpdatedAtRef.current = null
        setManageLastUpdatedAt(null)
      }
    } finally {
      if (requestId === manageMetricsRequestRef.current) {
        if (manageMetricsRefreshRef.current) {
          clearTimeout(manageMetricsRefreshRef.current)
          manageMetricsRefreshRef.current = null
        }
        setManageMetricsRefreshing(false)
      }
    }
  }, [
    gatewayConnected,
    gatewayDetail,
    manageUsageMode,
    manageUsageCustomStart,
    manageUsageCustomEnd,
  ])

  useEffect(() => {
    if (!active || page !== 'manage') return
    if (!document.hidden) {
      requestManageMetrics(true)
    }
    if (manageMetricsIntervalRef.current) {
      clearInterval(manageMetricsIntervalRef.current)
      manageMetricsIntervalRef.current = null
    }
    manageMetricsIntervalRef.current = setInterval(() => {
      if (document.hidden) return
      requestManageMetrics(false)
    }, MANAGE_REFRESH_MS)

    const offShown = window.cosmic?.onShown?.(() => {
      if (document.hidden) return
      if (Date.now() - (manageLastUpdatedAtRef.current || 0) > MANAGE_REFRESH_MS) {
        requestManageMetrics(false)
      }
    })
    const handleVisibilityChange = () => {
      if (document.hidden) return
      if (Date.now() - (manageLastUpdatedAtRef.current || 0) > MANAGE_REFRESH_MS) {
        requestManageMetrics(false)
      }
    }
    document.addEventListener('visibilitychange', handleVisibilityChange)

    return () => {
      if (manageMetricsIntervalRef.current) {
        clearInterval(manageMetricsIntervalRef.current)
        manageMetricsIntervalRef.current = null
      }
      if (manageMetricsRefreshRef.current) {
        clearTimeout(manageMetricsRefreshRef.current)
        manageMetricsRefreshRef.current = null
      }
      document.removeEventListener('visibilitychange', handleVisibilityChange)
      offShown?.()
    }
    }, [active, page, requestManageMetrics])

  const requestRegistryAgents = useCallback(async (showSpinner = false) => {
    if (!gatewayConnected) {
      setRegistryAgents([])
      setRegistryAgentsFetchedAt(null)
      setRegistryAgentsError(String(gatewayDetail || 'The desktop app is not connected to your VM yet.'))
      if (window.cosmic?.requestGatewayResume) {
        window.cosmic.requestGatewayResume().catch(() => { })
      }
      return
    }
    if (!window.cosmic?.getGatewayRegistryAgents) {
      setRegistryAgentsError('Gateway transport bridge is unavailable.')
      return
    }
    const requestId = ++registryAgentsRequestRef.current
    if (showSpinner) {
      setRegistryAgentsRefreshing(true)
    }
    try {
      const raw = await window.cosmic.getGatewayRegistryAgents()
      if (requestId !== registryAgentsRequestRef.current) {
        return
      }
      const normalized = normalizeRegistryAgentsPayload(raw)
      setRegistryAgents(normalized.agents)
      setRegistryAgentsFetchedAt(normalized.fetchedAtMs ?? Date.now())
      setRegistryAgentsError(null)
    } catch (error: unknown) {
      if (requestId !== registryAgentsRequestRef.current) {
        return
      }
      setRegistryAgentsError(toErrorMessage(error, 'Unable to load the specialist registry.'))
    } finally {
      if (requestId === registryAgentsRequestRef.current) {
        setRegistryAgentsRefreshing(false)
      }
    }
  }, [gatewayConnected, gatewayDetail])

  useEffect(() => {
    if (!active || page !== 'agents') {
      return
    }
    if (!document.hidden) {
      requestRegistryAgents(true)
    }
    if (registryAgentsIntervalRef.current) {
      clearInterval(registryAgentsIntervalRef.current)
      registryAgentsIntervalRef.current = null
    }
    registryAgentsIntervalRef.current = setInterval(() => {
      if (document.hidden) {
        return
      }
      requestRegistryAgents(false)
    }, SPACES_REGISTRY_REFRESH_MS)
    const offShown = window.cosmic?.onShown?.(() => {
      if (document.hidden) {
        return
      }
      requestRegistryAgents(false)
    })
    return () => {
      if (registryAgentsIntervalRef.current) {
        clearInterval(registryAgentsIntervalRef.current)
        registryAgentsIntervalRef.current = null
      }
      offShown?.()
    }
  }, [active, page, requestRegistryAgents])

  const requestSessionsList = useCallback(async (showSpinner = false) => {
    if (!gatewayConnected) {
      setSessionsList([])
      setSessionsFetchedAt(null)
      setSessionsError(String(gatewayDetail || 'The desktop app is not connected to your VM yet.'))
      if (window.cosmic?.requestGatewayResume) {
        window.cosmic.requestGatewayResume().catch(() => { })
      }
      return
    }
    if (!window.cosmic?.listGatewaySessions) {
      setSessionsError('Gateway transport bridge is unavailable.')
      return
    }
    const requestId = ++sessionsRequestRef.current
    if (showSpinner) {
      setSessionsRefreshing(true)
    }
    try {
      const raw = await window.cosmic.listGatewaySessions()
      if (requestId !== sessionsRequestRef.current) {
        return
      }
      const normalized = normalizeGatewaySessionsPayload(raw)
      setSessionsList(normalized)
      setSessionsFetchedAt(Date.now())
      setSessionsError(null)
      setSelectedSessionId((current) => {
        if (current && normalized.some((session) => session.id === current)) {
          return current
        }
        if (normalized.length === 0) {
          return ''
        }
        let latest = normalized[0]
        let latestMs = gatewaySessionRecencyMs(latest)
        for (let i = 1; i < normalized.length; i += 1) {
          const row = normalized[i]
          const ms = gatewaySessionRecencyMs(row)
          if (ms >= latestMs) {
            latest = row
            latestMs = ms
          }
        }
        return latest.id
      })
    } catch (error: unknown) {
      if (requestId !== sessionsRequestRef.current) {
        return
      }
      setSessionsError(toErrorMessage(error, 'Unable to load session history lanes.'))
    } finally {
      if (requestId === sessionsRequestRef.current) {
        setSessionsRefreshing(false)
      }
    }
  }, [gatewayConnected, gatewayDetail])

  const requestSelectedSessionHistory = useCallback(async (sessionId: string, showSpinner = false) => {
    const targetId = String(sessionId || '').trim()
    if (!targetId) {
      setSelectedSessionMessages([])
      setSelectedSessionFetchedAt(null)
      setSelectedSessionError(null)
      return
    }
    if (!gatewayConnected) {
      setSelectedSessionMessages([])
      setSelectedSessionFetchedAt(null)
      setSelectedSessionError(String(gatewayDetail || 'The desktop app is not connected to your VM yet.'))
      return
    }
    if (!window.cosmic?.getGatewaySessionHistory) {
      setSelectedSessionError('Gateway transport bridge is unavailable.')
      return
    }
    const requestId = ++sessionsDetailRequestRef.current
    if (showSpinner) {
      setSelectedSessionLoading(true)
    }
    try {
      const raw = await window.cosmic.getGatewaySessionHistory(targetId)
      if (requestId !== sessionsDetailRequestRef.current) {
        return
      }
      const normalized = normalizeGatewaySessionHistoryPayload(raw)
      setSelectedSessionMessages(normalized.messages)
      setSelectedSessionFetchedAt(Date.now())
      setSelectedSessionError(null)
    } catch (error: unknown) {
      if (requestId !== sessionsDetailRequestRef.current) {
        return
      }
      setSelectedSessionMessages([])
      setSelectedSessionFetchedAt(null)
      setSelectedSessionError(toErrorMessage(error, 'Unable to load this session.'))
    } finally {
      if (requestId === sessionsDetailRequestRef.current) {
        setSelectedSessionLoading(false)
      }
    }
  }, [gatewayConnected, gatewayDetail])

  const requestSelectedSessionRequestTraces = useCallback(async (sessionId: string) => {
    const targetId = String(sessionId || '').trim()
    if (!targetId) {
      setSelectedSessionRequestTraces([])
      setSelectedSessionTraceError(null)
      setSelectedSessionTraceLoadedForId(null)
      setSelectedSessionTracesLoading(false)
      return
    }
    if (!gatewayConnected) {
      setSelectedSessionRequestTraces([])
      setSelectedSessionTraceError(String(gatewayDetail || 'The desktop app is not connected to your VM yet.'))
      setSelectedSessionTraceLoadedForId(targetId)
      setSelectedSessionTracesLoading(false)
      return
    }
    if (!window.cosmic?.getGatewayRequestTraces) {
      setSelectedSessionTraceError('Gateway request trace bridge is unavailable.')
      setSelectedSessionTraceLoadedForId(targetId)
      setSelectedSessionTracesLoading(false)
      return
    }
    const requestId = ++sessionsTraceRequestRef.current
    setSelectedSessionTracesLoading(true)
    try {
      const raw = await window.cosmic.getGatewayRequestTraces(targetId)
      if (requestId !== sessionsTraceRequestRef.current) {
        return
      }
      const normalized = normalizeGatewayRequestTracePayload(raw)
      setSelectedSessionRequestTraces(normalized.requestTraces)
      setSelectedSessionTraceError(null)
      setSelectedSessionTraceLoadedForId(targetId)
    } catch (error: unknown) {
      if (requestId !== sessionsTraceRequestRef.current) {
        return
      }
      setSelectedSessionRequestTraces([])
      setSelectedSessionTraceError(toErrorMessage(error, 'Unable to load request traces for this session.'))
      setSelectedSessionTraceLoadedForId(targetId)
    } finally {
      if (requestId === sessionsTraceRequestRef.current) {
        setSelectedSessionTracesLoading(false)
      }
    }
  }, [gatewayConnected, gatewayDetail])

  useEffect(() => {
    if (!active || page !== 'sessions') {
      return
    }
    if (!document.hidden) {
      requestSessionsList(true)
    }
    if (sessionsIntervalRef.current) {
      clearInterval(sessionsIntervalRef.current)
      sessionsIntervalRef.current = null
    }
    sessionsIntervalRef.current = setInterval(() => {
      if (document.hidden) {
        return
      }
      requestSessionsList(false)
    }, SPACES_SESSIONS_REFRESH_MS)
    const offShown = window.cosmic?.onShown?.(() => {
      if (document.hidden) {
        return
      }
      requestSessionsList(false)
    })
    return () => {
      if (sessionsIntervalRef.current) {
        clearInterval(sessionsIntervalRef.current)
        sessionsIntervalRef.current = null
      }
      offShown?.()
    }
  }, [active, page, requestSessionsList])

  useEffect(() => {
    if (!active || page !== 'sessions') {
      setSessionsListCollapsed(false)
      setSessionsDiagnosticsOpen(false)
    }
  }, [active, page])

  useEffect(() => {
    if (!active || page !== 'sessions') {
      return
    }
    if (!selectedSessionId) {
      setSelectedSessionMessages([])
      setSelectedSessionRequestTraces([])
      setSelectedSessionFetchedAt(null)
      setSelectedSessionError(null)
      setSelectedSessionTraceError(null)
      setSelectedSessionTraceLoadedForId(null)
      setSelectedSessionTracesLoading(false)
      setSessionsDiagnosticsOpen(false)
      sessionsTraceRequestRef.current += 1
      return
    }
    setSessionsDiagnosticsOpen(false)
    setSelectedSessionRequestTraces([])
    setSelectedSessionTraceError(null)
    setSelectedSessionTraceLoadedForId(null)
    setSelectedSessionTracesLoading(false)
    sessionsTraceRequestRef.current += 1
    requestSelectedSessionHistory(selectedSessionId, true)
  }, [active, page, selectedSessionId, requestSelectedSessionHistory])

  useEffect(() => {
    if (!active || page !== 'sessions' || !selectedSessionId || !sessionsDiagnosticsOpen) {
      return
    }
    if (selectedSessionTracesLoading || selectedSessionTraceLoadedForId === selectedSessionId) {
      return
    }
    requestSelectedSessionRequestTraces(selectedSessionId)
  }, [
    active,
    page,
    selectedSessionId,
    sessionsDiagnosticsOpen,
    selectedSessionTraceLoadedForId,
    selectedSessionTracesLoading,
    requestSelectedSessionRequestTraces,
  ])

  const gatewayStatus = useMemo(() => normalizeGatewayState(gatewayState), [gatewayState])
  const today = useMemo(() => new Date(), [])
  const manageSnapshot = useMemo(() => buildManageSnapshot(manageMetrics), [manageMetrics])
  const manageUsageBounds = useMemo(() => {
    const usageBounds = toRecord(manageMetrics)?.usage_bounds
    const earliestCallAt = pickString(usageBounds || {}, ['earliest_call_at'])
    return {
      minYmd: utcMonthStartYmdFromIso(earliestCallAt),
      maxYmd: todayYmdLocal(),
    }
  }, [manageMetrics])

  useEffect(() => {
    if (manageUsageMode !== 'custom') return
    const defaultStart = defaultCustomUsageStartYmd(manageUsageBounds.minYmd, manageUsageBounds.maxYmd)
    const nextStart = clampYmdToBounds(
      manageUsageCustomStart || defaultStart,
      manageUsageBounds.minYmd,
      manageUsageBounds.maxYmd,
    )
    const nextEnd = clampYmdToBounds(
      manageUsageCustomEnd || manageUsageBounds.maxYmd,
      nextStart || manageUsageBounds.minYmd,
      manageUsageBounds.maxYmd,
    )
    if (nextStart && nextStart !== manageUsageCustomStart) {
      setManageUsageCustomStart(nextStart)
    }
    if (nextEnd && nextEnd !== manageUsageCustomEnd) {
      setManageUsageCustomEnd(nextEnd)
    }
  }, [
    manageUsageBounds.maxYmd,
    manageUsageBounds.minYmd,
    manageUsageCustomEnd,
    manageUsageCustomStart,
    manageUsageMode,
  ])

  const groupedSessions = useMemo(() => {
    const sorted = [...sessionsList].sort((a, b) => {
      const dayDelta = gatewaySessionGroupInfo(b).sortMs - gatewaySessionGroupInfo(a).sortMs
      if (dayDelta !== 0) return dayDelta
      return gatewaySessionRecencyMs(b) - gatewaySessionRecencyMs(a)
    })
    const groups: Array<{ key: string; label: string; sortMs: number; sessions: GatewaySessionRow[] }> = []
    const indexByKey = new Map<string, number>()
    for (const session of sorted) {
      const groupInfo = gatewaySessionGroupInfo(session)
      let groupIndex = indexByKey.get(groupInfo.key)
      if (groupIndex == null) {
        groupIndex = groups.length
        indexByKey.set(groupInfo.key, groupIndex)
        groups.push({ key: groupInfo.key, label: groupInfo.label, sortMs: groupInfo.sortMs, sessions: [] })
      }
      groups[groupIndex].sessions.push(session)
    }
    groups.sort((a, b) => b.sortMs - a.sortMs)
    return groups
  }, [sessionsList])
  const selectedSession = useMemo(
    () => sessionsList.find((session) => session.id === selectedSessionId) || null,
    [sessionsList, selectedSessionId],
  )
  const selectedSessionCompactedSummary = useMemo(() => {
    const summaryMessage = selectedSessionMessages.find(
      (message) =>
        Boolean(message.metadata?.compacted_summary) ||
        (message.role === 'system' && message.content.startsWith('[Compacted session summary]')),
    )
    return summaryMessage?.content || null
  }, [selectedSessionMessages])
  const selectedSessionConversationMessages = useMemo(() => {
    const filtered = selectedSessionMessages.filter(isRenderableSessionConversationMessage)
    return [...filtered].sort((a, b) => gatewaySessionMessageMs(a) - gatewaySessionMessageMs(b))
  }, [selectedSessionMessages])
  const selectedSessionHiddenMessageCount = Math.max(
    0,
    selectedSessionMessages.length - selectedSessionConversationMessages.length - (selectedSessionCompactedSummary ? 1 : 0),
  )

  const updateSessionsJumpVisibility = useCallback(() => {
    const el = sessionsDetailScrollRef.current
    if (!el) {
      setSessionsJumpToBottomVisible(false)
      return
    }
    const threshold = 72
    const gap = el.scrollHeight - el.scrollTop - el.clientHeight
    setSessionsJumpToBottomVisible(gap > threshold)
  }, [])

  const scrollSessionsDetailToBottom = useCallback(
    (smooth: boolean) => {
      const el = sessionsDetailScrollRef.current
      if (!el) return
      if (smooth) {
        el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
      } else {
        el.scrollTop = el.scrollHeight
      }
      window.setTimeout(updateSessionsJumpVisibility, smooth ? 380 : 0)
    },
    [updateSessionsJumpVisibility],
  )

  useLayoutEffect(() => {
    if (!active || page !== 'sessions' || !selectedSessionId) {
      setSessionsJumpToBottomVisible(false)
      return
    }
    if (selectedSessionLoading) {
      return
    }
    const el = sessionsDetailScrollRef.current
    if (!el) return
    const run = () => {
      el.scrollTop = el.scrollHeight
      updateSessionsJumpVisibility()
    }
    run()
    const raf = requestAnimationFrame(run)
    const t1 = window.setTimeout(run, 60)
    const t2 = window.setTimeout(run, 240)
    return () => {
      cancelAnimationFrame(raf)
      clearTimeout(t1)
      clearTimeout(t2)
    }
  }, [active, page, selectedSessionId, selectedSessionLoading, selectedSessionFetchedAt, updateSessionsJumpVisibility])

  const handleMiniDayClick = useCallback((date: Date | null) => {
    if (!date) return
    const todayMidnight = new Date(today.getFullYear(), today.getMonth(), today.getDate())
    const clickedMidnight = new Date(date.getFullYear(), date.getMonth(), date.getDate())
    const diffDays = Math.round((clickedMidnight.getTime() - todayMidnight.getTime()) / 86400000)
    setCalDayOffset(diffDays)
    setCalView('day')
    setSelectedCalEvent(null)
  }, [today])

  const monthLabel = useMemo(
    () => today.toLocaleDateString(undefined, { month: 'long', year: 'numeric' }),
    [today],
  )
  /* ── COMMAND data ─────────────────────────────────────── */

  const attentionMetrics = useMemo<SpaceMetric[]>(() => ([
    {
      label: 'Gateway posture',
      value: gatewayStatus.label,
      note: 'Session sync, channel ingress, and credential management.',
      tone: gatewayStatus.tone,
    },
    {
      label: 'Pending task inputs',
      value: String(pendingTaskCount).padStart(2, '0'),
      note: pendingTaskCount > 0 ? 'Items waiting for your decision.' : 'Nothing waiting on you right now.',
      tone: pendingTaskCount > 0 ? 'warm' : 'good',
    },
    {
      label: 'Cron results waiting',
      value: String(pendingCronCount).padStart(2, '0'),
      note: pendingCronCount > 0 ? 'Automation results ready for review.' : 'All results delivered.',
      tone: pendingCronCount > 0 ? 'warm' : 'cool',
    },
    {
      label: 'Active model',
      value: selectedModelLabel,
      note: 'Steering target for direct desktop conversations.',
      tone: 'muted',
    },
  ]), [gatewayStatus.label, gatewayStatus.tone, pendingCronCount, pendingTaskCount, selectedModelLabel])

  const operations = useMemo<OperationItem[]>(() => ([
    {
      title: 'Memory graph ingest',
      owner: 'cosmic-memory',
      status: 'Streaming',
      channel: 'Gateway',
      note: 'Canonical writes land first, then the durable graph queue drains behind them.',
      accent: 'azure',
    },
    {
      title: 'Firecrawl research agent',
      owner: 'firecrawl agent',
      status: 'Warm standby',
      channel: 'Agent bus',
      note: 'Specialist worker is healthy and ready for delegated scrape or extract intents.',
      accent: 'mint',
    },
    {
      title: 'YC watchlist diff',
      owner: 'Opus',
      status: pendingTaskCount > 0 ? `${pendingTaskCount} inputs waiting` : 'Waiting on you',
      channel: 'Desktop',
      note: 'Source review and send-path confirmation before the morning delivery runs.',
      accent: pendingTaskCount > 0 ? 'rose' : 'gold',
    },
    {
      title: 'DeepAgents eval loop',
      owner: 'Opus',
      status: 'Parked',
      channel: 'Desktop',
      note: 'Needs a call on the reward signal and acceptance gate before execution restarts.',
      accent: 'slate',
    },
  ]), [pendingTaskCount])

  /* ── AUTOPILOT data ───────────────────────────────────── */

  const cronCards: CronCard[] = [
    {
      label: 'Morning YC radar',
      schedule: '06:00 local time',
      channel: 'Desktop \u2192 WhatsApp if requested',
      timezone: 'User-local timezone snapshot',
      note: 'Diff against the saved baseline, then deliver through the requested destination channel.',
      state: 'live',
    },
    {
      label: 'Session rollover',
      schedule: '04:00 local time',
      channel: 'Gateway internal',
      timezone: 'Authoritative desktop timezone',
      note: 'Creates the daily session summary and carry-forward packet for exact revisit continuity.',
      state: 'live',
    },
    {
      label: 'Ontology curator',
      schedule: 'Every 6 hours',
      channel: 'cosmic-memory internal',
      timezone: 'UTC service loop',
      note: 'Batches recurring weak-fit observations into learned aliases without mutating the hard ontology inline.',
      state: 'queued',
    },
    {
      label: 'Operator rebuild lane',
      schedule: 'Manual / bounded maintenance',
      channel: 'Ops only',
      timezone: 'On demand',
      note: 'Reserved for future graph sync and cache-warm repair runs.',
      state: 'draft',
    },
  ]

  const backgroundProcesses: BackgroundProcess[] = [
    {
      title: 'Ontology alias promotion',
      owner: 'memory curator',
      status: 'Nightly review',
      channel: 'xAI loop',
      note: 'Recurring weak-fit labels gather here until the curator safely resolves them.',
      accent: 'mint',
    },
    {
      title: 'Operator smoke pack',
      owner: 'desktop',
      status: 'Draft shell',
      channel: 'Control Center',
      note: 'Backend feeds for this screen will plug into this operator lane next.',
      accent: 'slate',
    },
    {
      title: 'Usage ledger foundation',
      owner: 'Gateway',
      status: 'Stable',
      channel: 'SQLite',
      note: 'Provider usage now lands in the gateway usage ledger with queue-backed writes.',
      accent: 'azure',
    },
    {
      title: 'Reminder context capture',
      owner: 'Gateway',
      status: 'Stable',
      channel: 'Scheduler',
      note: 'Long-delay cron runs carry their own context packet forward instead of leaning on rollover alone.',
      accent: 'gold',
    },
  ]

  /* ── PULSE data ───────────────────────────────────────── */

  const meshEvents: MeshEvent[] = [
    {
      from: 'Desktop',
      to: 'Gateway',
      type: 'resume + history sync',
      note: 'State hydration, pending task inputs, and cross-channel continuity all enter here first.',
      tone: 'flow',
    },
    {
      from: 'Gateway',
      to: 'Orchestrator',
      type: 'TaskEnvelope (HTTP streaming)',
      note: 'Interactive user turns stay on the proven hot path instead of moving to Redis prematurely.',
      tone: 'flow',
    },
    {
      from: 'Orchestrator',
      to: 'Redis agent bus',
      type: 'delegate_to_agent',
      note: 'Specialist work fans out through the agent registry and signed child-task dispatch.',
      tone: 'observe',
    },
    {
      from: 'Agent bus',
      to: 'Gateway events',
      type: 'streams:events',
      note: 'Progress, defer, reject, and completion events all return through one shared event lane.',
      tone: 'flow',
    },
    {
      from: 'Gateway',
      to: 'Desktop',
      type: 'task.progress / response.complete',
      note: 'Desktop live activity and replayed offline results eventually light up this surface directly.',
      tone: 'wait',
    },
  ]

  const providerCards: ObservatoryCard[] = [
    {
      label: 'Anthropic / Opus',
      value: 'Task orchestration',
      detail: 'Primary planner, reminder author, and high-agency tool user.',
      accent: 'azure',
    },
    {
      label: 'xAI / Grok',
      value: 'Graph intelligence',
      detail: 'Write-time extraction, adjudication, and soft ontology curation.',
      accent: 'rose',
    },
    {
      label: 'Perplexity',
      value: 'Current info + vectors',
      detail: 'Verification-heavy direct routes and passive recall embeddings.',
      accent: 'mint',
    },
    {
      label: 'Gateway ledgers',
      value: 'Operator truth tables',
      detail: 'Usage, routing, scheduler, and memory audit all converge here.',
      accent: 'gold',
    },
  ]

  /* ── MY CALENDAR data ──────────────────────────────────── */

  const weekStart = useMemo(() => {
    const d = new Date(today)
    d.setDate(d.getDate() - d.getDay() + calWeekOffset * 7)
    return d
  }, [today, calWeekOffset])

  // Day view: which single day to show
  const dayViewDate = useMemo(() => {
    const d = new Date(today)
    d.setDate(d.getDate() + calDayOffset)
    return d
  }, [today, calDayOffset])

  const weekDays = useMemo(
    () => Array.from({ length: 7 }, (_, i) => addDays(weekStart, i)),
    [weekStart],
  )

  const weekRangeLabel = useMemo(() => {
    const start = weekDays[0]
    const end = weekDays[6]
    const startStr = start.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    const endStr = end.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
    return `${startStr} \u2013 ${endStr}`
  }, [weekDays])

  const calendarHours = useMemo(
    () => Array.from({ length: CAL_LAST_HOUR - CAL_FIRST_HOUR }, (_, i) => CAL_FIRST_HOUR + i),
    [],
  )

  // Convert live CalendarAgendaEvents → WeekEvent for the current week
  const weekEvents = useMemo<WeekEvent[]>(() => {
    return calendarData.events
      .map((e) => agendaEventToWeekEvent(e, weekStart))
      .filter((e): e is WeekEvent => e !== null)
  }, [calendarData.events, weekStart])

  // All-day events for the current week (banner row)
  const allDayEvents = useMemo<CalendarAgendaEvent[]>(() => {
    return calendarData.events.filter((e) => {
      if (!e.isAllDay) return false
      const start = getCalendarEventStart(e)
      if (!start) return false
      const weekEnd = new Date(weekStart)
      weekEnd.setDate(weekStart.getDate() + 7)
      return start >= weekStart && start < weekEnd
    })
  }, [calendarData.events, weekStart])

  // Today's events for "Up next" sidebar (sorted by start time)
  const todayAgendaEvents = useMemo<CalendarAgendaEvent[]>(() => {
    const now = new Date()
    return calendarData.events
      .filter((e) => {
        const start = getCalendarEventStart(e)
        return start && isSameCalendarDay(start, today)
      })
      .sort((a, b) => {
        const sa = getCalendarEventStart(a)
        const sb = getCalendarEventStart(b)
        return (sa?.getTime() ?? 0) - (sb?.getTime() ?? 0)
      })
      .filter((e) => {
        const end = getCalendarEventEnd(e)
        return !end || end >= now
      })
  }, [calendarData.events, today])

  // Unique calendar names for "My calendars" sidebar
  const calendarNames = useMemo(() => {
    const seen = new Set<string>()
    const names: Array<{ name: string; accent: AccentTone }> = []
    for (const e of calendarData.events) {
      if (!seen.has(e.calendar_id)) {
        seen.add(e.calendar_id)
        names.push({ name: e.calendar_name || 'Primary', accent: googleColorToAccent(e.colorId, e.calendar_color) })
      }
    }
    return names
  }, [calendarData.events])

  const calendarMarkerDays = useMemo(() => {
    const markers = new Set<number>()
    for (const e of calendarData.events) {
      const start = getCalendarEventStart(e)
      if (start && start.getFullYear() === today.getFullYear() && start.getMonth() === today.getMonth()) {
        markers.add(start.getDate())
      }
    }
    return markers
  }, [calendarData.events, today])

  const calendarMonthCells = useMemo(
    () => buildCalendarMonthCells(today, calendarMarkerDays),
    [today, calendarMarkerDays],
  )


  const nowLineTop = useMemo(() => {
    const hour = now.getHours()
    const minute = now.getMinutes()
    if (hour < CAL_FIRST_HOUR || hour >= CAL_LAST_HOUR) return null
    return (hour - CAL_FIRST_HOUR + minute / 60) * CAL_HOUR_HEIGHT
  }, [now])

  const calendarHasAccounts = calendarData.accounts.length > 0

  const [agentEmailBaseUrl, setAgentEmailBaseUrl] = useState('')
  const [agentEmailApiToken, setAgentEmailApiToken] = useState('')
  const [agentEmailSettingsLoaded, setAgentEmailSettingsLoaded] = useState(false)
  const [agentEmailBackendStatus, setAgentEmailBackendStatus] = useState<GatewayAgentEmailStatus | null>(null)
  const [agentEmailBackendLoading, setAgentEmailBackendLoading] = useState(false)
  const [agentEmailLoading, setAgentEmailLoading] = useState(false)
  const [agentEmailConfigSaving, setAgentEmailConfigSaving] = useState(false)
  const [agentEmailSyncingInbox, setAgentEmailSyncingInbox] = useState(false)
  const [agentEmailReplySending, setAgentEmailReplySending] = useState(false)
  const [agentEmailCreatingDomain, setAgentEmailCreatingDomain] = useState(false)
  const [agentEmailCreatingMailbox, setAgentEmailCreatingMailbox] = useState(false)
  const [agentEmailCreatingNewAgent, setAgentEmailCreatingNewAgent] = useState(false)
  const [agentEmailVerifyingDomain, setAgentEmailVerifyingDomain] = useState(false)
  const [agentEmailActionId, setAgentEmailActionId] = useState<string | null>(null)
  const [agentEmailError, setAgentEmailError] = useState<string | null>(null)
  const [agentEmailBanner, setAgentEmailBanner] = useState<{ tone: AgentEmailBannerTone; message: string } | null>(null)
  const [agentEmailThreadMessagesLoadingId, setAgentEmailThreadMessagesLoadingId] = useState<string | null>(null)
  const [agentEmailOrg, setAgentEmailOrg] = useState<CosmicMailOrganizationRead | null>(null)
  const [agentEmailChecklist, setAgentEmailChecklist] = useState<AgentEmailChecklistItem[]>([])
  const [agentEmailAgents, setAgentEmailAgents] = useState<AgentEmailAgent[]>([])
  const [agentEmailInboxes, setAgentEmailInboxes] = useState<AgentEmailInbox[]>([])
  const [agentEmailThreads, setAgentEmailThreads] = useState<AgentEmailThread[]>([])
  const [agentEmailDomains, setAgentEmailDomains] = useState<AgentEmailDomain[]>([])
  const [agentEmailApprovals, setAgentEmailApprovals] = useState<AgentEmailApproval[]>([])
  const [agentEmailReplyDraft, setAgentEmailReplyDraft] = useState('')
  const [agentEmailDomainNameDraft, setAgentEmailDomainNameDraft] = useState('')
  const [agentEmailMailboxLocalPartDraft, setAgentEmailMailboxLocalPartDraft] = useState('')
  const [agentEmailMailboxDisplayNameDraft, setAgentEmailMailboxDisplayNameDraft] = useState('')
  const [agentEmailMailboxDomainIdDraft, setAgentEmailMailboxDomainIdDraft] = useState('')
  const [agentEmailNewAgentNameDraft, setAgentEmailNewAgentNameDraft] = useState('')
  const [agentEmailNewAgentSlugDraft, setAgentEmailNewAgentSlugDraft] = useState('')
  const [agentEmailNewAgentDomainIdDraft, setAgentEmailNewAgentDomainIdDraft] = useState('')
  const [agentEmailInboxSearchQuery, setAgentEmailInboxSearchQuery] = useState('')
  const [agentEmailInboxSearchApplied, setAgentEmailInboxSearchApplied] = useState('')
  const [agentEmailTrustedSenders, setAgentEmailTrustedSenders] = useState<string[]>([])
  const [agentEmailTrustedSenderDraft, setAgentEmailTrustedSenderDraft] = useState('')
  /** Inbox: reply composer expanded vs Gmail-style reply strip. */
  const [agentEmailComposerExpanded, setAgentEmailComposerExpanded] = useState(false)
  const [agentEmailReplyAttachmentFiles, setAgentEmailReplyAttachmentFiles] = useState<File[]>([])
  const [agentEmailAttachmentActionId, setAgentEmailAttachmentActionId] = useState<string | null>(null)
  const [gmailApprovals, setGmailApprovals] = useState<GmailApproval[]>([])
  const [gmailLoading, setGmailLoading] = useState(false)
  const [gmailError, setGmailError] = useState<string | null>(null)
  const [gmailActionId, setGmailActionId] = useState<string | null>(null)
  const [gmailSelectedApprovalId, setGmailSelectedApprovalId] = useState<string | null>(null)
  const [gmailBanner, setGmailBanner] = useState<{ tone: AgentEmailBannerTone; message: string } | null>(null)
  const agentEmailReplyAttachInputRef = useRef<HTMLInputElement>(null)
  const agentEmailMessagesEndRef = useRef<HTMLDivElement>(null)
  const agentEmailBaseUrlRef = useRef('')
  const agentEmailApiTokenRef = useRef('')
  const agentEmailTrustedSendersRef = useRef<string[]>([])
  const agentEmailBackendBootstrapDoneRef = useRef(false)
  const agentEmailLocalConfigReady = agentEmailBaseUrl.trim().length > 0 && agentEmailApiToken.trim().length > 0
  const agentEmailBackendConfigured = Boolean(agentEmailBackendStatus?.configured)
  const agentEmailEffectiveBaseUrl = agentEmailBaseUrl.trim() || agentEmailBackendStatus?.base_url?.trim() || ''
  const agentEmailEffectiveApiToken = agentEmailApiToken.trim() || agentEmailBackendStatus?.api_token?.trim() || ''
  const agentEmailBackendHasUsableConfig = Boolean(
    !agentEmailBackendStatus?.explicitly_disconnected
    && agentEmailBackendStatus?.base_url
    && agentEmailBackendStatus?.api_token
    && (
      agentEmailBackendStatus.configured
      || agentEmailBackendStatus.connected
      || agentEmailBackendStatus.healthy
      || agentEmailBackendStatus.adapter_registered
    ),
  )
  const agentEmailConfigReady = Boolean(
    agentEmailEffectiveBaseUrl
    && agentEmailEffectiveApiToken
    && (agentEmailBackendHasUsableConfig || (agentEmailLocalConfigReady && agentEmailBackendConfigured)),
  )

  useEffect(() => {
    agentEmailBaseUrlRef.current = agentEmailBaseUrl
  }, [agentEmailBaseUrl])

  useEffect(() => {
    agentEmailApiTokenRef.current = agentEmailApiToken
  }, [agentEmailApiToken])

  useEffect(() => {
    agentEmailTrustedSendersRef.current = agentEmailTrustedSenders
  }, [agentEmailTrustedSenders])

  const callAgentEmailApi = useCallback(async (
    path: string,
    init: {
      method?: string
      body?: unknown
      timeoutMs?: number
    } = {},
  ) => {
    if (!window.cosmic?.cosmicMailRequest) {
      throw new Error('Cosmic Mail transport bridge is unavailable.')
    }
    const normalizedPath = (() => {
      const cleanPath = path.startsWith('/') ? path : `/${path}`
      if (cleanPath === '/health' || cleanPath === '/ready' || cleanPath.startsWith('/v1/')) {
        return cleanPath
      }
      return `/v1${cleanPath}`
    })()
    return window.cosmic.cosmicMailRequest({
      baseUrl: agentEmailEffectiveBaseUrl,
      apiToken: agentEmailEffectiveApiToken,
      path: normalizedPath,
      method: init.method,
      body: init.body,
      timeoutMs: init.timeoutMs,
    })
  }, [agentEmailEffectiveApiToken, agentEmailEffectiveBaseUrl])

  const requestAgentEmailBackendStatus = useCallback(async (
    applyFields = true,
    showSpinner = false,
    preserveLocalIfBackendUnconfigured = false,
  ) => {
    if (!window.cosmic?.getGatewayAgentEmailStatus) {
      return null
    }
    if (showSpinner) {
      setAgentEmailBackendLoading(true)
    }
    try {
      const raw = await window.cosmic.getGatewayAgentEmailStatus()
      const status = normalizeGatewayAgentEmailStatus(raw)
      setAgentEmailBackendStatus(status)
      if (
        status.trusted_senders.length
        && trustedSenderListSignature(status.trusted_senders) !== trustedSenderListSignature(agentEmailTrustedSendersRef.current)
      ) {
        setAgentEmailTrustedSenders(status.trusted_senders)
        window.cosmic?.saveSetting(AGENT_EMAIL_SETTINGS_KEYS.trustedSenders, JSON.stringify(status.trusted_senders))
      }
      if (applyFields) {
        const shouldApplyBackendFields =
          status.configured ||
          status.explicitly_disconnected ||
          !preserveLocalIfBackendUnconfigured
        if (shouldApplyBackendFields) {
          const nextBaseUrl = status.base_url || ''
          const nextApiToken = status.api_token || ''
          if (agentEmailBaseUrlRef.current !== nextBaseUrl) {
            setAgentEmailBaseUrl(nextBaseUrl)
            window.cosmic?.saveSetting(AGENT_EMAIL_SETTINGS_KEYS.baseUrl, nextBaseUrl)
          }
          if (agentEmailApiTokenRef.current !== nextApiToken) {
            setAgentEmailApiToken(nextApiToken)
            window.cosmic?.saveSetting(AGENT_EMAIL_SETTINGS_KEYS.apiToken, nextApiToken)
          }
        }
      }
      return status
    } catch (error: unknown) {
      setAgentEmailBackendStatus(null)
      throw error
    } finally {
      if (showSpinner) {
        setAgentEmailBackendLoading(false)
      }
    }
  }, [])

  const syncAgentEmailTrustedSenders = useCallback(async (nextTrustedSenders: string[]) => {
    const normalized = parseAgentEmailTrustedSendersSetting(nextTrustedSenders)
    if (trustedSenderListSignature(normalized) !== trustedSenderListSignature(agentEmailTrustedSendersRef.current)) {
      setAgentEmailTrustedSenders(normalized)
      window.cosmic?.saveSetting(AGENT_EMAIL_SETTINGS_KEYS.trustedSenders, JSON.stringify(normalized))
    }
    if (!gatewayConnected || !window.cosmic?.saveGatewayAgentEmailTrustedSenders) {
      return null
    }
    try {
      const rawStatus = await window.cosmic.saveGatewayAgentEmailTrustedSenders({
        trustedSenders: normalized,
      })
      const status = normalizeGatewayAgentEmailStatus(rawStatus)
      setAgentEmailBackendStatus(status)
      if (trustedSenderListSignature(status.trusted_senders) !== trustedSenderListSignature(agentEmailTrustedSendersRef.current)) {
        setAgentEmailTrustedSenders(status.trusted_senders)
        window.cosmic?.saveSetting(AGENT_EMAIL_SETTINGS_KEYS.trustedSenders, JSON.stringify(status.trusted_senders))
      }
      return status
    } catch (error: unknown) {
      setAgentEmailBanner({
        tone: 'error',
        message: `Trusted sender sync failed. Saved locally only. ${toErrorMessage(error)}`,
      })
      return null
    }
  }, [gatewayConnected])

  const ensureAgentEmailBackendConnection = useCallback(async (showSpinner = false) => {
    let status = await requestAgentEmailBackendStatus(true, showSpinner, true)
    if (!status) {
      return null
    }
    const localTrustedSenders = parseAgentEmailTrustedSendersSetting(agentEmailTrustedSendersRef.current)
    if (status.trusted_senders.length) {
      if (trustedSenderListSignature(status.trusted_senders) !== trustedSenderListSignature(localTrustedSenders)) {
        setAgentEmailTrustedSenders(status.trusted_senders)
        window.cosmic?.saveSetting(AGENT_EMAIL_SETTINGS_KEYS.trustedSenders, JSON.stringify(status.trusted_senders))
      }
    } else if (localTrustedSenders.length) {
      const syncedStatus = await syncAgentEmailTrustedSenders(localTrustedSenders)
      if (syncedStatus) {
        status = syncedStatus
      }
    }
    const nextBaseUrl = agentEmailBaseUrlRef.current.trim()
    const nextApiToken = agentEmailApiTokenRef.current.trim()
    if (status.explicitly_disconnected) {
      if (agentEmailBaseUrlRef.current) {
        setAgentEmailBaseUrl('')
        window.cosmic?.saveSetting(AGENT_EMAIL_SETTINGS_KEYS.baseUrl, '')
      }
      if (agentEmailApiTokenRef.current) {
        setAgentEmailApiToken('')
        window.cosmic?.saveSetting(AGENT_EMAIL_SETTINGS_KEYS.apiToken, '')
      }
      return status
    }
    if (status.configured || !nextBaseUrl || !nextApiToken) {
      return status
    }
    if (!window.cosmic?.saveGatewayAgentEmailConfig) {
      throw new Error('Gateway Agent Email bridge is unavailable.')
    }
    const rawStatus = await window.cosmic.saveGatewayAgentEmailConfig({
      baseUrl: nextBaseUrl,
      apiToken: nextApiToken,
    })
    const syncedStatus = normalizeGatewayAgentEmailStatus(rawStatus)
    setAgentEmailBackendStatus(syncedStatus)
    const syncedBaseUrl = syncedStatus.base_url || nextBaseUrl
    const syncedApiToken = syncedStatus.api_token || nextApiToken
    setAgentEmailBaseUrl(syncedBaseUrl)
    setAgentEmailApiToken(syncedApiToken)
    window.cosmic?.saveSetting(AGENT_EMAIL_SETTINGS_KEYS.baseUrl, syncedBaseUrl)
    window.cosmic?.saveSetting(AGENT_EMAIL_SETTINGS_KEYS.apiToken, syncedApiToken)
    setAgentEmailBanner({ tone: 'success', message: 'Cosmic Mail connection synced to the VM.' })
    return syncedStatus
  }, [
    requestAgentEmailBackendStatus,
    syncAgentEmailTrustedSenders,
  ])

  const loadAgentEmailThreads = useCallback(async (
    mailboxId: string,
    approvals: AgentEmailApproval[],
    mailboxAddress?: string,
    searchQuery?: string,
    hydrateThreadId?: string,
  ): Promise<AgentEmailThread[]> => {
    const q = (searchQuery || '').trim()
    const threadsRawAll = await callAgentEmailApi(
      `/threads?${new URLSearchParams({ mailbox_id: mailboxId }).toString()}`,
    ) as CosmicMailThreadRead[]
    const needle = q.toLowerCase()
    const threadsRaw = needle
      ? threadsRawAll.filter((thread) => {
          const subject = (thread.subject || '').toLowerCase()
          const normalized = (thread.normalized_subject || '').toLowerCase()
          const snippet = (thread.snippet || '').toLowerCase()
          return subject.includes(needle) || normalized.includes(needle) || snippet.includes(needle)
        })
      : threadsRawAll
    const threads = [...threadsRaw].sort(
      (a, b) => new Date(b.last_message_at).getTime() - new Date(a.last_message_at).getTime(),
    )
    const selectedHydrateThreadId = hydrateThreadId && threads.some((thread) => thread.id === hydrateThreadId)
      ? hydrateThreadId
      : threads[0]?.id || ''
    const threadEntries = await Promise.all(threads.map(async (thread) => {
      let messages: CosmicMailMessageRead[] = []
      let messagesLoaded = false
      if (thread.id === selectedHydrateThreadId) {
        try {
          messages = await callAgentEmailApi(`/threads/${thread.id}/messages`, { timeoutMs: 12000 }) as CosmicMailMessageRead[]
          messagesLoaded = true
        } catch {
          messages = []
        }
      }
      const sortedMessages = [...messages].sort((a, b) => {
        const aTime = new Date(a.received_at || a.sent_at || a.created_at).getTime()
        const bTime = new Date(b.received_at || b.sent_at || b.created_at).getTime()
        return aTime - bTime
      })
      const lastMessage = sortedMessages[sortedMessages.length - 1] || null
      const unread = messagesLoaded
        ? sortedMessages.some((message) => message.direction === 'inbound' && !message.is_read)
        : false
      const matchingApproval = approvals.find((approval) => {
        if (approval.subject !== thread.subject) {
          return false
        }
        if (!mailboxAddress) {
          return true
        }
        return approval.mailbox === mailboxAddress
      })
      const state = matchingApproval && matchingApproval.subject === thread.subject
        ? 'Awaiting approval'
        : unread
          ? 'Needs reply'
          : lastMessage?.direction === 'outbound'
            ? 'Waiting'
            : 'Read'

      return {
        id: thread.id,
        subject: thread.subject,
        fromName: lastMessage?.from_name || lastMessage?.from_address || 'Thread',
        fromAddress: lastMessage?.from_address || '—',
        time: formatAgentEmailRelative(lastMessage?.received_at || lastMessage?.sent_at || thread.last_message_at),
        unread,
        state,
        snippet: buildAgentEmailSnippet(lastMessage, thread.snippet),
        lastMessageAt: thread.last_message_at,
        threadSnapshot: thread,
        messagesSource: sortedMessages,
        messages: sortedMessages.map((message) => ({
          id: message.id,
          direction: message.direction,
          author: message.from_name || message.from_address || 'Unknown sender',
          address: message.from_address,
          time: formatAgentEmailAbsolute(message.received_at || message.sent_at || message.created_at),
          body: buildAgentEmailMessageBody(message),
          isRead: message.is_read,
          attachments: message.attachments ?? [],
        })),
        messagesLoaded,
      } satisfies AgentEmailThread
    }))
    return [...threadEntries].sort((a, b) => {
      if (a.unread !== b.unread) {
        return a.unread ? -1 : 1
      }
      return new Date(b.lastMessageAt).getTime() - new Date(a.lastMessageAt).getTime()
    })
  }, [callAgentEmailApi])

  const requestAgentEmailSnapshot = useCallback(async (showSpinner = false) => {
    if (!agentEmailConfigReady) {
      setAgentEmailOrg(null)
      setAgentEmailChecklist([])
      setAgentEmailAgents([])
      setAgentEmailInboxes([])
      setAgentEmailThreads([])
      setAgentEmailDomains([])
      setAgentEmailApprovals([])
      setAgentEmailError(null)
      return
    }

    if (showSpinner) {
      setAgentEmailLoading(true)
    }

    try {
      const partialErrors: string[] = []
      const loadOptionalValue = async <T,>(pathName: string, label: string, timeoutMs = 20000): Promise<T | null> => {
        try {
          return await callAgentEmailApi(pathName, { timeoutMs }) as T
        } catch (error: unknown) {
          partialErrors.push(`${label}: ${toErrorMessage(error)}`)
          return null
        }
      }
      const loadOptionalList = async <T,>(pathName: string, label: string, timeoutMs = 20000): Promise<T[]> => {
        try {
          const value = await callAgentEmailApi(pathName, { timeoutMs })
          return Array.isArray(value) ? value as T[] : []
        } catch (error: unknown) {
          partialErrors.push(`${label}: ${toErrorMessage(error)}`)
          return []
        }
      }

      const [authContext, organizations, agentsRaw, mailboxesRaw, domainsRaw, approvalsRaw] = await Promise.all([
        loadOptionalValue<CosmicMailAuthContextRead>('/system/auth-context', 'Auth context', 8000),
        loadOptionalList<CosmicMailOrganizationRead>('/organizations', 'Organizations', 8000),
        loadOptionalList<CosmicMailAgentRead>('/agents', 'Agents'),
        loadOptionalList<CosmicMailMailboxRead>('/mailboxes', 'Inboxes'),
        loadOptionalList<CosmicMailDomainRead>('/domains', 'Domains', 8000),
        loadOptionalList<CosmicMailApprovalRead>('/approvals', 'Approvals'),
      ])

      const fallbackOrganizationId =
        authContext?.organization_id ||
        organizations[0]?.id ||
        agentsRaw[0]?.organization_id ||
        mailboxesRaw[0]?.organization_id ||
        domainsRaw[0]?.organization_id ||
        approvalsRaw[0]?.organization_id ||
        'cosmic-mail'
      const preferredOrganization =
        organizations.find((organization) => organization.id === authContext?.organization_id) ||
        organizations.find((organization) => organization.slug.toLowerCase() === 'cosmic' || organization.name.toLowerCase() === 'cosmic') ||
        organizations[0] ||
        {
          id: fallbackOrganizationId,
          name: 'Cosmic Mail',
          slug: 'cosmic',
          created_at: '',
        }
      const shouldFilterByOrganization = Boolean(authContext?.organization_id || organizations.length)
      const belongsToPreferredOrganization = (organizationId: string | null | undefined) => (
        !shouldFilterByOrganization || !organizationId || organizationId === preferredOrganization.id
      )

      const organizationAgents = agentsRaw.filter((agent) => belongsToPreferredOrganization(agent.organization_id))
      const organizationMailboxes = mailboxesRaw.filter((mailbox) => belongsToPreferredOrganization(mailbox.organization_id))
      const organizationDomains = domainsRaw.filter((domain) => belongsToPreferredOrganization(domain.organization_id))
      const organizationApprovals = approvalsRaw.filter((approval) => belongsToPreferredOrganization(approval.organization_id))

      const domainDeliverability = await Promise.all(organizationDomains.map(async (domain) => {
        try {
          const detail = await callAgentEmailApi(`/domains/${domain.id}/deliverability`, { timeoutMs: 8000 }) as CosmicMailDomainDeliverabilityRead
          return [domain.id, detail] as const
        } catch {
          return [domain.id, null] as const
        }
      }))

      const domainDetailMap = new Map(domainDeliverability)
      const mailboxAgentNames = new Map<string, string[]>()
      for (const agent of organizationAgents) {
        for (const mailbox of agent.mailboxes) {
          const next = mailboxAgentNames.get(mailbox.mailbox_id) || []
          next.push(agent.name)
          mailboxAgentNames.set(mailbox.mailbox_id, next)
        }
      }

      const mappedAgents: AgentEmailAgent[] = organizationAgents.map((agent) => ({
        id: agent.id,
        name: agent.name,
        role: agent.title || agent.persona_summary || 'Default operator',
        address: agent.mailboxes[0]?.address || `${agent.slug}@${agent.default_domain_name || preferredOrganization.slug}`,
        status: humanizeAgentEmailValue(agent.status),
        domain: agent.default_domain_name || agent.mailboxes[0]?.domain_name || preferredOrganization.slug,
        inboxes: agent.mailboxes.map((mailbox) => mailbox.address),
        approvalMode: agent.approval_required ? 'Required' : 'Autonomous',
        lastActivity: agent.mailboxes[0]?.last_synced_at ? `Synced ${formatAgentEmailRelative(agent.mailboxes[0].last_synced_at)}` : 'No recent sync activity',
        note: agent.persona_summary || agent.system_prompt || 'No agent summary has been provided yet.',
        accent: mapAgentEmailAccent(agent.status, 'agent'),
      }))

      const mappedInboxes: AgentEmailInbox[] = organizationMailboxes.map((mailbox) => ({
        id: mailbox.id,
        address: mailbox.address,
        type: mailbox.display_name || 'Primary',
        status: humanizeAgentEmailValue(mailbox.status),
        sync: mailbox.inbound_sync_enabled ? 'IMAP connected' : 'Sync disabled',
        queue: mailbox.last_sync_error ? 'Needs attention' : 'Ready',
        linkedAgents: mailboxAgentNames.get(mailbox.id) || [],
        lastSync: formatAgentEmailRelative(mailbox.last_synced_at),
        note: mailbox.last_sync_error || 'Inbound sync is healthy and this inbox is ready for review inside Spaces.',
        accent: mapAgentEmailAccent(mailbox.status, 'inbox'),
      }))

      const mappedDomains: AgentEmailDomain[] = organizationDomains.map((domain) => {
        const detail = domainDetailMap.get(domain.id)
        const relatedMailboxes = organizationMailboxes.filter((mailbox) => mailbox.domain_id === domain.id)
        const recordSource = detail?.dns_records || domain.dns_records
        return {
          id: domain.id,
          name: domain.name,
          status: humanizeAgentEmailValue(domain.status),
          dns: recordSource.length
            ? `${recordSource.filter((record) => !!record.value).length} records published`
            : 'No DNS records available',
          mailboxes: `${relatedMailboxes.length} inbox${relatedMailboxes.length === 1 ? '' : 'es'}`,
          provider: 'External DNS',
          reputation: detail?.status ? humanizeAgentEmailValue(detail.status) : humanizeAgentEmailValue(domain.status),
          note: detail?.dmarc_value || 'DNS guidance is available once the domain is connected.',
          records: recordSource.map((record) => ({
            label: record.type === 'TXT'
              ? (record.host.includes('_domainkey') ? 'DKIM' : record.host.includes('_dmarc') ? 'DMARC' : 'TXT')
              : record.type,
            status: record.value ? 'Published' : 'Missing',
            value: `${record.host} -> ${record.value}`,
          })),
          accent: mapAgentEmailAccent(domain.status, 'domain'),
        }
      })

      const formatRecipientList = (contacts: CosmicMailMailContact[] | undefined) =>
        contacts?.map((recipient) => recipient.email).filter(Boolean).join(', ') || ''

      const mappedApprovals: AgentEmailApproval[] = organizationApprovals.map((approval) => ({
        id: approval.id,
        subject: approval.draft?.subject || 'Untitled draft',
        agent: approval.agent_name || 'Unknown agent',
        mailbox: approval.mailbox_address,
        recipients: formatRecipientList(approval.draft?.to_recipients) || '—',
        cc: formatRecipientList(approval.draft?.cc_recipients),
        bcc: formatRecipientList(approval.draft?.bcc_recipients),
        state: humanizeAgentEmailValue(approval.status),
        reason: approval.reviewer_note || 'Waiting for review',
        time: formatAgentEmailRelative(approval.created_at),
        summary: approval.draft?.text_body || stripAgentEmailHtml(approval.draft?.html_body) || 'No draft body available.',
        excerpt: approval.draft?.text_body || stripAgentEmailHtml(approval.draft?.html_body) || 'No draft body available.',
        accent: mapAgentEmailAccent(approval.status, 'approval'),
      }))

      const nextSelectedInboxId = mappedInboxes.find((inbox) => inbox.id === agentEmailSelectedInboxId)?.id || mappedInboxes[0]?.id || ''
      const nextSelectedInbox = mappedInboxes.find((inbox) => inbox.id === nextSelectedInboxId)
      let mappedThreads: AgentEmailThread[] = []
      if (nextSelectedInboxId) {
        try {
          mappedThreads = await loadAgentEmailThreads(nextSelectedInboxId, mappedApprovals, nextSelectedInbox?.address, undefined, agentEmailSelectedThreadId)
        } catch (error: unknown) {
          partialErrors.push(`Inbox: ${toErrorMessage(error)}`)
          mappedThreads = []
        }
      }

      setAgentEmailOrg(preferredOrganization)
      setAgentEmailChecklist([
        {
          label: `${preferredOrganization.name} organization ready`,
          state: 'Ready',
          note: 'Spaces is connected to the default Cosmic Mail organization.',
          complete: true,
        },
        {
          label: 'Primary domain linked',
          state: organizationDomains.length ? 'Verified' : 'Pending',
          note: organizationDomains.length ? 'A sending domain is available for the default org.' : 'Link a domain to start sending from your own address.',
          complete: organizationDomains.length > 0,
        },
        {
          label: 'Default inbox connected',
          state: organizationMailboxes.length ? 'Live' : 'Pending',
          note: organizationMailboxes.length ? 'Inbound mail is flowing into the default inbox.' : 'Provision an inbox to begin receiving messages.',
          complete: organizationMailboxes.length > 0,
        },
        {
          label: 'Spaces live bridge',
          state: 'Connected',
          note: 'This screen is pulling live Cosmic Mail data now.',
          complete: true,
        },
      ])
      setAgentEmailAgents(mappedAgents)
      setAgentEmailInboxes(mappedInboxes)
      setAgentEmailThreads(mappedThreads)
      setAgentEmailDomains(mappedDomains)
      setAgentEmailApprovals(mappedApprovals)
      setAgentEmailSelectedAgentId((current) => mappedAgents.find((agent) => agent.id === current)?.id || mappedAgents[0]?.id || '')
      setAgentEmailSelectedInboxId(nextSelectedInboxId)
      setAgentEmailSelectedThreadId((current) => mappedThreads.find((thread) => thread.id === current)?.id || mappedThreads[0]?.id || '')
      setAgentEmailSelectedDomainId((current) => mappedDomains.find((domain) => domain.id === current)?.id || mappedDomains[0]?.id || '')
      setAgentEmailSelectedApprovalId((current) => mappedApprovals.find((approval) => approval.id === current)?.id || mappedApprovals[0]?.id || '')
      setAgentEmailError(partialErrors.length ? `Some Agent Email sections did not finish loading. ${partialErrors[0]}` : null)
    } catch (error: unknown) {
      setAgentEmailError(toErrorMessage(error))
    } finally {
      setAgentEmailLoading(false)
    }
  }, [
    agentEmailConfigReady,
    agentEmailSelectedInboxId,
    agentEmailSelectedThreadId,
    callAgentEmailApi,
    loadAgentEmailThreads,
  ])

  useEffect(() => {
    if (!agentEmailBanner) return
    const timer = window.setTimeout(() => setAgentEmailBanner(null), 4000)
    return () => window.clearTimeout(timer)
  }, [agentEmailBanner])

  useEffect(() => {
    const offSettings = window.cosmic?.onSettingsUpdate((settings) => {
      setAgentEmailBaseUrl(String(settings?.[AGENT_EMAIL_SETTINGS_KEYS.baseUrl] ?? ''))
      setAgentEmailApiToken(String(settings?.[AGENT_EMAIL_SETTINGS_KEYS.apiToken] ?? ''))
      setAgentEmailTrustedSenders(parseAgentEmailTrustedSendersSetting(settings?.[AGENT_EMAIL_SETTINGS_KEYS.trustedSenders]))
      setAgentEmailSettingsLoaded(true)
    })
    window.cosmic?.getSettings()
    return () => { offSettings?.() }
  }, [])

  useEffect(() => {
    if (!active || page !== 'agent-email' || !agentEmailSettingsLoaded || !gatewayConnected) {
      agentEmailBackendBootstrapDoneRef.current = false
      return
    }
    if (agentEmailBackendBootstrapDoneRef.current) {
      return
    }
    agentEmailBackendBootstrapDoneRef.current = true
    void ensureAgentEmailBackendConnection(true).catch((error: unknown) => {
      agentEmailBackendBootstrapDoneRef.current = false
      setAgentEmailBanner({ tone: 'error', message: toErrorMessage(error) })
    })
  }, [active, page, gatewayConnected, agentEmailSettingsLoaded, ensureAgentEmailBackendConnection])

  useEffect(() => {
    if (!active || page !== 'agent-email' || !agentEmailSettingsLoaded || !agentEmailConfigReady) {
      return
    }
    void requestAgentEmailSnapshot(true)
  }, [active, page, agentEmailSettingsLoaded, agentEmailConfigReady, requestAgentEmailSnapshot])

  useEffect(() => {
    if (!active || page !== 'agent-email' || !agentEmailConfigReady) {
      return
    }
    if (!agentEmailSelectedInboxId) {
      return
    }
    const selectedInboxExists = agentEmailInboxes.some((inbox) => inbox.id === agentEmailSelectedInboxId)
    if (!selectedInboxExists) {
      return
    }
    void (async () => {
      try {
        const selectedInbox = agentEmailInboxes.find((inbox) => inbox.id === agentEmailSelectedInboxId)
        const threads = await loadAgentEmailThreads(
          agentEmailSelectedInboxId,
          agentEmailApprovals,
          selectedInbox?.address,
          agentEmailInboxSearchApplied,
          agentEmailSelectedThreadId,
        )
        setAgentEmailThreads(threads)
        setAgentEmailSelectedThreadId((current) => threads.find((thread) => thread.id === current)?.id || threads[0]?.id || '')
      } catch (error: unknown) {
        setAgentEmailError(toErrorMessage(error))
      }
    })()
  }, [
    active,
    agentEmailApprovals,
    agentEmailConfigReady,
    agentEmailInboxSearchApplied,
    agentEmailInboxes,
    agentEmailSelectedInboxId,
    agentEmailSelectedThreadId,
    loadAgentEmailThreads,
    page,
  ])

  useEffect(() => {
    setAgentEmailReplyDraft('')
    setAgentEmailComposerExpanded(false)
    setAgentEmailReplyAttachmentFiles([])
  }, [agentEmailSelectedThreadId])

  useEffect(() => {
    if (!agentEmailThreads.length) {
      setAgentEmailReplyDraft('')
      setAgentEmailComposerExpanded(false)
      setAgentEmailReplyAttachmentFiles([])
    }
  }, [agentEmailThreads.length])

  useEffect(() => {
    if (!agentEmailConfigReady && agentEmailSettingsSection !== 'connection' && agentEmailSettingsSection !== 'trusted-senders') {
      setAgentEmailSettingsSection('connection')
    }
  }, [agentEmailConfigReady, agentEmailSettingsSection])

  useEffect(() => {
    const trimmed = agentEmailInboxSearchQuery.trim()
    if (!trimmed) {
      setAgentEmailInboxSearchApplied('')
      return
    }
    const timer = window.setTimeout(() => {
      setAgentEmailInboxSearchApplied(trimmed)
    }, 400)
    return () => window.clearTimeout(timer)
  }, [agentEmailInboxSearchQuery])

  useEffect(() => {
    setAgentEmailInboxSearchQuery('')
    setAgentEmailInboxSearchApplied('')
  }, [agentEmailSelectedInboxId])

  const lastAgentEmailNavigateSignalRef = useRef(0)
  const pendingAgentEmailInboxMailboxIdRef = useRef<string | null>(null)

  useEffect(() => {
    const sig = agentEmailNavigateInboxSignal ?? 0
    if (!sig || sig === lastAgentEmailNavigateSignalRef.current) return
    const mid = (agentEmailNavigateInboxMailboxId && String(agentEmailNavigateInboxMailboxId).trim()) || ''
    pendingAgentEmailInboxMailboxIdRef.current = mid || null
    if (!active) return
    lastAgentEmailNavigateSignalRef.current = sig
    setPage('agent-email')
    setAgentEmailView('inboxes')
  }, [active, agentEmailNavigateInboxSignal, agentEmailNavigateInboxMailboxId])

  useEffect(() => {
    if (!active) return
    const want = pendingAgentEmailInboxMailboxIdRef.current
    if (!want) return
    if (agentEmailInboxes.length === 0) return
    if (agentEmailInboxes.some((inbox) => inbox.id === want)) {
      setAgentEmailSelectedInboxId(want)
    }
    pendingAgentEmailInboxMailboxIdRef.current = null
  }, [active, agentEmailInboxes])

  const lastAgentEmailNavigateApprovalsSignalRef = useRef(0)
  const pendingAgentEmailApprovalsIdRef = useRef<string | null>(null)

  useEffect(() => {
    const sig = agentEmailNavigateApprovalsSignal ?? 0
    if (!sig || sig === lastAgentEmailNavigateApprovalsSignalRef.current) return
    const aid = (agentEmailNavigateApprovalsId && String(agentEmailNavigateApprovalsId).trim()) || ''
    pendingAgentEmailApprovalsIdRef.current = aid || null
    if (!active) return
    lastAgentEmailNavigateApprovalsSignalRef.current = sig
    setPage('agent-email')
    setAgentEmailView('approvals')
  }, [active, agentEmailNavigateApprovalsSignal, agentEmailNavigateApprovalsId])

  useEffect(() => {
    if (!active) return
    const want = pendingAgentEmailApprovalsIdRef.current
    if (!want) return
    if (agentEmailApprovals.some((approval) => approval.id === want)) {
      setAgentEmailSelectedApprovalId(want)
    }
    pendingAgentEmailApprovalsIdRef.current = null
  }, [active, agentEmailApprovals])

  const agentEmailSelectedAgent = agentEmailAgents.find((agent) => agent.id === agentEmailSelectedAgentId) || agentEmailAgents[0] || null
  const agentEmailSelectedInbox = agentEmailInboxes.find((inbox) => inbox.id === agentEmailSelectedInboxId) || agentEmailInboxes[0] || null
  const agentEmailSelectedThread = agentEmailThreads.find((thread) => thread.id === agentEmailSelectedThreadId) || agentEmailThreads[0] || null
  const agentEmailSelectedDomain = agentEmailDomains.find((domain) => domain.id === agentEmailSelectedDomainId) || agentEmailDomains[0] || null
  const agentEmailSelectedApproval = agentEmailApprovals.find((approval) => approval.id === agentEmailSelectedApprovalId) || agentEmailApprovals[0] || null
  const gmailSelectedApproval = gmailApprovals.find((approval) => approval.id === gmailSelectedApprovalId) || gmailApprovals[0] || null

  useEffect(() => {
    if (!active || page !== 'agent-email' || !agentEmailConfigReady || !agentEmailSelectedThread || agentEmailSelectedThread.messagesLoaded) {
      return
    }

    let cancelled = false
    const threadId = agentEmailSelectedThread.id
    setAgentEmailThreadMessagesLoadingId(threadId)

    void (async () => {
      try {
        const messages = await callAgentEmailApi(`/threads/${threadId}/messages`, { timeoutMs: 15000 }) as CosmicMailMessageRead[]
        if (cancelled) {
          return
        }
        const sortedMessages = [...messages].sort((a, b) => {
          const aTime = new Date(a.received_at || a.sent_at || a.created_at).getTime()
          const bTime = new Date(b.received_at || b.sent_at || b.created_at).getTime()
          return aTime - bTime
        })
        const lastMessage = sortedMessages[sortedMessages.length - 1] || null
        setAgentEmailThreads((current) => current.map((thread) => {
          if (thread.id !== threadId) {
            return thread
          }
          const unread = sortedMessages.some((message) => message.direction === 'inbound' && !message.is_read)
          const matchingApproval = agentEmailApprovals.find((approval) => {
            if (approval.subject !== thread.subject) {
              return false
            }
            if (!agentEmailSelectedInbox?.address) {
              return true
            }
            return approval.mailbox === agentEmailSelectedInbox.address
          })
          const state = matchingApproval && matchingApproval.subject === thread.subject
            ? 'Awaiting approval'
            : unread
              ? 'Needs reply'
              : lastMessage?.direction === 'outbound'
                ? 'Waiting'
                : 'Read'
          return {
            ...thread,
            fromName: lastMessage?.from_name || lastMessage?.from_address || thread.fromName,
            fromAddress: lastMessage?.from_address || thread.fromAddress,
            time: formatAgentEmailRelative(lastMessage?.received_at || lastMessage?.sent_at || thread.lastMessageAt),
            unread,
            state,
            snippet: buildAgentEmailSnippet(lastMessage, thread.threadSnapshot.snippet),
            messagesSource: sortedMessages,
            messages: sortedMessages.map((message) => ({
              id: message.id,
              direction: message.direction,
              author: message.from_name || message.from_address || 'Unknown sender',
              address: message.from_address,
              time: formatAgentEmailAbsolute(message.received_at || message.sent_at || message.created_at),
              body: buildAgentEmailMessageBody(message),
              isRead: message.is_read,
              attachments: message.attachments ?? [],
            })),
            messagesLoaded: true,
          }
        }))
        setAgentEmailError((current) => current?.startsWith('Thread messages') ? null : current)
      } catch (error: unknown) {
        if (!cancelled) {
          setAgentEmailError(`Thread messages did not finish loading. ${toErrorMessage(error)}`)
        }
      } finally {
        if (!cancelled) {
          setAgentEmailThreadMessagesLoadingId((current) => current === threadId ? null : current)
        }
      }
    })()

    return () => {
      cancelled = true
    }
  }, [
    active,
    agentEmailApprovals,
    agentEmailConfigReady,
    agentEmailSelectedInbox?.address,
    agentEmailSelectedThread,
    callAgentEmailApi,
    page,
  ])

  useEffect(() => {
    if (!agentEmailSelectedThread) return
    const frame = window.requestAnimationFrame(() => {
      agentEmailMessagesEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [agentEmailSelectedThread])

  const unreadAgentEmailThreads = agentEmailThreads.filter((thread) => thread.unread).length
  const pendingAgentEmailApprovals = agentEmailApprovals.filter((approval) => approval.state === 'Pending').length
  const pendingGmailApprovals = gmailApprovals.filter((approval) => approval.status === 'pending').length
  const agentEmailSidebarAttentionCount =
    agentEmailConfigReady ? pendingAgentEmailApprovals + unreadAgentEmailThreads : 0
  const activeAgentEmailDomains = agentEmailDomains.filter((domain) => domain.status === 'Active').length
  const activeAgentEmailAgents = agentEmailAgents.filter((agent) => agent.status === 'Active').length
  const agentEmailHasData =
    agentEmailAgents.length > 0 ||
    agentEmailInboxes.length > 0 ||
    agentEmailDomains.length > 0 ||
    agentEmailApprovals.length > 0 ||
    agentEmailThreads.length > 0
  const agentEmailConnectionStatus = !gatewayConnected
    ? { tone: 'rose' as const, label: 'VM offline' }
    : agentEmailConfigSaving || agentEmailBackendLoading
      ? { tone: 'azure' as const, label: 'Syncing' }
      : !agentEmailConfigReady
        ? { tone: 'rose' as const, label: 'Not connected' }
      : agentEmailError && !agentEmailHasData
        ? { tone: 'gold' as const, label: 'Needs attention' }
          : agentEmailOrg && agentEmailBackendStatus?.connected
            ? { tone: 'mint' as const, label: `${agentEmailOrg.name} live` }
            : agentEmailBackendStatus?.healthy
              ? { tone: 'azure' as const, label: 'Connected' }
              : { tone: 'gold' as const, label: 'Needs attention' }

  const handleAgentEmailSaveConfig = useCallback(async () => {
    const nextBaseUrl = agentEmailBaseUrl.trim()
    const nextApiToken = agentEmailApiToken.trim()
    if (!nextBaseUrl || !nextApiToken) {
      setAgentEmailBanner({ tone: 'error', message: 'Add both a base URL and an API key.' })
      return
    }
    try {
      setAgentEmailConfigSaving(true)
      if (!window.cosmic?.saveGatewayAgentEmailConfig) {
        throw new Error('Gateway Agent Email bridge is unavailable.')
      }
      const rawStatus = await window.cosmic.saveGatewayAgentEmailConfig({
        baseUrl: nextBaseUrl,
        apiToken: nextApiToken,
      })
      const status = normalizeGatewayAgentEmailStatus(rawStatus)
      setAgentEmailBackendStatus(status)
      const syncedBaseUrl = status.base_url || nextBaseUrl
      const syncedApiToken = status.api_token || nextApiToken
      setAgentEmailBaseUrl(syncedBaseUrl)
      setAgentEmailApiToken(syncedApiToken)
      window.cosmic?.saveSetting(AGENT_EMAIL_SETTINGS_KEYS.baseUrl, syncedBaseUrl)
      window.cosmic?.saveSetting(AGENT_EMAIL_SETTINGS_KEYS.apiToken, syncedApiToken)
      await requestAgentEmailSnapshot(true)
      setAgentEmailBanner({ tone: 'success', message: 'Cosmic Mail connection synced to the VM.' })
    } catch (error: unknown) {
      setAgentEmailBanner({ tone: 'error', message: toErrorMessage(error) })
    } finally {
      setAgentEmailConfigSaving(false)
    }
  }, [agentEmailApiToken, agentEmailBaseUrl, requestAgentEmailSnapshot])

  const handleAgentEmailUseVmConfig = useCallback(async () => {
    if (!window.cosmic?.getGatewayAgentEmailDesktopConfig) {
      setAgentEmailBanner({
        tone: 'error',
        message: 'Gateway Agent Email bridge is unavailable.',
      })
      return
    }
    try {
      setAgentEmailConfigSaving(true)
      const desktopConfig = await window.cosmic.getGatewayAgentEmailDesktopConfig()
      if (!desktopConfig?.available || !desktopConfig.base_url || !desktopConfig.api_token) {
        setAgentEmailBanner({
          tone: 'info',
          message: 'No VM-provisioned Cosmic Mail config yet. Re-run bootstrap or use the manual form.',
        })
        return
      }
      const status = normalizeGatewayAgentEmailStatus(
        await window.cosmic.saveGatewayAgentEmailConfig({
          baseUrl: desktopConfig.base_url,
          apiToken: desktopConfig.api_token,
          primaryMailboxAddress: desktopConfig.primary_mailbox_address ?? null,
        }),
      )
      setAgentEmailBackendStatus(status)
      const syncedBaseUrl = status.base_url || desktopConfig.base_url
      const syncedApiToken = status.api_token || desktopConfig.api_token
      setAgentEmailBaseUrl(syncedBaseUrl)
      setAgentEmailApiToken(syncedApiToken)
      window.cosmic?.saveSetting(AGENT_EMAIL_SETTINGS_KEYS.baseUrl, syncedBaseUrl)
      window.cosmic?.saveSetting(AGENT_EMAIL_SETTINGS_KEYS.apiToken, syncedApiToken)
      await requestAgentEmailSnapshot(true)
      setAgentEmailBanner({
        tone: 'success',
        message: 'Connected via VM-provisioned Cosmic Mail org.',
      })
    } catch (error: unknown) {
      setAgentEmailBanner({ tone: 'error', message: toErrorMessage(error) })
    } finally {
      setAgentEmailConfigSaving(false)
    }
  }, [requestAgentEmailSnapshot])

  const handleAgentEmailDisconnect = useCallback(async () => {
    try {
      setAgentEmailConfigSaving(true)
      if (!window.cosmic?.clearGatewayAgentEmailConfig) {
        throw new Error('Gateway Agent Email bridge is unavailable.')
      }
      const rawStatus = await window.cosmic.clearGatewayAgentEmailConfig()
      const status = normalizeGatewayAgentEmailStatus(rawStatus)
      setAgentEmailBackendStatus(status)
      setAgentEmailBaseUrl('')
      setAgentEmailApiToken('')
      window.cosmic?.saveSetting(AGENT_EMAIL_SETTINGS_KEYS.baseUrl, '')
      window.cosmic?.saveSetting(AGENT_EMAIL_SETTINGS_KEYS.apiToken, '')
      setAgentEmailOrg(null)
      setAgentEmailChecklist([])
      setAgentEmailAgents([])
      setAgentEmailInboxes([])
      setAgentEmailThreads([])
      setAgentEmailDomains([])
      setAgentEmailApprovals([])
      setAgentEmailError(null)
      setAgentEmailBanner({ tone: 'success', message: 'Cosmic Mail disconnected from the VM.' })
    } catch (error: unknown) {
      setAgentEmailBanner({ tone: 'error', message: toErrorMessage(error) })
    } finally {
      setAgentEmailConfigSaving(false)
    }
  }, [])

  const handleAgentEmailSyncInbox = useCallback(async () => {
    if (!agentEmailSelectedInbox) return
    try {
      setAgentEmailSyncingInbox(true)
      const result = await callAgentEmailApi(`/mailboxes/${agentEmailSelectedInbox.id}/sync-inbox`, {
        method: 'POST',
        timeoutMs: 30000,
      }) as CosmicMailMailboxSyncResult
      await requestAgentEmailSnapshot(false)
      setAgentEmailBanner({
        tone: 'success',
        message: `Inbox synced. Imported ${result.imported_count} message${result.imported_count === 1 ? '' : 's'}.`,
      })
    } catch (error: unknown) {
      setAgentEmailBanner({ tone: 'error', message: toErrorMessage(error) })
    } finally {
      setAgentEmailSyncingInbox(false)
    }
  }, [agentEmailSelectedInbox, callAgentEmailApi, requestAgentEmailSnapshot])

  const handleAgentEmailDownloadAttachment = useCallback(async (attachment: CosmicMailAttachmentRead) => {
    if (!window.cosmic?.cosmicMailDownloadAttachment) {
      setAgentEmailBanner({ tone: 'error', message: 'Attachment download requires the desktop app bridge.' })
      return
    }
    try {
      setAgentEmailAttachmentActionId(attachment.id)
      const result = await window.cosmic.cosmicMailDownloadAttachment({
        baseUrl: agentEmailEffectiveBaseUrl,
        apiToken: agentEmailEffectiveApiToken,
        attachmentId: attachment.id,
        suggestedFilename: attachment.filename,
      })
      if (!result.cancelled) {
        setAgentEmailBanner({ tone: 'success', message: `Saved ${attachment.filename}.` })
      }
    } catch (error: unknown) {
      setAgentEmailBanner({ tone: 'error', message: toErrorMessage(error) })
    } finally {
      setAgentEmailAttachmentActionId(null)
    }
  }, [agentEmailEffectiveApiToken, agentEmailEffectiveBaseUrl])

  const handleAgentEmailReply = useCallback(async () => {
    if (!agentEmailSelectedThread || !agentEmailSelectedInbox) return
    const bodyText = agentEmailReplyDraft.trim()
    const hasFiles = agentEmailReplyAttachmentFiles.length > 0
    if (!bodyText && !hasFiles) return
    if (!agentEmailSelectedThread.messagesLoaded) {
      setAgentEmailBanner({ tone: 'info', message: 'Wait for the selected conversation to finish loading before replying.' })
      return
    }

    if (hasFiles) {
      const missingPath = agentEmailReplyAttachmentFiles.some((file) => !getElectronLocalFilePath(file))
      if (missingPath) {
        setAgentEmailBanner({
          tone: 'error',
          message: 'Could not read local file paths for attachments. Reattach files using the file picker (desktop app).',
        })
        return
      }
    }

    try {
      setAgentEmailReplySending(true)

      if (!hasFiles) {
        const result = await callAgentEmailApi(`/threads/${agentEmailSelectedThread.id}/reply`, {
          method: 'POST',
          body: {
            mailbox_id: agentEmailSelectedInbox.id,
            text_body: bodyText,
          },
          timeoutMs: 30000,
        }) as CosmicMailDraftSendResult
        setAgentEmailReplyDraft('')
        setAgentEmailComposerExpanded(false)
        await requestAgentEmailSnapshot(false)
        if (result.queued_for_approval) {
          setAgentEmailView('approvals')
          setAgentEmailBanner({ tone: 'info', message: 'Reply queued for approval.' })
        } else {
          setAgentEmailBanner({ tone: 'success', message: 'Reply sent.' })
        }
        return
      }

      if (!window.cosmic?.cosmicMailUploadDraftAttachment) {
        throw new Error('Attachment upload requires the Cosmic Mail desktop bridge.')
      }

      const paths = agentEmailReplyAttachmentFiles.map((file) => ({
        path: getElectronLocalFilePath(file) as string,
        name: file.name,
      }))

      const draftPayload = buildAgentEmailThreadReplyDraftPayload(
        agentEmailSelectedThread.threadSnapshot,
        agentEmailSelectedInbox.address,
        agentEmailSelectedThread.messagesSource,
        agentEmailReplyDraft,
      )

      const created = await callAgentEmailApi('/drafts', {
        method: 'POST',
        body: draftPayload,
        timeoutMs: 30000,
      }) as CosmicMailDraftRead

      for (const item of paths) {
        await window.cosmic.cosmicMailUploadDraftAttachment({
          baseUrl: agentEmailEffectiveBaseUrl,
          apiToken: agentEmailEffectiveApiToken,
          draftId: created.id,
          filePath: item.path,
          filename: item.name,
          timeoutMs: 120_000,
        })
      }

      const result = await callAgentEmailApi(`/drafts/${created.id}/send`, {
        method: 'POST',
        timeoutMs: 30000,
      }) as CosmicMailDraftSendResult

      setAgentEmailReplyDraft('')
      setAgentEmailReplyAttachmentFiles([])
      setAgentEmailComposerExpanded(false)
      await requestAgentEmailSnapshot(false)
      if (result.queued_for_approval) {
        setAgentEmailView('approvals')
        setAgentEmailBanner({ tone: 'info', message: 'Reply queued for approval.' })
      } else {
        setAgentEmailBanner({ tone: 'success', message: 'Reply sent.' })
      }
    } catch (error: unknown) {
      setAgentEmailBanner({ tone: 'error', message: toErrorMessage(error) })
    } finally {
      setAgentEmailReplySending(false)
    }
  }, [
    agentEmailEffectiveApiToken,
    agentEmailEffectiveBaseUrl,
    agentEmailReplyAttachmentFiles,
    agentEmailReplyDraft,
    agentEmailSelectedInbox,
    agentEmailSelectedThread,
    callAgentEmailApi,
    requestAgentEmailSnapshot,
  ])

  const handleAgentEmailCreateDomain = useCallback(async () => {
    const nextDomain = agentEmailDomainNameDraft.trim()
    if (!agentEmailOrg || !nextDomain) {
      setAgentEmailBanner({ tone: 'error', message: 'Add a domain name first.' })
      return
    }
    try {
      setAgentEmailCreatingDomain(true)
      const createdDomain = await callAgentEmailApi('/domains', {
        method: 'POST',
        body: {
          organization_id: agentEmailOrg.id,
          domain: nextDomain,
        },
        timeoutMs: 30000,
      }) as CosmicMailDomainRead
      setAgentEmailDomainNameDraft('')
      setAgentEmailSelectedDomainId(createdDomain.id)
      await requestAgentEmailSnapshot(false)
      setAgentEmailBanner({ tone: 'success', message: 'Domain added. Publish the DNS records to finish setup.' })
    } catch (error: unknown) {
      setAgentEmailBanner({ tone: 'error', message: toErrorMessage(error) })
    } finally {
      setAgentEmailCreatingDomain(false)
    }
  }, [agentEmailDomainNameDraft, agentEmailOrg, callAgentEmailApi, requestAgentEmailSnapshot])

  const handleAgentEmailVerifyDomain = useCallback(async () => {
    if (!agentEmailSelectedDomain) return
    try {
      setAgentEmailVerifyingDomain(true)
      const verification = await callAgentEmailApi(`/domains/${agentEmailSelectedDomain.id}/verify-dns`, {
        method: 'POST',
        timeoutMs: 30000,
      }) as CosmicMailDomainVerificationRead
      await requestAgentEmailSnapshot(false)
      setAgentEmailBanner({
        tone: verification.all_records_present ? 'success' : 'info',
        message: verification.all_records_present ? 'All DNS records are live.' : 'Some DNS records are still missing.',
      })
    } catch (error: unknown) {
      setAgentEmailBanner({ tone: 'error', message: toErrorMessage(error) })
    } finally {
      setAgentEmailVerifyingDomain(false)
    }
  }, [agentEmailSelectedDomain, callAgentEmailApi, requestAgentEmailSnapshot])

  const handleAgentEmailAddTrustedSender = useCallback(() => {
    const raw = agentEmailTrustedSenderDraft.trim()
    if (!raw) return
    if (!isPlausibleTrustedSenderEmail(raw)) {
      setAgentEmailBanner({ tone: 'error', message: 'Enter a valid email address.' })
      return
    }
    const key = raw.toLowerCase()
    if (agentEmailTrustedSenders.some((entry) => entry.toLowerCase() === key)) {
      setAgentEmailTrustedSenderDraft('')
      return
    }
    const next = [...agentEmailTrustedSenders, raw]
    setAgentEmailTrustedSenderDraft('')
    void syncAgentEmailTrustedSenders(next)
  }, [agentEmailTrustedSenderDraft, agentEmailTrustedSenders, syncAgentEmailTrustedSenders])

  const handleAgentEmailRemoveTrustedSender = useCallback((email: string) => {
    const next = agentEmailTrustedSenders.filter((entry) => entry !== email)
    void syncAgentEmailTrustedSenders(next)
  }, [agentEmailTrustedSenders, syncAgentEmailTrustedSenders])

  useEffect(() => {
    if (agentEmailDomains.length && !agentEmailMailboxDomainIdDraft) {
      setAgentEmailMailboxDomainIdDraft(agentEmailDomains[0].id)
    }
  }, [agentEmailDomains, agentEmailMailboxDomainIdDraft])

  useEffect(() => {
    if (agentEmailDomains.length && !agentEmailNewAgentDomainIdDraft) {
      setAgentEmailNewAgentDomainIdDraft(agentEmailDomains[0].id)
    }
  }, [agentEmailDomains, agentEmailNewAgentDomainIdDraft])

  const handleAgentEmailCreateMailbox = useCallback(async () => {
    const localPart = agentEmailMailboxLocalPartDraft.trim()
    const domainId = agentEmailMailboxDomainIdDraft
    if (!agentEmailOrg || !localPart || !domainId) {
      setAgentEmailBanner({ tone: 'error', message: 'Choose a domain and enter a local part (e.g. support).' })
      return
    }
    try {
      setAgentEmailCreatingMailbox(true)
      await callAgentEmailApi('/mailboxes', {
        method: 'POST',
        body: {
          organization_id: agentEmailOrg.id,
          domain_id: domainId,
          local_part: localPart,
          ...(agentEmailMailboxDisplayNameDraft.trim() ? { display_name: agentEmailMailboxDisplayNameDraft.trim() } : {}),
        },
        timeoutMs: 30000,
      })
      setAgentEmailMailboxLocalPartDraft('')
      setAgentEmailMailboxDisplayNameDraft('')
      await requestAgentEmailSnapshot(false)
      setAgentEmailBanner({ tone: 'success', message: 'Mailbox created.' })
    } catch (error: unknown) {
      setAgentEmailBanner({ tone: 'error', message: toErrorMessage(error) })
    } finally {
      setAgentEmailCreatingMailbox(false)
    }
  }, [
    agentEmailMailboxDisplayNameDraft,
    agentEmailMailboxDomainIdDraft,
    agentEmailMailboxLocalPartDraft,
    agentEmailOrg,
    callAgentEmailApi,
    requestAgentEmailSnapshot,
  ])

  const handleAgentEmailCreateNewAgent = useCallback(async () => {
    const name = agentEmailNewAgentNameDraft.trim()
    let slug = agentEmailNewAgentSlugDraft.trim().toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '')
    if (!slug && name) {
      slug = name
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '')
    }
    if (!agentEmailOrg || !name || !slug) {
      setAgentEmailBanner({ tone: 'error', message: 'Enter a display name and a slug (e.g. billing-agent).' })
      return
    }
    try {
      setAgentEmailCreatingNewAgent(true)
      await callAgentEmailApi('/agents', {
        method: 'POST',
        body: {
          organization_id: agentEmailOrg.id,
          name,
          slug,
          ...(agentEmailNewAgentDomainIdDraft ? { default_domain_id: agentEmailNewAgentDomainIdDraft } : {}),
        },
        timeoutMs: 30000,
      })
      setAgentEmailNewAgentNameDraft('')
      setAgentEmailNewAgentSlugDraft('')
      await requestAgentEmailSnapshot(false)
      setAgentEmailBanner({ tone: 'success', message: 'Agent created.' })
    } catch (error: unknown) {
      setAgentEmailBanner({ tone: 'error', message: toErrorMessage(error) })
    } finally {
      setAgentEmailCreatingNewAgent(false)
    }
  }, [
    agentEmailNewAgentDomainIdDraft,
    agentEmailNewAgentNameDraft,
    agentEmailNewAgentSlugDraft,
    agentEmailOrg,
    callAgentEmailApi,
    requestAgentEmailSnapshot,
  ])

  const handleAgentEmailApprove = useCallback(async (approvalId: string) => {
    try {
      setAgentEmailActionId(approvalId)
      await callAgentEmailApi(`/approvals/${approvalId}/approve`, {
        method: 'POST',
        timeoutMs: 30000,
      })
      await requestAgentEmailSnapshot(false)
      setAgentEmailBanner({ tone: 'success', message: 'Approval released and sent.' })
    } catch (error: unknown) {
      setAgentEmailBanner({ tone: 'error', message: toErrorMessage(error) })
    } finally {
      setAgentEmailActionId(null)
    }
  }, [callAgentEmailApi, requestAgentEmailSnapshot])

  const handleAgentEmailReject = useCallback(async (approvalId: string) => {
    try {
      setAgentEmailActionId(approvalId)
      await callAgentEmailApi(`/approvals/${approvalId}/reject`, {
        method: 'POST',
        body: { note: 'Rejected from Spaces.' },
        timeoutMs: 30000,
      })
      await requestAgentEmailSnapshot(false)
      setAgentEmailBanner({ tone: 'success', message: 'Approval rejected.' })
    } catch (error: unknown) {
      setAgentEmailBanner({ tone: 'error', message: toErrorMessage(error) })
    } finally {
      setAgentEmailActionId(null)
    }
  }, [callAgentEmailApi, requestAgentEmailSnapshot])

  const requestGmailApprovals = useCallback(async (showSpinner = false) => {
    if (!window.cosmic?.getGatewayGmailApprovals) {
      setGmailError('Gateway Gmail approval bridge is unavailable.')
      return
    }
    if (showSpinner) setGmailLoading(true)
    try {
      const raw = await window.cosmic.getGatewayGmailApprovals()
      const mapped = normalizeGmailApprovals(raw)
      setGmailApprovals(mapped)
      setGmailError(null)
      setGmailSelectedApprovalId((current) => {
        if (current && mapped.some((approval) => approval.id === current)) return current
        return mapped[0]?.id ?? null
      })
    } catch (error: unknown) {
      setGmailError(toErrorMessage(error))
    } finally {
      if (showSpinner) setGmailLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!active || page !== 'gmail' || !gatewayConnected) return
    void requestGmailApprovals(true)
    const timer = window.setInterval(() => {
      void requestGmailApprovals(false)
    }, 30000)
    return () => window.clearInterval(timer)
  }, [active, gatewayConnected, page, requestGmailApprovals])

  const handleGmailApprove = useCallback(async (approvalId: string) => {
    if (!window.cosmic?.approveGatewayGmailApproval) {
      setGmailBanner({ tone: 'error', message: 'Gateway Gmail approval bridge is unavailable.' })
      return
    }
    try {
      setGmailActionId(approvalId)
      await window.cosmic.approveGatewayGmailApproval({ approvalId })
      await requestGmailApprovals(false)
      setGmailBanner({ tone: 'success', message: 'Gmail draft approved and sent.' })
    } catch (error: unknown) {
      setGmailBanner({ tone: 'error', message: toErrorMessage(error) })
    } finally {
      setGmailActionId(null)
    }
  }, [requestGmailApprovals])

  const handleGmailReject = useCallback(async (approvalId: string) => {
    if (!window.cosmic?.rejectGatewayGmailApproval) {
      setGmailBanner({ tone: 'error', message: 'Gateway Gmail approval bridge is unavailable.' })
      return
    }
    try {
      setGmailActionId(approvalId)
      await window.cosmic.rejectGatewayGmailApproval({ approvalId, note: 'Rejected from Spaces Gmail.' })
      await requestGmailApprovals(false)
      setGmailBanner({ tone: 'success', message: 'Gmail draft rejected.' })
    } catch (error: unknown) {
      setGmailBanner({ tone: 'error', message: toErrorMessage(error) })
    } finally {
      setGmailActionId(null)
    }
  }, [requestGmailApprovals])

  const agentEmailViews: Array<{ id: AgentEmailViewId; label: string; kicker: string; signal: string; detail: string }> = [
    { id: 'overview', label: 'Overview', kicker: 'Command view', signal: 'Setup + health', detail: 'Start with readiness, the default Cosmic mail setup, and the current operating posture.' },
    { id: 'agents', label: 'Agent', kicker: '', signal: 'Default identity', detail: 'Default identity for outbound mail and inbox triage.' },
    { id: 'inboxes', label: 'Inbox', kicker: '', signal: `${unreadAgentEmailThreads} unread`, detail: '' },
    { id: 'approvals', label: 'Approvals', kicker: '', signal: `${pendingAgentEmailApprovals} waiting`, detail: '' },
    {
      id: 'settings',
      label: 'Settings',
      kicker: 'Cosmic API',
      signal: agentEmailConfigReady ? `${agentEmailDomains.length} domain · ${agentEmailConnectionStatus.label}` : agentEmailConnectionStatus.label,
      detail: 'Connection, sending domains, DNS verification, and console hosts such as thelearnchain.',
    },
  ]

  const agentEmailMetrics: Array<{ label: string; value: string; note: string; tone: MetricTone }> = [
    {
      label: 'Agent',
      value: String(agentEmailAgents.length).padStart(2, '0'),
      note: `${activeAgentEmailAgents} default identity active for the Cosmic org.`,
      tone: 'cool',
    },
    {
      label: 'Inbox',
      value: String(agentEmailInboxes.length).padStart(2, '0'),
      note: `${unreadAgentEmailThreads} unread conversations currently sitting in the primary inbox.`,
      tone: 'good',
    },
    {
      label: 'Domain',
      value: `${activeAgentEmailDomains}/${agentEmailDomains.length}`,
      note: 'The default sending domain is verified and ready for normal operation.',
      tone: 'muted',
    },
    {
      label: 'Approvals',
      value: String(pendingAgentEmailApprovals).padStart(2, '0'),
      note: 'Outbound currently paused for review, policy checks, or send-window timing.',
      tone: 'warm',
    },
  ]

  const currentPage = SPACE_PAGES.find((item) => item.id === page) || SPACE_PAGES[0]

  /* ── PAGE RENDERERS ───────────────────────────────────── */

  const renderToolsPage = () => {
    const visible = toolOpportunities.filter((item) => !['archived', 'declined'].includes(item.status))
    const liveCount = visible.filter((item) => item.status === 'live').length
    const buildingCount = visible.filter((item) => ['accepted', 'building'].includes(item.status)).length
    const suggestionCount = visible.filter((item) => ['candidate', 'suggested', 'deferred'].includes(item.status)).length
    return (
      <div className="spaces-page">
        <section className="spaces-banner tools-banner">
          <div className="tools-banner-copy">
            <div className="spaces-banner-kicker">Persistent interfaces</div>
            <h2 className="spaces-hero">Useful tools, shaped around your work.</h2>
            <p className="spaces-hero-copy">
              COSMIC can suggest and build focused sites, dashboards, trackers, and utilities. Optional materials improve a build, but do not block it.
            </p>
          </div>
          <div className="tools-banner-actions">
            <span className="tools-live-signal"><i />Continuously shaped by COSMIC</span>
            <button type="button" className="tools-refresh-btn" onClick={() => void refreshToolOpportunities()} disabled={toolOpportunitiesRefreshing}>
              <svg aria-hidden viewBox="0 0 24 24"><path d="M20 11a8.1 8.1 0 0 0-14.9-4L3 9m0 0V4m0 5h5m-4 4a8.1 8.1 0 0 0 14.9 4L21 15m0 0v5m0-5h-5" /></svg>
              {toolOpportunitiesRefreshing ? 'Refreshing' : 'Refresh'}
            </button>
          </div>
        </section>

        <section className="tools-summary-strip">
          <div><span>Suggestions</span><strong>{String(suggestionCount).padStart(2, '0')}</strong><i /></div>
          <div><span>Building</span><strong>{String(buildingCount).padStart(2, '0')}</strong><i /></div>
          <div><span>Live</span><strong>{String(liveCount).padStart(2, '0')}</strong><i /></div>
        </section>

        {toolOpportunitiesError ? <div className="tools-error">{toolOpportunitiesError}</div> : null}
        {!toolOpportunitiesRefreshing && visible.length === 0 ? (
          <section className="spaces-card tools-empty">
            <strong>No tool opportunities yet</strong>
            <p>COSMIC will add useful opportunities here as your projects and goals develop.</p>
          </section>
        ) : null}

        <section className="tools-grid">
          {visible.map((item) => {
            const isBusy = toolOpportunityActionId === item.opportunity_id
            const canBuild = ['candidate', 'suggested', 'deferred', 'accepted', 'failed'].includes(item.status)
            const isLive = item.status === 'live' && item.deployment_url
            return (
              <article key={item.opportunity_id} className={`tools-card status-${item.status}`}>
                <div className="tools-card-top">
                  <span className="tools-kind"><i />{item.tool_type}</span>
                  <span className="tools-status">{item.status}</span>
                </div>
                <h3>{item.title}</h3>
                <p className="tools-goal">{item.goal}</p>
                <p className="tools-reasoning">{item.reasoning}</p>
                {item.proposed_features?.length ? (
                  <div className="tools-feature-row">
                    {item.proposed_features.slice(0, 4).map((feature) => <span key={feature}>{feature}</span>)}
                  </div>
                ) : null}
                {item.helpful_materials?.length ? (
                  <div className="tools-materials">
                    <strong><span aria-hidden>+</span> Helpful, not required</strong>
                    <span>{item.helpful_materials.slice(0, 5).join(' · ')}</span>
                  </div>
                ) : null}
                <div className="tools-card-actions">
                  {isLive ? (
                    <button type="button" className="tools-primary" onClick={() => window.open(String(item.deployment_url), '_blank')}>Open</button>
                  ) : canBuild ? (
                    <button type="button" className="tools-primary" disabled={isBusy} onClick={() => void buildToolOpportunity(item.opportunity_id)}>
                      {isBusy ? 'Preparing' : item.status === 'accepted' ? 'Continue in chat' : 'Build now'}
                    </button>
                  ) : (
                    <button type="button" className="tools-primary" onClick={() => onPromptChat(`Continue working on My Tools opportunity ${item.opportunity_id}: ${item.title}.`)}>Continue in chat</button>
                  )}
                  {!['live', 'building'].includes(item.status) ? (
                    <button type="button" className="tools-secondary" disabled={isBusy} onClick={() => void updateToolOpportunityStatus(item.opportunity_id, 'declined')}>Not interested</button>
                  ) : null}
                  {item.status === 'live' ? (
                    <button type="button" className="tools-secondary" disabled={isBusy} onClick={() => void updateToolOpportunityStatus(item.opportunity_id, 'archived')}>Archive</button>
                  ) : null}
                  {item.repo_url ? (
                    <button type="button" className="tools-secondary" onClick={() => window.open(String(item.repo_url), '_blank')}>Repository</button>
                  ) : null}
                </div>
              </article>
            )
          })}
        </section>
      </div>
    )
  }

  const renderCommandPage = () => (
    <div className="spaces-page">
      <section className="spaces-banner">
        <div>
          <div className="spaces-banner-kicker">Space Control Center</div>
          <h2 className="spaces-hero">Your operating picture, one glance.</h2>
          <p className="spaces-hero-copy">
            What needs your attention, what COSMIC is actively running, and whether every system is healthy &mdash; all in one surface.
          </p>
        </div>
        <div className="spaces-banner-stack">
          <div className="spaces-mini-pill preview">Preview</div>
          <div className={`spaces-mini-pill ${gatewayStatus.tone}`}>{gatewayStatus.label}</div>
        </div>
      </section>

      <section className="spaces-overview-strip">
        {attentionMetrics.map((metric) => (
          <article key={metric.label} className={`spaces-overview-item ${metric.tone}`}>
            <div className="spaces-overview-label">{metric.label}</div>
            <div className="spaces-overview-value">{metric.value}</div>
            <div className="spaces-overview-note">{metric.note}</div>
          </article>
        ))}
      </section>

      <section className="spaces-command-columns">
        <article className="spaces-card spaces-operations-card">
          <div className="spaces-card-head">
            <div>
              <div className="spaces-card-kicker">Active operations</div>
              <h3>What COSMIC is working on</h3>
            </div>
          </div>
          <div className="spaces-operations-feed">
            {operations.map((op) => (
              <div key={op.title} className={`spaces-operation-item ${op.accent}`}>
                <div className="spaces-status-row">
                  <strong>{op.title}</strong>
                  <span className="spaces-status-chip">{op.status}</span>
                </div>
                <div className="spaces-inline-meta">
                  <span>{op.owner}</span>
                  <span>{op.channel}</span>
                </div>
                <p>{op.note}</p>
              </div>
            ))}
          </div>
        </article>

        <div className="spaces-quick-grid">
          {SPACE_PAGES.slice(1).map((item) => (
            <button
              key={item.id}
              type="button"
              className={`spaces-card spaces-quick-card ${item.accent}`}
              onClick={() => setPage(item.id)}
            >
              <span className="spaces-focus-icon">
                <SpacesNavIcon page={item.id} />
              </span>
              <div>
                <strong>{item.label}</strong>
                <p className="spaces-card-note">{item.kicker}</p>
              </div>
              <span className="spaces-focus-meta">{item.countLabel}</span>
            </button>
          ))}
        </div>
      </section>

      <section className="spaces-card spaces-posture-bar">
        <div className="spaces-card-kicker">System posture</div>
        <div className="spaces-topology">
          <div className="spaces-topology-node">Desktop</div>
          <div className="spaces-topology-link">Gateway</div>
          <div className="spaces-topology-link">Orchestrator</div>
          <div className="spaces-topology-link">Agents</div>
          <div className="spaces-topology-node">Memory</div>
        </div>
      </section>
    </div>
  )


  const renderCalendarEventDetail = (evt: CalendarAgendaEvent) => {
    const start = getCalendarEventStart(evt)
    const dayNum   = start ? start.getDate() : ''
    const monthName   = start ? start.toLocaleDateString([], { month: 'long' }) : ''
    const weekdayName = start ? start.toLocaleDateString([], { weekday: 'long' }) : ''
    const dateLabel   = start ? start.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' }) : ''
    const durationLabel = getEventDurationLabel(evt)
    const relativeLabel = getCalendarRelativeLabel(evt, now)
    const isPast = relativeLabel.startsWith('Ended') || relativeLabel === 'Just ended'
    const isLive = !isPast && (relativeLabel.includes('left') || relativeLabel.toLowerCase().startsWith('live'))
    const statusVariant = isPast ? 'past' : isLive ? 'live' : 'upcoming'
    const accent = googleColorToAccent(evt.colorId, evt.calendar_color)
    const attendees = evt.attendees ?? []

    return (
      <div className="spaces-page spaces-cal-page">
        <div className="cal-detail-split">

          {/* Left hero */}
          <div className={`cal-detail-hero accent-${accent}`}>
            <div className="cal-detail-hero-glow" />

            <div className="cal-detail-hero-inner">
              {/* Big date */}
              <div className="cal-detail-hero-date">
                <span className="cal-detail-hero-month">{monthName}</span>
                <span className="cal-detail-hero-daynum">{dayNum}</span>
                <span className="cal-detail-hero-weekday">{weekdayName}</span>
              </div>

              {/* Time */}
              <div className="cal-detail-hero-time">
                {evt.isAllDay ? (
                  <span className="cal-detail-hero-allday">All day</span>
                ) : (
                  <>
                    <span className="cal-detail-hero-timerange">{formatCalendarTime(evt.start, false)}</span>
                    <span className="cal-detail-hero-timearrow">→</span>
                    <span className="cal-detail-hero-timerange">{formatCalendarTime(evt.end, false)}</span>
                  </>
                )}
                {durationLabel && !evt.isAllDay && (
                  <span className="cal-detail-hero-dur">{durationLabel}</span>
                )}
              </div>

              {/* Status */}
              <span className={`cal-detail-hero-status ${statusVariant}`}>{relativeLabel}</span>
            </div>

            {/* Mini-month at base of hero */}
            <div className="cal-detail-mini">
              <div className="spaces-cal-mini-weekdays">
                {CALENDAR_WEEKDAYS.map((d) => (
                  <span key={d}>{d.charAt(0)}</span>
                ))}
              </div>
              <div className="spaces-cal-mini-grid">
                {calendarMonthCells.map((cell) => (
                  <button
                    key={cell.key}
                    type="button"
                    className={`spaces-cal-mini-day${cell.muted ? ' muted' : ''}${cell.isToday ? ' today' : ''}${cell.hasEvent ? ' has-event' : ''}`}
                    onClick={() => handleMiniDayClick(cell.date)}
                  >
                    {cell.label}
                  </button>
                ))}
              </div>
            </div>
          </div>

          {/* Right detail body */}
          <div className="cal-detail-body">
            {/* Back button + calendar name row */}
            <div className="cal-detail-topbar">
              <button
                type="button"
                className="cal-detail-back"
                onClick={() => setSelectedCalEvent(null)}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
                  <path d="M19 12H5" />
                  <path d="M12 19l-7-7 7-7" />
                </svg>
                Back
              </button>
              <div className="cal-detail-nav-cal">
                <span className={`cal-detail-nav-dot ${accent}`} />
                <span>{evt.calendar_name || 'Google Calendar'}</span>
              </div>
            </div>

            <h2 className="cal-detail-title">{evt.summary}</h2>

            {/* Divider after title */}
            <div className="cal-detail-rule" />

              {/* Meta rows */}
              <div className="cal-detail-metas">
                {/* Date & time row */}
                <div className="cal-detail-meta-row">
                  <svg className="cal-detail-meta-icon" width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M20 3h-1V1h-2v2H7V1H5v2H4c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 18H4V8h16v13z"/>
                  </svg>
                  <div className="cal-detail-meta-stack">
                    <span className="cal-detail-meta-primary">{dateLabel}</span>
                    {!evt.isAllDay && (
                      <span className="cal-detail-meta-secondary">
                        {formatCalendarTime(evt.start, false)}&nbsp;&ndash;&nbsp;{formatCalendarTime(evt.end, false)}
                        {durationLabel ? ` · ${durationLabel}` : ''}
                      </span>
                    )}
                  </div>
                </div>

                {/* Location */}
                {evt.location && (
                  <div className="cal-detail-meta-row">
                    <svg className="cal-detail-meta-icon" width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                      <path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5c-1.38 0-2.5-1.12-2.5-2.5S10.62 6.5 12 6.5s2.5 1.12 2.5 2.5S13.38 11.5 12 11.5z"/>
                    </svg>
                    <span className="cal-detail-meta-primary">{evt.location}</span>
                  </div>
                )}

                {/* Description */}
                {evt.description && (
                  <div className="cal-detail-meta-row cal-detail-meta-row--top">
                    <svg className="cal-detail-meta-icon" width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                      <path d="M14 17H4v2h10v-2zm6-8H4v2h16V9zM4 15h16v-2H4v2zM4 5v2h16V5H4z"/>
                    </svg>
                    <p className="cal-detail-desc">{evt.description}</p>
                  </div>
                )}

                {/* Organizer */}
                {evt.organizer && (
                  <div className="cal-detail-meta-row">
                    <svg className="cal-detail-meta-icon" width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                      <path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/>
                    </svg>
                    <div className="cal-detail-meta-stack">
                      <span className="cal-detail-meta-label">Organiser</span>
                      <span className="cal-detail-meta-primary">{evt.organizer}</span>
                    </div>
                  </div>
                )}

                {/* Attendees */}
                {attendees.length > 0 && (
                  <div className="cal-detail-meta-row cal-detail-meta-row--top">
                    <svg className="cal-detail-meta-icon" width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                      <path d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/>
                    </svg>
                    <div className="cal-detail-attendees">
                      <span className="cal-detail-meta-label">{attendees.length} attendee{attendees.length !== 1 ? 's' : ''}</span>
                      <div className="cal-detail-attendee-list">
                        {attendees.map((a) => (
                          <div key={a.email} className="cal-detail-attendee-row">
                            <span className={`cal-detail-attendee-dot status-${a.response_status}`} />
                            <span className="cal-detail-attendee-name">{a.display_name || a.email}</span>
                            {a.response_status && a.response_status !== 'accepted' && (
                              <span className={`cal-detail-attendee-badge status-${a.response_status}`}>{a.response_status}</span>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                )}
              </div>

              {/* Actions */}
              {(evt.meetingLink || evt.htmlLink) && (
                <div className="cal-detail-actions">
                  {evt.meetingLink && (
                    <button
                      type="button"
                      className="cal-detail-btn-join"
                      onClick={() => window.cosmic?.openExternal?.(evt.meetingLink)}
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M17 10.5V7c0-.55-.45-1-1-1H4c-.55 0-1 .45-1 1v10c0 .55.45 1 1 1h12c.55 0 1-.45 1-1v-3.5l4 4v-11l-4 4z"/>
                      </svg>
                      Join meeting
                    </button>
                  )}
                  {evt.htmlLink && (
                    <button
                      type="button"
                      className="cal-detail-btn-open"
                      onClick={() => window.cosmic?.openExternal?.(evt.htmlLink)}
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
                        <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 01-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" fill="#4285F4"/>
                        <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>
                        <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/>
                        <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>
                      </svg>
                      Open in Google
                    </button>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
    )
  }

  const renderCalendarGrid = (cols: { day: Date; events: WeekEvent[] }[], showNowLine: boolean, isDayView = false) => (
    <div className="spaces-cal-week">
      {/* Column headers */}
      <div className="spaces-cal-header-row" style={{ gridTemplateColumns: `48px repeat(${cols.length}, minmax(0,1fr))` }}>
        <div className="spaces-cal-gutter-head" />
        {cols.map(({ day }, i) => {
          const isToday = isSameCalendarDay(day, today)
          return (
            <div key={`col-${i}`} className={`spaces-cal-col-head${isToday && !isDayView ? ' today' : ''}`}>
              <span className={`spaces-cal-col-weekday${isToday ? ' today' : ''}`}>
                {day.toLocaleDateString(undefined, { weekday: 'short' }).toUpperCase()}
              </span>
              <span className={`spaces-cal-col-date${isToday ? ' today' : ''}`}>{day.getDate()}</span>
            </div>
          )
        })}
      </div>

      {/* All-day events row */}
      {allDayEvents.filter((e) => cols.some(({ day }) => { const s = getCalendarEventStart(e); return s && isSameCalendarDay(s, day) })).length > 0 && (
        <div className="spaces-cal-allday-row" style={{ gridTemplateColumns: `48px minmax(0,1fr)` }}>
          <div className="spaces-cal-allday-gutter">all day</div>
          <div className="spaces-cal-allday-cols" style={{ gridTemplateColumns: `repeat(${cols.length}, minmax(0,1fr))` }}>
            {cols.map(({ day }, i) => {
              const dayAllDay = allDayEvents.filter((e) => { const s = getCalendarEventStart(e); return s && isSameCalendarDay(s, day) })
              return (
                <div key={`allday-${i}`} className="spaces-cal-allday-col">
                  {dayAllDay.map((e) => (
                    <button
                      key={e.id}
                      type="button"
                      className={`spaces-cal-allday-chip ${googleColorToAccent(e.colorId, e.calendar_color)}`}
                      onClick={() => setSelectedCalEvent(e)}
                    >
                      {e.summary}
                    </button>
                  ))}
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Scrollable time grid */}
      <div className="spaces-cal-scroll">
        <div className="spaces-cal-grid" style={{ height: (CAL_LAST_HOUR - CAL_FIRST_HOUR) * CAL_HOUR_HEIGHT, gridTemplateColumns: `48px minmax(0,1fr)` }}>
          <div className="spaces-cal-gutter">
            {calendarHours.map((h) => (
              <div key={`g-${h}`} className="spaces-cal-gutter-label" style={{ top: (h - CAL_FIRST_HOUR) * CAL_HOUR_HEIGHT }}>
                {formatHour(h)}
              </div>
            ))}
          </div>
          <div className="spaces-cal-columns" style={{ gridTemplateColumns: `repeat(${cols.length}, minmax(0,1fr))` }}>
            {cols.map(({ day, events }, colIdx) => {
              const isToday = isSameCalendarDay(day, today)
              return (
                <div key={`col-${colIdx}`} className={`spaces-cal-column${isToday && !isDayView ? ' today' : ''}`}>
                  {calendarHours.map((h) => (
                    <div key={`lines-${h}`}>
                      <div className="spaces-cal-hour-line" style={{ top: (h - CAL_FIRST_HOUR) * CAL_HOUR_HEIGHT }} />
                      <div className="spaces-cal-half-line" style={{ top: (h - CAL_FIRST_HOUR) * CAL_HOUR_HEIGHT + CAL_HOUR_HEIGHT / 2 }} />
                    </div>
                  ))}
                  {events.map((evt) => {
                    const top = (evt.startHour - CAL_FIRST_HOUR + evt.startMinute / 60) * CAL_HOUR_HEIGHT
                    const height = (evt.durationMinutes / 60) * CAL_HOUR_HEIGHT
                    const endHour = evt.startHour + Math.floor((evt.startMinute + evt.durationMinutes) / 60)
                    const endMin = (evt.startMinute + evt.durationMinutes) % 60
                    // Find original CalendarAgendaEvent to open detail
                    const source = calendarData.events.find((e) => e.id === evt.id)
                    return (
                      <button
                        key={evt.id}
                        type="button"
                        className={`spaces-cal-event ${evt.accent}`}
                        style={{ top, height: Math.max(height, 24) }}
                        onClick={() => source && setSelectedCalEvent(source)}
                      >
                        <strong>{evt.title}</strong>
                        {height > 32 && (
                          <span className="spaces-cal-event-time">
                            {formatHour(evt.startHour)}:{String(evt.startMinute).padStart(2, '0')}
                            {' \u2013 '}
                            {formatHour(endHour)}:{String(endMin).padStart(2, '0')}
                          </span>
                        )}
                      </button>
                    )
                  })}
                  {showNowLine && isToday && nowLineTop !== null && (
                    <div className="spaces-cal-now-line" style={{ top: nowLineTop }}>
                      <div className="spaces-cal-now-dot" />
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </div>
  )

  const renderCalendarPage = () => {
    if (selectedCalEvent) return renderCalendarEventDetail(selectedCalEvent)

    const isWeekView = calView === 'week'
    const viewCols = isWeekView
      ? weekDays.map((day, i) => ({ day, events: weekEvents.filter((e) => e.dayIndex === i) }))
      : [{ day: dayViewDate, events: weekEvents.filter((e) => isSameCalendarDay(addDays(weekStart, e.dayIndex), dayViewDate)) }]

    const handlePrev = () => {
      if (isWeekView) setCalWeekOffset((o) => o - 1)
      else setCalDayOffset((o) => o - 1)
    }
    const handleNext = () => {
      if (isWeekView) setCalWeekOffset((o) => o + 1)
      else setCalDayOffset((o) => o + 1)
    }
    const handleToday = () => {
      setCalWeekOffset(0)
      setCalDayOffset(0)
    }

    const rangeLabel = isWeekView
      ? weekRangeLabel
      : dayViewDate.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })

    return (
      <div className="spaces-page spaces-cal-page">
        <section className="spaces-cal-layout">
          {/* ── Sidebar ─────────────────────────────── */}
          <aside className="spaces-cal-sidebar">
            <div className="spaces-cal-mini-month">
              <div className="spaces-cal-mini-head">
                <span className="spaces-cal-mini-title">{monthLabel}</span>
                <div className="spaces-cal-mini-arrows">
                  <button type="button" className="spaces-cal-mini-arrow" onClick={() => setCalWeekOffset((o) => o - 4)} aria-label="Previous month">&lsaquo;</button>
                  <button type="button" className="spaces-cal-mini-arrow" onClick={() => setCalWeekOffset((o) => o + 4)} aria-label="Next month">&rsaquo;</button>
                </div>
              </div>
              <div className="spaces-cal-mini-weekdays">
                {CALENDAR_WEEKDAYS.map((day) => (
                  <span key={day}>{day.charAt(0)}</span>
                ))}
              </div>
              <div className="spaces-cal-mini-grid">
                {calendarMonthCells.map((cell) => (
                  <button
                    key={cell.key}
                    type="button"
                    className={`spaces-cal-mini-day${cell.muted ? ' muted' : ''}${cell.isToday ? ' today' : ''}${cell.hasEvent ? ' has-event' : ''}`}
                    onClick={() => handleMiniDayClick(cell.date)}
                  >
                    {cell.label}
                  </button>
                ))}
              </div>
            </div>

            {/* Up next */}
            <div className="spaces-cal-section">
              <span className="spaces-cal-section-label">Up next</span>
              {todayAgendaEvents.length > 0 ? (
                <div className="spaces-cal-upnext">
                  {todayAgendaEvents.slice(0, 3).map((evt) => (
                    <button
                      key={evt.id}
                      type="button"
                      className={`spaces-cal-upnext-item ${googleColorToAccent(evt.colorId, evt.calendar_color)}`}
                      onClick={() => setSelectedCalEvent(evt)}
                    >
                      <div className="spaces-cal-upnext-accent" />
                      <div className="spaces-cal-upnext-body">
                        <strong>{evt.summary}</strong>
                        <span>{evt.isAllDay ? 'All day' : formatCalendarRange(evt)}</span>
                      </div>
                    </button>
                  ))}
                </div>
              ) : (
                <div className="spaces-cal-upnext-empty">
                  {calendarHasAccounts ? 'Nothing left today' : 'No calendar connected'}
                </div>
              )}
            </div>

            {/* Calendars */}
            <div className="spaces-cal-section">
              <span className="spaces-cal-section-label">My calendars</span>
              <div className="spaces-cal-categories">
                {calendarNames.length > 0 ? calendarNames.map((cal) => (
                  <label key={cal.name} className="spaces-cal-category">
                    <span className={`spaces-cal-dot ${cal.accent}`} />
                    {cal.name}
                  </label>
                )) : (
                  <label className="spaces-cal-category"><span className="spaces-cal-dot azure" />Google Calendar</label>
                )}
              </div>
            </div>
          </aside>

          {/* ── Main calendar ───────────────────────── */}
          <div className="spaces-cal-main">
            <div className="spaces-cal-toolbar">
              <button type="button" className="spaces-cal-today-btn" onClick={handleToday}>Today</button>
              <button type="button" className="spaces-cal-arrow" onClick={handlePrev} aria-label="Previous">&lsaquo;</button>
              <button type="button" className="spaces-cal-arrow" onClick={handleNext} aria-label="Next">&rsaquo;</button>
              <span className="spaces-cal-range">{rangeLabel}</span>
              <div className="spaces-cal-toolbar-right">
                {!calendarHasAccounts ? (
                  <button type="button" className="spaces-cal-connect-btn" onClick={() => window.cosmic?.connectGoogleAccount?.({})}>
                    Connect Google Calendar
                  </button>
                ) : (
                  <button
                    type="button"
                    className={`spaces-cal-refresh-btn ${calendarRefreshing ? 'spinning' : ''}`}
                    onClick={() => requestCalendarAgenda(true)}
                    title="Refresh calendar"
                    aria-label="Refresh calendar"
                  >
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor">
                      <path d="M17.65 6.35A7.958 7.958 0 0 0 12 4C7.58 4 4 7.58 4 12s3.58 8 8 8 8-3.58 8-8h-2c0 3.31-2.69 6-6 6s-6-2.69-6-6 2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35Z"/>
                    </svg>
                  </button>
                )}
                <div className="spaces-cal-view-toggle">
                  <button
                    type="button"
                    className={`spaces-cal-view-btn ${calView === 'day' ? 'active' : ''}`}
                    onClick={() => setCalView('day')}
                  >Day</button>
                  <button
                    type="button"
                    className={`spaces-cal-view-btn ${calView === 'week' ? 'active' : ''}`}
                    onClick={() => setCalView('week')}
                  >Week</button>
                </div>
              </div>
            </div>

            {!calendarHasAccounts && calendarData.state !== 'idle' && (
              <div className="spaces-cal-connect-prompt">
                <p>Connect your Google Calendar to see real events here.</p>
                <button type="button" className="spaces-cal-connect-btn" onClick={() => window.cosmic?.connectGoogleAccount?.({})}>
                  Connect Google Calendar
                </button>
              </div>
            )}

            {renderCalendarGrid(viewCols, true, !isWeekView)}
          </div>
        </section>
      </div>
    )
  }

  const prophetEditionDate = useMemo(
    () => today.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' }),
    [today],
  )

  const prophetSections = useMemo(() => {
    const order: ProphetSection[] = ['breaking', 'tech', 'markets', 'social', 'science']
    return order
      .map((s) => ({ key: s, label: PROPHET_SECTION_LABELS[s], articles: PROPHET_ARTICLES.filter((a) => a.section === s && !a.featured) }))
      .filter((s) => s.articles.length > 0)
  }, [])

  const renderProphetPage = () => {
    const leadArticle = PROPHET_ARTICLES.find((a) => a.featured)!

    return (
      <div className="spaces-page prophet-page">
        {/* Masthead */}
        <header className="prophet-masthead">
          <div className="prophet-rule prophet-rule-thick" />
          <div className="prophet-masthead-inner">
            <div className="prophet-edition">
              <span className="prophet-edition-vol">Vol. MMXXVI</span>
              <span className="prophet-edition-dot" />
              <span>No. {today.getDate()}</span>
              <span className="prophet-edition-dot" />
              <span className="prophet-price">Price: Five Knuts</span>
            </div>
            <h1 className="prophet-title">The Daily Prophet</h1>
            <p className="prophet-subtitle">Curated intelligence for the informed operator</p>
            <div className="prophet-dateline">
              <span>{prophetEditionDate}</span>
              <span className="prophet-edition-dot" />
              <span>COSMIC Edition</span>
              <span className="prophet-edition-dot" />
              <span>Proprietor: COSMIC Systems</span>
            </div>
          </div>
          <div className="prophet-rule prophet-rule-thick" />
        </header>

        {/* Lead Story — full width above the fold */}
        <section className="prophet-lead">
          <div className="prophet-lead-label">Breaking Dispatch</div>
          <h2 className="prophet-lead-headline">{leadArticle.headline}</h2>
          <div className="prophet-lead-byline">
            <em>By special correspondent</em>
            <span className="prophet-byline-dot" />
            <span>{leadArticle.source}</span>
            <span className="prophet-byline-dot" />
            <span>{leadArticle.timeAgo}</span>
          </div>
          <p className="prophet-lead-body">{leadArticle.summary}</p>
          <p className="prophet-lead-cont">Continued on Page 3, Column IV</p>
        </section>

        <div className="prophet-double-rule" />

        {/* Magazine well — editorial grids; cosmic page BG unchanged */}
        <div className="prophet-body">
          <div className="prophet-magazine">
            {prophetSections.map((section) => {
              const n = section.articles.length
              const storiesClass =
                n <= 1 ? 'prophet-section-stories prophet-section-stories--n1' :
                n === 2 ? 'prophet-section-stories prophet-section-stories--n2' :
                'prophet-section-stories prophet-section-stories--n3'
              return (
                <section key={section.key} className={`prophet-section prophet-section--${section.key}`}>
                  <div className="prophet-section-head">
                    <span className="prophet-section-ornament" aria-hidden>
                      ◆
                    </span>
                    <h2 className="prophet-section-title">{section.label}</h2>
                    <div className="prophet-section-rule" />
                    <span className="prophet-section-ornament" aria-hidden>
                      ◆
                    </span>
                  </div>
                  <div className={storiesClass}>
                    {section.articles.map((article, index) => {
                      const isFeature = n >= 3 && index === 0
                      return (
                        <article
                          key={article.id}
                          className={`prophet-story prophet-story--accent-${article.accent} ${isFeature ? 'prophet-story--feature' : ''}`}
                        >
                          <h3 className="prophet-story-headline">{article.headline}</h3>
                          <p className="prophet-story-body">{article.summary}</p>
                          <div className="prophet-story-byline">
                            <span>{article.source}</span>
                            <span className="prophet-byline-dot" />
                            <span>{article.timeAgo}</span>
                          </div>
                        </article>
                      )
                    })}
                  </div>
                </section>
              )
            })}
          </div>
        </div>

        <footer className="prophet-footer">
          <div className="prophet-rule prophet-rule-thick" />
          <p>Assembled by COSMIC &middot; Sources verified against your trusted feeds &middot; All times local</p>
        </footer>
      </div>
    )
  }

  const renderAutopilotPage = () => (
    <div className="spaces-page">
      <section className="spaces-section-copy">
        <div className="spaces-card-kicker">Autopilot</div>
        <h2>Routines should feel like controlled flight, not hidden timers.</h2>
        <p>
          Everything COSMIC runs on its own &mdash; scheduled deliveries, maintenance loops, background processes, and future multi-channel automations. Each routine carries its own context, delivery target, and timezone so it never depends on chat continuity alone.
        </p>
      </section>

      <section className="spaces-orbit-grid">
        {cronCards.map((cron) => (
          <article key={cron.label} className="spaces-card spaces-cron-card">
            <div className="spaces-cron-top">
              <div>
                <div className="spaces-card-kicker">{cron.label}</div>
                <h3>{cron.schedule}</h3>
              </div>
              <span className={`spaces-state-pill ${cron.state}`}>{cron.state}</span>
            </div>
            <div className="spaces-cron-meta">
              <div>
                <span>Channel</span>
                <strong>{cron.channel}</strong>
              </div>
              <div>
                <span>Timezone</span>
                <strong>{cron.timezone}</strong>
              </div>
            </div>
            <p className="spaces-cron-note">{cron.note}</p>
          </article>
        ))}
      </section>

      <section className="spaces-two-column">
        <article className="spaces-card">
          <div className="spaces-card-head">
            <div>
              <div className="spaces-card-kicker">Routine contract</div>
              <h3>Every automation should carry these fields</h3>
            </div>
          </div>
          <ul className="spaces-checklist">
            <li>Concrete delivery target resolved by Gateway, defaulting to the incoming channel unless you explicitly change it.</li>
            <li>Authoritative timezone snapshot so &quot;tomorrow at 6&quot; means your local 6 AM, not the VM&apos;s clock.</li>
            <li>Durable context summary so long-delay or recurring jobs do not depend on chat continuity alone.</li>
            <li>Future controls for pause, resume, edit, and operator inspection without forcing you back through natural-language prompts.</li>
          </ul>
        </article>

        <article className="spaces-card">
          <div className="spaces-card-head">
            <div>
              <div className="spaces-card-kicker">Background processes</div>
              <h3>Quiet work running outside your hot path</h3>
            </div>
          </div>
          <div className="spaces-operations-feed">
            {backgroundProcesses.map((proc) => (
              <div key={proc.title} className={`spaces-operation-item ${proc.accent}`}>
                <div className="spaces-status-row">
                  <strong>{proc.title}</strong>
                  <span className="spaces-status-chip">{proc.status}</span>
                </div>
                <div className="spaces-inline-meta">
                  <span>{proc.owner}</span>
                  <span>{proc.channel}</span>
                </div>
                <p>{proc.note}</p>
              </div>
            ))}
          </div>
        </article>
      </section>

      <section className="spaces-card">
        <div className="spaces-card-head">
          <div>
            <div className="spaces-card-kicker">Timeline bridge</div>
            <h3>See when these routines land on your week.</h3>
          </div>
        </div>
        <p className="spaces-card-note">
          Autopilot defines the rules. My Calendar shows where those rules touch your actual timeline &mdash; so you can see if an automation conflicts with a focus block or meeting.
        </p>
        <div className="spaces-chip-row">
          <span className="spaces-chip">Cron truth</span>
          <span className="spaces-chip">Timezone aware</span>
          <span className="spaces-chip">Channel delivery</span>
        </div>
        <button type="button" className="spaces-surface-btn" onClick={() => setPage('calendar')}>
          Open my calendar
        </button>
      </section>
    </div>
  )

  const renderPulsePage = () => (
    <div className="spaces-page">
      <section className="spaces-section-copy">
        <div className="spaces-card-kicker">Pulse</div>
        <h2>Every system COSMIC depends on, one health check.</h2>
        <p>
          Provider usage, system connectivity, storage health, and the data flows that keep the assistant running. This is where real diagnostics will land once backend feeds are wired.
        </p>
      </section>

      <section className="spaces-observatory-grid">
        {providerCards.map((card) => (
          <article key={card.label} className={`spaces-card spaces-observatory-card ${card.accent}`}>
            <div className="spaces-card-kicker">{card.label}</div>
            <div className="spaces-observatory-value">{card.value}</div>
            <div className="spaces-card-note">{card.detail}</div>
          </article>
        ))}
      </section>

      <section className="spaces-two-column">
        <article className="spaces-card">
          <div className="spaces-card-head">
            <div>
              <div className="spaces-card-kicker">System map</div>
              <h3>How data flows through COSMIC</h3>
            </div>
          </div>
          <div className="spaces-topology">
            <div className="spaces-topology-node">Desktop</div>
            <div className="spaces-topology-link">Gateway</div>
            <div className="spaces-topology-link">Orchestrator</div>
            <div className="spaces-topology-link">Redis bus</div>
            <div className="spaces-topology-node">Memory</div>
          </div>
          <div className="spaces-mesh-list">
            {meshEvents.map((event) => (
              <div key={`${event.from}-${event.to}-${event.type}`} className={`spaces-mesh-event ${event.tone}`}>
                <div className="spaces-mesh-flow">
                  <span>{event.from}</span>
                  <strong>{event.to}</strong>
                </div>
                <div className="spaces-mesh-copy">
                  <div className="spaces-mesh-type">{event.type}</div>
                  <p>{event.note}</p>
                </div>
              </div>
            ))}
          </div>
        </article>

        <article className="spaces-card">
          <div className="spaces-card-head">
            <div>
              <div className="spaces-card-kicker">Storage map</div>
              <h3>Where the system truth lives</h3>
            </div>
          </div>
          <div className="spaces-storage-grid">
            <div className="spaces-storage-card">
              <strong>Gateway SQLite</strong>
              <p>Sessions, scheduler, routing audit, memory write audit, and usage ledger.</p>
            </div>
            <div className="spaces-storage-card">
              <strong>Canonical memory</strong>
              <p>Markdown files plus registry rows remain the durable memory source of truth.</p>
            </div>
            <div className="spaces-storage-card">
              <strong>Qdrant + Neo4j</strong>
              <p>Passive recall vectors and persistent graph intelligence stay off the user hot path.</p>
            </div>
            <div className="spaces-storage-card">
              <strong>Redis bus</strong>
              <p>Agent dispatch, events, liveness, and future relay inspection all converge here.</p>
            </div>
          </div>
        </article>
      </section>

      <section className="spaces-card">
        <div className="spaces-card-head">
          <div>
            <div className="spaces-card-kicker">Wiring priorities</div>
            <h3>Backend feeds to connect first</h3>
          </div>
        </div>
        <ul className="spaces-checklist">
          <li>Gateway usage ledger and routing audit with per-call drill-down and cost visibility.</li>
          <li>Scheduler list and execution history so Autopilot stops being a static sketch.</li>
          <li>Agent registry health, child-task traces, and Redis event summaries for the system map.</li>
          <li>Memory graph queue posture and recent adjudication outcomes so operators can trust the memory system.</li>
        </ul>
      </section>
    </div>
  )

  const renderAgentEmailPage = () => renderAgentEmailPageMinimal()

  const renderAgentEmailPageMinimal = () => {
    const currentAgentEmailTab = agentEmailViews.find((item) => item.id === agentEmailView) || agentEmailViews[0]
    const orgName = agentEmailOrg?.name || 'Cosmic'
    const selectedAgentName = agentEmailSelectedAgent?.name || 'Cosmic Agent'
    const selectedInboxName = agentEmailSelectedInbox?.address || 'Primary inbox'
    const primaryDomainLabel = agentEmailSelectedDomain?.name || agentEmailDomains[0]?.name || 'No domain linked'

    const shortenAgentEmailUrl = (value: string, max = 42) => {
      const t = value.trim()
      if (!t) return '—'
      if (t.length <= max) return t
      return `${t.slice(0, Math.max(0, max - 1))}…`
    }

    const renderAgentEmailConnectionBanners = () => (
      <>
        {agentEmailBanner ? (
          <div className={`agent-email-banner ${agentEmailBanner.tone}`}>{agentEmailBanner.message}</div>
        ) : null}
        {agentEmailError && agentEmailHasData ? (
          <div className="agent-email-banner warning">{agentEmailError}</div>
        ) : null}
      </>
    )

    const renderAgentEmailEmptyState = (
      title: string,
      description: string,
      actionLabel?: string,
      onAction?: () => void,
    ) => (
      <div className="agent-email-minimal-empty">
        <strong>{title}</strong>
        <p>{description}</p>
        {actionLabel && onAction ? (
          <button type="button" className="agent-email-console-primary agent-email-empty-action" onClick={onAction}>
            {actionLabel}
          </button>
        ) : null}
      </div>
    )

    const renderAgentEmailConnectionPanel = () => (
      <section className="agent-email-connection-card agent-email-connection-card--settings">
        <div className="agent-email-connection-head">
          <div>
            <div className="spaces-card-kicker">Cosmic Mail API</div>
            <h3>{agentEmailOrg ? `${agentEmailOrg.name}` : 'Connection'}</h3>
            <p className="agent-email-connection-lead">Base URL and org API key for your Cosmic Mail control plane.</p>
          </div>
          <div className="agent-email-connection-actions">
            <span className={`agent-email-minimal-pill ${agentEmailConnectionStatus.tone}`}>{agentEmailConnectionStatus.label}</span>
            <button
              type="button"
              className="agent-email-console-secondary"
              onClick={() => void ensureAgentEmailBackendConnection(true)}
              disabled={!gatewayConnected || agentEmailLoading || agentEmailBackendLoading}
            >
              {agentEmailLoading || agentEmailBackendLoading ? 'Refreshing…' : 'Refresh'}
            </button>
            <button
              type="button"
              className="agent-email-console-secondary"
              onClick={() => void handleAgentEmailDisconnect()}
              disabled={!agentEmailBackendConfigured || agentEmailConfigSaving}
            >
              Disconnect
            </button>
          </div>
        </div>

        {renderAgentEmailConnectionBanners()}

        <div className="agent-email-form-grid">
          <label className="agent-email-form-field">
            <span>Base URL</span>
            <input
              className="agent-email-form-input"
              type="text"
              value={agentEmailBaseUrl}
              placeholder="https://console.thelearnchain.com"
              onChange={(event) => setAgentEmailBaseUrl(event.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
          </label>

          <label className="agent-email-form-field">
            <span>API key</span>
            <input
              className="agent-email-form-input"
              type="password"
              value={agentEmailApiToken}
              placeholder="Paste a Cosmic Mail org or admin key"
              onChange={(event) => setAgentEmailApiToken(event.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
          </label>

          <div className="agent-email-form-field agent-email-form-actions">
            <span>Actions</span>
            <div className="agent-email-form-action-row">
              <button
                type="button"
                className="agent-email-console-primary"
                onClick={() => void handleAgentEmailUseVmConfig()}
                disabled={agentEmailConfigSaving || !gatewayConnected}
                title="Use the Cosmic Mail org that bootstrap already provisioned for this VM"
              >
                {agentEmailConfigSaving ? 'Connecting…' : 'Use VM-provisioned config'}
              </button>
              <button
                type="button"
                className="agent-email-console-secondary"
                onClick={() => void handleAgentEmailSaveConfig()}
                disabled={agentEmailConfigSaving || !gatewayConnected}
              >
                {agentEmailConfigSaving ? 'Saving…' : 'Save connection'}
              </button>
            </div>
          </div>
        </div>
      </section>
    )

    const renderAgentEmailDomainsSection = () => (
      <div className="agent-email-settings-domains">
        <section className="agent-email-settings-domain-add" aria-label="Add domain">
          <div className="agent-email-settings-domain-add-text">
            <h4 className="agent-email-settings-section-title">Add a sending domain</h4>
            <p className="agent-email-settings-section-lead">Creates the domain in Cosmic Mail; you publish DNS at your provider.</p>
          </div>
          <div className="agent-email-settings-domain-add-row">
            <input
              className="agent-email-form-input agent-email-settings-domain-add-input"
              type="text"
              value={agentEmailDomainNameDraft}
              placeholder="mail.example.com"
              onChange={(event) => setAgentEmailDomainNameDraft(event.target.value)}
              autoComplete="off"
              spellCheck={false}
              aria-label="Domain name"
            />
            <button
              type="button"
              className="agent-email-console-primary agent-email-settings-domain-add-btn"
              onClick={() => void handleAgentEmailCreateDomain()}
              disabled={agentEmailCreatingDomain || !agentEmailDomainNameDraft.trim() || !agentEmailOrg}
            >
              {agentEmailCreatingDomain ? 'Linking…' : 'Link'}
            </button>
          </div>
        </section>

        <div className="agent-email-settings-domain-split">
          <div className="agent-email-settings-domain-list-panel">
            <div className="agent-email-settings-panel-label">Your domains</div>
            {agentEmailDomains.length ? (
              <div className="agent-email-console-table-wrap agent-email-settings-domain-table-wrap">
                <table className="agent-email-console-table agent-email-settings-domain-table">
                  <thead>
                    <tr>
                      <th>Domain</th>
                      <th>Status</th>
                      <th>Mailboxes</th>
                    </tr>
                  </thead>
                  <tbody>
                    {agentEmailDomains.map((domain) => (
                      <tr
                        key={domain.id}
                        className={agentEmailSelectedDomain?.id === domain.id ? 'active' : ''}
                        onClick={() => setAgentEmailSelectedDomainId(domain.id)}
                      >
                        <td data-label="Domain"><strong>{domain.name}</strong></td>
                        <td data-label="Status"><span className={`agent-email-minimal-pill ${domain.accent}`}>{domain.status}</span></td>
                        <td data-label="Mailboxes">{domain.mailboxes}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              renderAgentEmailEmptyState('No domain yet', 'Link a domain above to generate DNS records.')
            )}
          </div>

          <div className="agent-email-settings-domain-detail-panel">
            <div className="agent-email-settings-panel-label">Details</div>
            {agentEmailSelectedDomain ? (
              <article className="agent-email-console-card agent-email-settings-domain-detail-card">
                <div className="agent-email-console-card-head agent-email-settings-domain-detail-head">
                  <div>
                    <h4>{agentEmailSelectedDomain.name}</h4>
                    <p className="agent-email-settings-domain-detail-provider">{agentEmailSelectedDomain.provider}</p>
                  </div>
                  <div className="agent-email-settings-domain-detail-actions">
                    <span className={`agent-email-minimal-pill ${agentEmailSelectedDomain.accent}`}>{agentEmailSelectedDomain.status}</span>
                    <button
                      type="button"
                      className="agent-email-console-primary agent-email-settings-verify-btn"
                      onClick={() => void handleAgentEmailVerifyDomain()}
                      disabled={agentEmailVerifyingDomain}
                    >
                      {agentEmailVerifyingDomain ? 'Verifying…' : 'Verify DNS'}
                    </button>
                  </div>
                </div>
                <div className="agent-email-console-detail-rows">
                  <div className="agent-email-console-detail-row"><span>DNS posture</span><strong>{agentEmailSelectedDomain.dns}</strong></div>
                  <div className="agent-email-console-detail-row"><span>Reputation</span><strong>{agentEmailSelectedDomain.reputation}</strong></div>
                  <div className="agent-email-console-detail-row"><span>Records</span><strong>{agentEmailSelectedDomain.records.length}</strong></div>
                </div>
                <div className="agent-email-console-records">
                  {agentEmailSelectedDomain.records.length ? agentEmailSelectedDomain.records.map((record) => (
                    <div key={`${record.label}-${record.value}`} className="agent-email-console-record">
                      <div>
                        <strong>{record.label}</strong>
                        <p>{record.value}</p>
                      </div>
                      <span>{record.status}</span>
                    </div>
                  )) : renderAgentEmailEmptyState('No DNS rows yet', 'Records appear after the domain is provisioned.')}
                </div>
                {agentEmailSelectedDomain.note ? (
                  <div className="agent-email-console-text-block agent-email-settings-domain-notes">
                    <span>Notes</span>
                    <p>{agentEmailSelectedDomain.note}</p>
                  </div>
                ) : null}
              </article>
            ) : (
              <div className="agent-email-settings-domain-detail-placeholder">
                {renderAgentEmailEmptyState('Select a domain', 'Pick a row on the left to view DNS and verification.')}
              </div>
            )}
          </div>
        </div>
      </div>
    )

    const renderAgentEmailMailboxesSection = () => (
      <div className="agent-email-settings-domains">
        <section className="agent-email-settings-domain-add" aria-label="Add mailbox">
          <div className="agent-email-settings-domain-add-text">
            <h4 className="agent-email-settings-section-title">Add an inbox</h4>
            <p className="agent-email-settings-section-lead">Creates a mailbox on a verified domain via Cosmic Mail.</p>
          </div>
          <div className="agent-email-settings-provision-grid">
            <label className="agent-email-form-field">
              <span>Domain</span>
              <select
                className="agent-email-form-input"
                value={agentEmailMailboxDomainIdDraft}
                onChange={(event) => setAgentEmailMailboxDomainIdDraft(event.target.value)}
                aria-label="Domain for new mailbox"
              >
                {agentEmailDomains.length ? agentEmailDomains.map((d) => (
                  <option key={d.id} value={d.id}>{d.name}</option>
                )) : (
                  <option value="">No domain linked</option>
                )}
              </select>
            </label>
            <label className="agent-email-form-field">
              <span>Local part</span>
              <input
                className="agent-email-form-input"
                type="text"
                value={agentEmailMailboxLocalPartDraft}
                placeholder="support"
                onChange={(event) => setAgentEmailMailboxLocalPartDraft(event.target.value)}
                autoComplete="off"
                spellCheck={false}
                aria-label="Local part before @"
              />
            </label>
            <label className="agent-email-form-field">
              <span>Display name (optional)</span>
              <input
                className="agent-email-form-input"
                type="text"
                value={agentEmailMailboxDisplayNameDraft}
                placeholder="Support"
                onChange={(event) => setAgentEmailMailboxDisplayNameDraft(event.target.value)}
                autoComplete="off"
                spellCheck={false}
              />
            </label>
            <div className="agent-email-form-field agent-email-settings-provision-actions">
              <span>&nbsp;</span>
              <button
                type="button"
                className="agent-email-console-primary"
                onClick={() => void handleAgentEmailCreateMailbox()}
                disabled={
                  agentEmailCreatingMailbox
                  || !agentEmailOrg
                  || !agentEmailDomains.length
                  || !agentEmailMailboxLocalPartDraft.trim()
                }
              >
                {agentEmailCreatingMailbox ? 'Creating…' : 'Create mailbox'}
              </button>
            </div>
          </div>
        </section>
        <p className="agent-email-settings-hint">
          Link a domain under Domains first if none appear here.
        </p>
      </div>
    )

    const renderAgentEmailTrustedSendersSection = () => (
      <div className="agent-email-settings-domains">
        <section className="agent-email-settings-domain-add" aria-label="Add trusted sender">
          <div className="agent-email-settings-domain-add-text">
            <h4 className="agent-email-settings-section-title">Email identity / trusted senders</h4>
            <p className="agent-email-settings-section-lead">
              Addresses you trust for inbound identity. Synced to Gateway on the VM and used by inbound email processing.
            </p>
          </div>
          <div className="agent-email-settings-domain-add-row">
            <input
              className="agent-email-form-input agent-email-settings-domain-add-input"
              type="email"
              value={agentEmailTrustedSenderDraft}
              placeholder="name@company.com"
              onChange={(event) => setAgentEmailTrustedSenderDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault()
                  handleAgentEmailAddTrustedSender()
                }
              }}
              autoComplete="off"
              spellCheck={false}
              aria-label="Trusted sender email"
            />
            <button
              type="button"
              className="agent-email-console-primary agent-email-settings-domain-add-btn"
              onClick={() => handleAgentEmailAddTrustedSender()}
              disabled={!agentEmailTrustedSenderDraft.trim()}
            >
              Add
            </button>
          </div>
        </section>

        <div className="agent-email-settings-domain-split agent-email-settings-trusted-senders-split">
          <div className="agent-email-settings-domain-list-panel agent-email-settings-trusted-senders-list">
            <div className="agent-email-settings-panel-label">Trusted addresses</div>
            {agentEmailTrustedSenders.length ? (
              <div className="agent-email-console-table-wrap agent-email-settings-domain-table-wrap">
                <table className="agent-email-console-table agent-email-settings-domain-table agent-email-settings-trusted-senders-table">
                  <thead>
                    <tr>
                      <th>Email</th>
                      <th className="agent-email-settings-trusted-senders-actions-col"> </th>
                    </tr>
                  </thead>
                  <tbody>
                    {agentEmailTrustedSenders.map((email) => (
                      <tr key={email.toLowerCase()}>
                        <td data-label="Email">
                          <strong className="agent-email-console-mono">{email}</strong>
                        </td>
                        <td data-label="Actions" className="agent-email-settings-trusted-senders-actions-cell">
                          <button
                            type="button"
                            className="agent-email-console-secondary agent-email-settings-trusted-remove"
                            onClick={() => handleAgentEmailRemoveTrustedSender(email)}
                          >
                            Remove
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              renderAgentEmailEmptyState('No trusted senders yet', 'Add an email above. Duplicates are ignored.')
            )}
          </div>
        </div>
      </div>
    )

    const renderAgentEmailAgentsProvisionSection = () => (
      <div className="agent-email-settings-domains">
        <section className="agent-email-settings-domain-add" aria-label="Add agent">
          <div className="agent-email-settings-domain-add-text">
            <h4 className="agent-email-settings-section-title">Add an agent</h4>
            <p className="agent-email-settings-section-lead">Registers a new mail agent identity in your organization.</p>
          </div>
          <div className="agent-email-settings-provision-grid">
            <label className="agent-email-form-field">
              <span>Display name</span>
              <input
                className="agent-email-form-input"
                type="text"
                value={agentEmailNewAgentNameDraft}
                placeholder="Billing assistant"
                onChange={(event) => setAgentEmailNewAgentNameDraft(event.target.value)}
                autoComplete="off"
                spellCheck={false}
              />
            </label>
            <label className="agent-email-form-field">
              <span>Slug</span>
              <input
                className="agent-email-form-input"
                type="text"
                value={agentEmailNewAgentSlugDraft}
                placeholder="billing-assistant"
                onChange={(event) => setAgentEmailNewAgentSlugDraft(event.target.value)}
                autoComplete="off"
                spellCheck={false}
                aria-label="Agent slug"
              />
            </label>
            <label className="agent-email-form-field">
              <span>Default domain</span>
              <select
                className="agent-email-form-input"
                value={agentEmailNewAgentDomainIdDraft}
                onChange={(event) => setAgentEmailNewAgentDomainIdDraft(event.target.value)}
                aria-label="Default sending domain"
              >
                {agentEmailDomains.length ? agentEmailDomains.map((d) => (
                  <option key={d.id} value={d.id}>{d.name}</option>
                )) : (
                  <option value="">No domain linked</option>
                )}
              </select>
            </label>
            <div className="agent-email-form-field agent-email-settings-provision-actions">
              <span>&nbsp;</span>
              <button
                type="button"
                className="agent-email-console-primary"
                onClick={() => void handleAgentEmailCreateNewAgent()}
                disabled={
                  agentEmailCreatingNewAgent
                  || !agentEmailOrg
                  || !agentEmailNewAgentNameDraft.trim()
                }
              >
                {agentEmailCreatingNewAgent ? 'Creating…' : 'Create agent'}
              </button>
            </div>
          </div>
        </section>
      </div>
    )

    const renderAgentEmailContent = () => {
      if (agentEmailView === 'settings') {
        return (
          <div className="agent-email-settings-page">
            <div className="agent-email-settings-subnav" role="tablist" aria-label="Settings sections">
              <button
                type="button"
                role="tab"
                aria-selected={agentEmailSettingsSection === 'connection'}
                className={`agent-email-settings-subtab ${agentEmailSettingsSection === 'connection' ? 'active' : ''}`}
                onClick={() => setAgentEmailSettingsSection('connection')}
              >
                Connection
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={agentEmailSettingsSection === 'trusted-senders'}
                className={`agent-email-settings-subtab ${agentEmailSettingsSection === 'trusted-senders' ? 'active' : ''}`}
                onClick={() => setAgentEmailSettingsSection('trusted-senders')}
              >
                Email identity
                {agentEmailTrustedSenders.length > 0 ? (
                  <span className="agent-email-settings-subtab-count">{agentEmailTrustedSenders.length}</span>
                ) : null}
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={agentEmailSettingsSection === 'domains'}
                className={`agent-email-settings-subtab ${agentEmailSettingsSection === 'domains' ? 'active' : ''}`}
                disabled={!agentEmailConfigReady}
                onClick={() => {
                  if (agentEmailConfigReady) {
                    setAgentEmailSettingsSection('domains')
                  }
                }}
              >
                Domains
                {agentEmailConfigReady && agentEmailDomains.length > 0 ? (
                  <span className="agent-email-settings-subtab-count">{agentEmailDomains.length}</span>
                ) : null}
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={agentEmailSettingsSection === 'inboxes'}
                className={`agent-email-settings-subtab ${agentEmailSettingsSection === 'inboxes' ? 'active' : ''}`}
                disabled={!agentEmailConfigReady}
                onClick={() => {
                  if (agentEmailConfigReady) {
                    setAgentEmailSettingsSection('inboxes')
                  }
                }}
              >
                Inboxes
                {agentEmailConfigReady && agentEmailInboxes.length > 0 ? (
                  <span className="agent-email-settings-subtab-count">{agentEmailInboxes.length}</span>
                ) : null}
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={agentEmailSettingsSection === 'agents'}
                className={`agent-email-settings-subtab ${agentEmailSettingsSection === 'agents' ? 'active' : ''}`}
                disabled={!agentEmailConfigReady}
                onClick={() => {
                  if (agentEmailConfigReady) {
                    setAgentEmailSettingsSection('agents')
                  }
                }}
              >
                Agents
                {agentEmailConfigReady && agentEmailAgents.length > 0 ? (
                  <span className="agent-email-settings-subtab-count">{agentEmailAgents.length}</span>
                ) : null}
              </button>
            </div>
            {agentEmailSettingsSection === 'connection' ? renderAgentEmailConnectionPanel() : null}
            {agentEmailSettingsSection === 'trusted-senders' ? renderAgentEmailTrustedSendersSection() : null}
            {agentEmailConfigReady && agentEmailSettingsSection === 'domains' ? renderAgentEmailDomainsSection() : null}
            {agentEmailConfigReady && agentEmailSettingsSection === 'inboxes' ? renderAgentEmailMailboxesSection() : null}
            {agentEmailConfigReady && agentEmailSettingsSection === 'agents' ? renderAgentEmailAgentsProvisionSection() : null}
          </div>
        )
      }

      if (!agentEmailConfigReady) {
        return renderAgentEmailEmptyState(
          'Cosmic Mail',
          'Configure the API base URL and key in Settings.',
          'Open Settings',
          () => setAgentEmailView('settings'),
        )
      }

      if (agentEmailLoading && !agentEmailHasData) {
        return renderAgentEmailEmptyState(
          'Loading Agent Email',
          'Spaces is pulling the current organization, inbox, threads, domains, and approval state from Cosmic Mail.',
        )
      }

      if (agentEmailError && !agentEmailHasData) {
        return renderAgentEmailEmptyState(
          'Unable to load Agent Email',
          agentEmailError,
          'Retry',
          () => { void requestAgentEmailSnapshot(true) },
        )
      }

      if (agentEmailView === 'overview') {
        return (
          <div className="agent-email-console-overview">
            <section className="agent-email-console-stats">
              {agentEmailMetrics.map((metric) => (
                <article key={metric.label} className="agent-email-console-stat">
                  <div className="agent-email-console-stat-label">{metric.label}</div>
                  <div className="agent-email-console-stat-value">{metric.value}</div>
                  <div className="agent-email-console-stat-meta">{metric.note}</div>
                </article>
              ))}
            </section>

            <section className="agent-email-console-two-col">
              <div className="agent-email-console-stack">
                <article className="agent-email-console-card">
                  <div className="agent-email-console-card-head">
                    <h4>Setup checklist</h4>
                  </div>
                  <div className="agent-email-console-checks">
                    {agentEmailChecklist.map((item) => (
                      <div key={item.label} className="agent-email-console-check">
                        <div className={`agent-email-console-check-dot ${item.complete ? 'complete' : ''}`} />
                        <div>
                          <strong>{item.label}</strong>
                          <p>{item.note}</p>
                        </div>
                      </div>
                    ))}
                  </div>
                </article>

                <article className="agent-email-console-card">
                  <div className="agent-email-console-card-head">
                    <h4>Default agent</h4>
                    <button type="button" className="agent-email-console-link" onClick={() => setAgentEmailView('agents')}>
                      Open
                    </button>
                  </div>
                  <div className="agent-email-console-simple-list">
                    {agentEmailSelectedAgent ? (
                      <button
                        type="button"
                        className="agent-email-console-simple-row"
                        onClick={() => {
                          setAgentEmailSelectedAgentId(agentEmailSelectedAgent.id)
                          setAgentEmailView('agents')
                        }}
                      >
                        <div>
                          <strong>{agentEmailSelectedAgent.name}</strong>
                          <span>{agentEmailSelectedAgent.role}</span>
                        </div>
                        <span className={`agent-email-minimal-pill ${agentEmailSelectedAgent.accent}`}>{agentEmailSelectedAgent.status}</span>
                      </button>
                    ) : (
                      renderAgentEmailEmptyState('No agent yet', 'Create or expose a default agent in Cosmic Mail to manage outbound identity here.')
                    )}
                  </div>
                </article>
              </div>

              <div className="agent-email-console-stack">
                <article className="agent-email-console-card">
                  <div className="agent-email-console-card-head">
                    <h4>System</h4>
                  </div>
                  <div className="agent-email-console-detail-rows">
                    <div className="agent-email-console-detail-row"><span>Organization</span><strong>{orgName}</strong></div>
                    <div className="agent-email-console-detail-row"><span>Live bridge</span><strong>{agentEmailConnectionStatus.label}</strong></div>
                    <div className="agent-email-console-detail-row"><span>Approval pressure</span><strong>{pendingAgentEmailApprovals} waiting</strong></div>
                    <div className="agent-email-console-detail-row"><span>Active domains</span><strong>{activeAgentEmailDomains}/{agentEmailDomains.length}</strong></div>
                    <div className="agent-email-console-detail-row"><span>Inbox</span><strong>{unreadAgentEmailThreads} unread</strong></div>
                  </div>
                </article>
              </div>
            </section>
          </div>
        )
      }

      if (agentEmailView === 'agents') {
        if (!agentEmailSelectedAgent) {
          return renderAgentEmailEmptyState('No agent available', 'This organization does not currently expose a default agent.')
        }

        return (
          <div className="agent-email-console-two-col">
            <article className="agent-email-console-card">
              <div className="agent-email-console-card-head">
                <div>
                  <h4>{agentEmailSelectedAgent.name}</h4>
                  <div className="agent-email-console-muted agent-email-console-mono">{agentEmailSelectedAgent.address}</div>
                </div>
                <span className={`agent-email-minimal-pill ${agentEmailSelectedAgent.accent}`}>{agentEmailSelectedAgent.status}</span>
              </div>
              <div className="agent-email-console-detail-rows">
                <div className="agent-email-console-detail-row"><span>Organization</span><strong>{orgName}</strong></div>
                <div className="agent-email-console-detail-row"><span>Role</span><strong>{agentEmailSelectedAgent.role}</strong></div>
                <div className="agent-email-console-detail-row"><span>Default domain</span><strong>{agentEmailSelectedAgent.domain}</strong></div>
                <div className="agent-email-console-detail-row"><span>Approval mode</span><strong>{agentEmailSelectedAgent.approvalMode}</strong></div>
                <div className="agent-email-console-detail-row"><span>Last activity</span><strong>{agentEmailSelectedAgent.lastActivity}</strong></div>
              </div>
              <div className="agent-email-console-text-block agent-email-agent-inboxes-block">
                <span>Linked inboxes</span>
                {agentEmailSelectedAgent.inboxes.length ? (
                  <div className="agent-email-agent-inbox-list" role="list">
                    {agentEmailSelectedAgent.inboxes.map((address) => {
                      const linkedInbox = agentEmailInboxes.find((inbox) => inbox.address === address)
                      return (
                        <button
                          key={address}
                          type="button"
                          role="listitem"
                          className="agent-email-agent-inbox-row"
                          disabled={!linkedInbox}
                          onClick={() => {
                            if (!linkedInbox) {
                              return
                            }
                            setAgentEmailSelectedInboxId(linkedInbox.id)
                            setAgentEmailView('inboxes')
                          }}
                        >
                          <div className="agent-email-agent-inbox-row-text">
                            <strong className="agent-email-agent-inbox-row-address">{address}</strong>
                            {linkedInbox ? (
                              <span className="agent-email-agent-inbox-row-meta">
                                {linkedInbox.id === agentEmailSelectedInboxId
                                  ? `${unreadAgentEmailThreads} unread`
                                  : `Updated ${linkedInbox.lastSync}`}
                              </span>
                            ) : (
                              <span className="agent-email-agent-inbox-row-meta agent-email-agent-inbox-row-meta--warn">
                                Not found in this organization
                              </span>
                            )}
                          </div>
                          {linkedInbox ? (
                            <span className={`agent-email-minimal-pill agent-email-agent-inbox-row-pill ${linkedInbox.accent}`}>
                              {linkedInbox.status}
                            </span>
                          ) : null}
                        </button>
                      )
                    })}
                  </div>
                ) : (
                  <p>No inbox is linked to this agent yet.</p>
                )}
              </div>
              <div className="agent-email-console-text-block">
                <span>Notes</span>
                <p>{agentEmailSelectedAgent.note}</p>
              </div>
            </article>

            <div className="agent-email-console-stack">
              <article className="agent-email-console-card">
                <div className="agent-email-console-card-head">
                  <h4>Controls</h4>
                </div>
                <div className="agent-email-console-detail-rows">
                  <div className="agent-email-console-detail-row"><span>Primary org</span><strong>{orgName}</strong></div>
                  <div className="agent-email-console-detail-row"><span>Outbound posture</span><strong>{agentEmailSelectedAgent.approvalMode}</strong></div>
                  <div className="agent-email-console-detail-row"><span>Inbox coverage</span><strong>{agentEmailSelectedAgent.inboxes.length} linked</strong></div>
                </div>
              </article>
            </div>
          </div>
        )
      }

      if (agentEmailView === 'inboxes') {
        if (!agentEmailSelectedInbox) {
          return renderAgentEmailEmptyState('No inbox available', 'Create or expose a mailbox in Cosmic Mail to review inbound conversations here.')
        }

        return (
          <div className="agent-email-inbox-layout">
            <aside className="agent-email-inbox-sidebar" aria-label="Threads">
              <div className="agent-email-inbox-sidebar-top">
                <div className="agent-email-inbox-sidebar-head">
                  <div className="agent-email-inbox-sidebar-title">
                    <div className="agent-email-inbox-mailbox-label-row">
                      <span className="agent-email-inbox-mailbox-label">Mailbox</span>
                      {unreadAgentEmailThreads > 0 ? (
                        <span className="agent-email-inbox-unread-badge" aria-label={`${unreadAgentEmailThreads} unread`}>
                          {unreadAgentEmailThreads > 99 ? '99+' : unreadAgentEmailThreads}
                        </span>
                      ) : null}
                    </div>
                    <h4 title={agentEmailSelectedInbox.address}>{agentEmailSelectedInbox.address}</h4>
                  </div>
                  <button
                    type="button"
                    className="agent-email-inbox-sync"
                    onClick={() => void handleAgentEmailSyncInbox()}
                    disabled={agentEmailSyncingInbox}
                  >
                    {agentEmailSyncingInbox ? '…' : 'Sync'}
                  </button>
                </div>
                <div className="agent-email-inbox-search">
                  <input
                    type="search"
                    className="agent-email-form-input agent-email-inbox-search-input"
                    placeholder="Filter by subject or preview…"
                    value={agentEmailInboxSearchQuery}
                    onChange={(event) => setAgentEmailInboxSearchQuery(event.target.value)}
                    aria-label="Filter threads by subject or preview text"
                    autoComplete="off"
                    spellCheck={false}
                  />
                </div>
              </div>
              <div className="agent-email-inbox-list-shell">
                <div className="agent-email-inbox-thread-list">
                  {agentEmailThreads.length ? agentEmailThreads.map((thread) => (
                    <button
                      key={thread.id}
                      type="button"
                      className={`agent-email-thread-item ${thread.unread ? 'agent-email-thread-item--unread' : ''} ${agentEmailSelectedThread?.id === thread.id ? 'active' : ''}`}
                      onClick={() => setAgentEmailSelectedThreadId(thread.id)}
                    >
                      <div className="agent-email-thread-row">
                        <div className="agent-email-thread-avatar" aria-hidden>
                          {getAgentEmailInitials(thread.fromName)}
                        </div>
                        <div className="agent-email-thread-main">
                          <div className="agent-email-thread-line1">
                            <span className="agent-email-thread-subject">{thread.subject || '(No subject)'}</span>
                            <span className="agent-email-thread-time">{thread.time}</span>
                          </div>
                          <div className="agent-email-thread-line2">
                            <span className="agent-email-thread-from">{thread.fromName}</span>
                          </div>
                          <p className="agent-email-thread-snippet">{thread.snippet}</p>
                          <div className="agent-email-thread-foot">
                            <span className="agent-email-thread-status-pill">{thread.state}</span>
                          </div>
                        </div>
                      </div>
                    </button>
                  )) : (
                    <div className="agent-email-inbox-thread-list-empty">
                      {renderAgentEmailEmptyState(
                        'Inbox is clear',
                        'No conversations are loaded for this inbox yet. Sync once mail has arrived to pull the latest inbound threads.',
                      )}
                    </div>
                  )}
                </div>
              </div>
            </aside>

            <section className="agent-email-inbox-pane" aria-label="Thread">
              {agentEmailSelectedThread ? (
                <div className="agent-email-inbox-pane-column">
                  <header className="agent-email-inbox-read-head">
                    <div className="agent-email-inbox-read-hero">
                      <div className="agent-email-inbox-read-avatar" aria-hidden>
                        {getAgentEmailInitials(agentEmailSelectedThread.fromName)}
                      </div>
                      <div className="agent-email-inbox-read-hero-main">
                        <div className="agent-email-inbox-read-title-row">
                          <h4 className="agent-email-inbox-read-subject">{agentEmailSelectedThread.subject || '(No subject)'}</h4>
                          <span className={`agent-email-inbox-status ${agentEmailSelectedThread.state === 'Awaiting approval' ? 'is-warn' : agentEmailSelectedThread.unread ? 'is-warn' : 'is-ok'}`}>
                            {agentEmailSelectedThread.state}
                          </span>
                        </div>
                        <p className="agent-email-inbox-read-from">
                          <span className="agent-email-inbox-read-from-name">{agentEmailSelectedThread.fromName}</span>
                          <span className="agent-email-inbox-read-from-sep" aria-hidden>·</span>
                          <span className="agent-email-console-mono agent-email-inbox-read-from-email">{agentEmailSelectedThread.fromAddress}</span>
                        </p>
                        <p className="agent-email-inbox-read-meta">
                          <span className="agent-email-inbox-read-meta-item">{selectedAgentName}</span>
                          <span className="agent-email-inbox-read-meta-sep" aria-hidden />
                          <span className="agent-email-console-mono agent-email-inbox-read-meta-item" title={agentEmailSelectedInbox.address}>{agentEmailSelectedInbox.address}</span>
                          <span className="agent-email-inbox-read-meta-sep" aria-hidden />
                          <span className="agent-email-inbox-read-meta-item">
                            {(agentEmailSelectedThread.messagesLoaded ? agentEmailSelectedThread.messages.length : agentEmailSelectedThread.threadSnapshot.message_count)} {(agentEmailSelectedThread.messagesLoaded ? agentEmailSelectedThread.messages.length : agentEmailSelectedThread.threadSnapshot.message_count) === 1 ? 'message' : 'messages'}
                          </span>
                        </p>
                      </div>
                    </div>
                  </header>

                  <div className="agent-email-inbox-messages-scroll">
                    <div className="agent-email-inbox-thread-body">
                      {!agentEmailSelectedThread.messagesLoaded && agentEmailThreadMessagesLoadingId === agentEmailSelectedThread.id ? (
                        <div className="agent-email-banner warning">Loading the selected conversation...</div>
                      ) : null}
                      {agentEmailSelectedThread.messages.map((message) => (
                        <article key={message.id} className={`agent-email-inbox-msg agent-email-inbox-msg--${message.direction}`}>
                          <div className="agent-email-inbox-msg-inner">
                            <header className="agent-email-inbox-msg-head">
                              <div className="agent-email-inbox-msg-who-wrap">
                                <span className="agent-email-inbox-msg-who">{message.author}</span>
                                <span className="agent-email-inbox-msg-pill">
                                  {message.direction === 'inbound' ? 'Inbound' : 'Outbound'}
                                </span>
                              </div>
                              <time className="agent-email-inbox-msg-time">{message.time}</time>
                            </header>
                            <div className="agent-email-inbox-msg-body">{message.body}</div>
                            {message.attachments.length > 0 ? (
                              <ul className="agent-email-msg-attachments" aria-label="Attachments">
                                {message.attachments.map((att) => (
                                  <li key={att.id}>
                                    <button
                                      type="button"
                                      className="agent-email-attachment-chip"
                                      onClick={() => void handleAgentEmailDownloadAttachment(att)}
                                      disabled={agentEmailAttachmentActionId === att.id}
                                    >
                                      <span className="agent-email-attachment-name">{att.filename}</span>
                                      <span className="agent-email-attachment-meta">{formatAgentEmailAttachmentSize(att.size_bytes)}</span>
                                    </button>
                                  </li>
                                ))}
                              </ul>
                            ) : null}
                          </div>
                        </article>
                      ))}
                    </div>
                    <div ref={agentEmailMessagesEndRef} className="agent-email-inbox-messages-end" aria-hidden />
                  </div>

                  <div className={`agent-email-compose-dock ${agentEmailComposerExpanded ? 'is-expanded' : 'is-collapsed'}`}>
                    {agentEmailComposerExpanded ? (
                      <div className="agent-email-compose-panel">
                        <div className="agent-email-compose-panel-head">
                          <span className="agent-email-compose-panel-label">Reply · {selectedAgentName}</span>
                          <button
                            type="button"
                            className="agent-email-compose-dismiss"
                            onClick={() => setAgentEmailComposerExpanded(false)}
                            aria-label="Close reply"
                          >
                            Done
                          </button>
                        </div>
                        <textarea
                          className="agent-email-compose-field"
                          value={agentEmailReplyDraft}
                          placeholder="Message"
                          rows={4}
                          onChange={(event) => setAgentEmailReplyDraft(event.target.value)}
                          onKeyDown={(event) => {
                            if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
                              event.preventDefault()
                              void handleAgentEmailReply()
                            }
                          }}
                        />
                        <input
                          ref={agentEmailReplyAttachInputRef}
                          type="file"
                          multiple
                          className="agent-email-compose-file-input"
                          onChange={(event) => {
                            const list = event.target.files
                            if (!list?.length) return
                            setAgentEmailReplyAttachmentFiles((prev) => [...prev, ...Array.from(list)])
                            event.target.value = ''
                          }}
                        />
                        {agentEmailReplyAttachmentFiles.length > 0 ? (
                          <ul className="agent-email-compose-attachments" aria-label="Attachments to send">
                            {agentEmailReplyAttachmentFiles.map((file, index) => (
                              <li key={`${file.name}-${index}-${file.size}`} className="agent-email-compose-attachment-row">
                                <span className="agent-email-compose-attachment-name">{file.name}</span>
                                <span className="agent-email-compose-attachment-meta">{formatAgentEmailAttachmentSize(file.size)}</span>
                                <button
                                  type="button"
                                  className="agent-email-compose-attachment-remove"
                                  onClick={() => setAgentEmailReplyAttachmentFiles((prev) => prev.filter((_, i) => i !== index))}
                                  aria-label={`Remove ${file.name}`}
                                >
                                  Remove
                                </button>
                              </li>
                            ))}
                          </ul>
                        ) : null}
                        <div className="agent-email-compose-panel-foot">
                          <button
                            type="button"
                            className="agent-email-compose-attach"
                            onClick={() => agentEmailReplyAttachInputRef.current?.click()}
                            disabled={agentEmailReplySending}
                          >
                            Attach
                          </button>
                          <span className="agent-email-compose-kbd-hint">⌘↵</span>
                          <button
                            type="button"
                            className="agent-email-compose-send"
                            onClick={() => void handleAgentEmailReply()}
                            disabled={
                              agentEmailReplySending
                              || (!agentEmailReplyDraft.trim() && agentEmailReplyAttachmentFiles.length === 0)
                            }
                          >
                            {agentEmailReplySending ? 'Sending…' : 'Send'}
                          </button>
                        </div>
                      </div>
                    ) : (
                      <div className="agent-email-inbox-reply-toolbar" role="toolbar" aria-label="Reply">
                        <button
                          type="button"
                          className="agent-email-reply-toolbar-primary"
                          onClick={() => setAgentEmailComposerExpanded(true)}
                        >
                          <svg className="agent-email-reply-icon" viewBox="0 0 24 24" width="18" height="18" aria-hidden>
                            <path
                              fill="currentColor"
                              d="M10 9V5l-7 7 7 7v-4.1c5 0 8.5 1.6 11 5.1-1-5-4-10-11-11z"
                            />
                          </svg>
                          <span>{agentEmailReplyDraft.trim() || agentEmailReplyAttachmentFiles.length > 0 ? 'Continue reply' : 'Reply'}</span>
                        </button>
                        <span className="agent-email-reply-toolbar-agent" title={selectedAgentName}>
                          {selectedAgentName}
                        </span>
                        {agentEmailReplyAttachmentFiles.length > 0 ? (
                          <span className="agent-email-reply-toolbar-meta" title="Draft attachments">
                            {agentEmailReplyAttachmentFiles.length} file{agentEmailReplyAttachmentFiles.length === 1 ? '' : 's'}
                          </span>
                        ) : null}
                        {agentEmailReplyDraft.trim() ? (
                          <span className="agent-email-reply-toolbar-meta" title="Draft length">
                            {agentEmailReplyDraft.trim().length} chars
                          </span>
                        ) : null}
                      </div>
                    )}
                  </div>

                  {agentEmailSelectedInbox.note ? (
                    <p className="agent-email-inbox-footnote">{agentEmailSelectedInbox.note}</p>
                  ) : null}
                </div>
              ) : (
                <div className="agent-email-inbox-pane-empty">
                  {renderAgentEmailEmptyState('No thread selected', 'Choose a conversation from the list.')}
                </div>
              )}
            </section>
          </div>
        )
      }

      if (agentEmailView === 'approvals') {
        if (!agentEmailApprovals.length) {
          return renderAgentEmailEmptyState('No approvals waiting', 'Outbound review is currently clear. Any policy-gated replies will appear here.')
        }

        return (
          <div className="agent-email-console-approvals">
            <div className="agent-email-console-approvals-list">
              <div className="agent-email-console-filterbar">
                <span className="agent-email-console-copy">{pendingAgentEmailApprovals} pending review</span>
              </div>
              <div className="agent-email-minimal-list compact">
                {agentEmailApprovals.map((approval) => (
                  <button
                    key={approval.id}
                    type="button"
                    className={`agent-email-minimal-row ${agentEmailSelectedApproval?.id === approval.id ? 'active' : ''}`}
                    onClick={() => setAgentEmailSelectedApprovalId(approval.id)}
                  >
                    <div className="agent-email-minimal-row-top">
                      <strong>{approval.subject}</strong>
                      <span className={`agent-email-minimal-pill ${approval.accent}`}>{approval.state}</span>
                    </div>
                    <div className="agent-email-minimal-row-meta">
                      <span>{approval.mailbox}</span>
                      <span>{approval.time}</span>
                    </div>
                    <p>{approval.reason}</p>
                  </button>
                ))}
              </div>
            </div>

            <div className="agent-email-console-approvals-detail">
              {agentEmailSelectedApproval ? (
                <div className="agent-email-console-card">
                  <div className="agent-email-console-card-head">
                    <h4>{agentEmailSelectedApproval.subject}</h4>
                    <span className={`agent-email-minimal-pill ${agentEmailSelectedApproval.accent}`}>{agentEmailSelectedApproval.state}</span>
                  </div>
                  <div className="agent-email-console-detail-rows">
                    <div className="agent-email-console-detail-row"><span>Agent</span><strong>{agentEmailSelectedApproval.agent}</strong></div>
                    <div className="agent-email-console-detail-row"><span>Mailbox</span><strong>{agentEmailSelectedApproval.mailbox}</strong></div>
                    <div className="agent-email-console-detail-row"><span>To</span><strong>{agentEmailSelectedApproval.recipients}</strong></div>
                    {agentEmailSelectedApproval.cc ? (
                      <div className="agent-email-console-detail-row"><span>Cc</span><strong>{agentEmailSelectedApproval.cc}</strong></div>
                    ) : null}
                    {agentEmailSelectedApproval.bcc ? (
                      <div className="agent-email-console-detail-row"><span>Bcc</span><strong>{agentEmailSelectedApproval.bcc}</strong></div>
                    ) : null}
                    <div className="agent-email-console-detail-row"><span>Triggered</span><strong>{agentEmailSelectedApproval.time}</strong></div>
                  </div>
                  <div className="agent-email-console-text-block">
                    <span>Reason</span>
                    <p>{agentEmailSelectedApproval.summary}</p>
                  </div>
                  <div className="agent-email-console-text-block">
                    <span>Draft preview</span>
                    <p>{agentEmailSelectedApproval.excerpt}</p>
                  </div>
                  <div className="agent-email-detail-actions">
                    <button
                      type="button"
                      className="agent-email-console-primary"
                      onClick={() => void handleAgentEmailApprove(agentEmailSelectedApproval.id)}
                      disabled={agentEmailActionId === agentEmailSelectedApproval.id || agentEmailSelectedApproval.state !== 'Pending'}
                    >
                      {agentEmailActionId === agentEmailSelectedApproval.id ? 'Processing...' : 'Approve and send'}
                    </button>
                    <button
                      type="button"
                      className="agent-email-console-secondary"
                      onClick={() => void handleAgentEmailReject(agentEmailSelectedApproval.id)}
                      disabled={agentEmailActionId === agentEmailSelectedApproval.id || agentEmailSelectedApproval.state !== 'Pending'}
                    >
                      Reject
                    </button>
                  </div>
                </div>
              ) : (
                renderAgentEmailEmptyState('Select an approval', 'Choose a queued outbound draft to inspect it and release or reject it.')
              )}
            </div>
          </div>
        )
      }

      return null
    }

    return (
      <div className="spaces-page agent-email-page">
        <section className="agent-email-minimal-tabs" role="tablist" aria-label="Agent email sections">
          {agentEmailViews.map((item) => (
            <button
              key={item.id}
              type="button"
              role="tab"
              aria-selected={agentEmailView === item.id}
              className={`agent-email-minimal-tab ${agentEmailView === item.id ? 'active' : ''}`}
              onClick={() => setAgentEmailView(item.id)}
            >
              <span className="agent-email-tab-label">{item.label}</span>
              <span className="agent-email-tab-signal">{item.signal}</span>
            </button>
          ))}
        </section>

        <section className={`agent-email-minimal-shell${agentEmailView === 'inboxes' ? ' agent-email-minimal-shell--inbox' : ''}`}>
          <div className="agent-email-minimal-shell-head">
            <div className="agent-email-minimal-shell-head-text">
              {currentAgentEmailTab.kicker ? (
                <div className="spaces-card-kicker">{currentAgentEmailTab.kicker}</div>
              ) : null}
              <h3>{currentAgentEmailTab.label}</h3>
              {currentAgentEmailTab.detail ? (
                <p className="agent-email-shell-subtitle">{currentAgentEmailTab.detail}</p>
              ) : null}
            </div>
            <div className="agent-email-shell-context" aria-label="Current context">
              {agentEmailView === 'overview' ? <span className="agent-email-context-chip">{orgName}</span> : null}
              {agentEmailView === 'inboxes' ? (
                agentEmailInboxes.length > 1 ? (
                  <select
                    className="agent-email-shell-mailbox-select agent-email-console-mono"
                    value={agentEmailSelectedInboxId}
                    onChange={(event) => setAgentEmailSelectedInboxId(event.target.value)}
                    aria-label="Active mailbox"
                  >
                    {agentEmailInboxes.map((inbox) => (
                      <option key={inbox.id} value={inbox.id}>
                        {inbox.address}
                      </option>
                    ))}
                  </select>
                ) : (
                  <span className="agent-email-context-chip agent-email-console-mono" title={selectedInboxName}>
                    {selectedInboxName}
                  </span>
                )
              ) : null}
              {agentEmailView === 'settings' && agentEmailDomains.length > 0 ? (
                <span className="agent-email-context-chip agent-email-console-mono" title={primaryDomainLabel}>{primaryDomainLabel}</span>
              ) : null}
              {agentEmailView === 'approvals' && pendingAgentEmailApprovals > 0 ? (
                <span className="agent-email-context-chip agent-email-context-chip-warm">{pendingAgentEmailApprovals} pending</span>
              ) : null}
              {agentEmailView === 'settings' && agentEmailEffectiveBaseUrl ? (
                <span className="agent-email-context-chip agent-email-console-mono" title={agentEmailEffectiveBaseUrl}>
                  {shortenAgentEmailUrl(agentEmailEffectiveBaseUrl)}
                </span>
              ) : null}
            </div>
          </div>
          {renderAgentEmailContent()}
        </section>
      </div>
    )
  }

  const renderManagePage = () => {
    const budgetUsagePercent = manageSnapshot.budgetTotal
      ? Math.min(100, Math.max(0, Math.round((manageSnapshot.budgetUsed / manageSnapshot.budgetTotal) * 100)))
      : 0
    const currencySymbol = manageSnapshot.budgetCurrency === 'USD' ? '$' : `${manageSnapshot.budgetCurrency} `
    const budgetCap = `${currencySymbol}${manageSnapshot.budgetTotal.toFixed(2)}`
    const lastUpdatedLabel = manageLastUpdatedAt
      ? new Date(manageLastUpdatedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
      : '—'

    const liveServiceCount = manageSnapshot.services.filter((s) => s.status === 'live').length
    const degradedCount = manageSnapshot.services.filter((s) => s.status === 'down').length

    const networkPercent = Math.min(
      100,
      Math.max(0, manageSnapshot.networkThroughput.includes('Mbps') ? 38 : 8),
    )

    const meterAccent = (pct: number): AccentTone =>
      pct >= 85 ? 'rose' : pct >= 65 ? 'gold' : pct >= 40 ? 'mint' : 'azure'

    const hardwareRows = [
      { label: 'CPU', value: `${manageSnapshot.cpuPercent}%`, pct: manageSnapshot.cpuPercent },
      { label: 'RAM', value: `${manageSnapshot.memoryUsedText} / ${manageSnapshot.memoryTotalText}`, pct: manageSnapshot.memoryPercent },
      { label: 'Disk', value: `${manageSnapshot.diskUsedText} / ${manageSnapshot.diskTotalText}`, pct: manageSnapshot.diskPercent },
      { label: 'Network', value: manageSnapshot.networkThroughput, pct: networkPercent },
    ]

    const arcPath = (pct: number, r: number) => {
      const c = 2 * Math.PI * r
      return `${(pct / 100) * c} ${c}`
    }

    const minSelectableYmd = manageUsageBounds.minYmd
    const maxSelectableYmd = manageUsageBounds.maxYmd
    const customStartValid = isValidYmd(manageUsageCustomStart)
    const customEndValid = isValidYmd(manageUsageCustomEnd)
    const customRangeValid =
      customStartValid &&
      customEndValid &&
      manageUsageCustomStart <= manageUsageCustomEnd &&
      (!minSelectableYmd || manageUsageCustomStart >= minSelectableYmd) &&
      (!maxSelectableYmd || manageUsageCustomEnd <= maxSelectableYmd)
    const customRangeHint = minSelectableYmd
      ? `Calendar starts at ${formatYmdLocaleLong(minSelectableYmd)} and ends at ${formatYmdLocaleLong(maxSelectableYmd)}.`
      : `Calendar ends at ${formatYmdLocaleLong(maxSelectableYmd)}. Earlier months appear once usage logs exist.`

    return (
      <div className="spaces-page mg-page">

        {/* ── Spend hero ── */}
        <section className="mg-hero">
          <div className="mg-hero-glow" />
          <div className="mg-hero-top">
            <div className="mg-hero-left">
              <span className="mg-kicker">{manageSnapshot.cycleLabel || 'Current cycle'}</span>
              <div className="mg-spend-line">
                <span className="mg-spend-sym">{currencySymbol.trim()}</span>
                <span className="mg-spend-val">{manageSnapshot.budgetUsed.toFixed(2)}</span>
              </div>
              <span className="mg-spend-cap">of {budgetCap} budget &middot; {budgetUsagePercent}% used</span>
              <div className="mg-usage-period" role="group" aria-label="Usage and cost time window">
                <span className="mg-usage-period-label">Spend window</span>
                <div className="mg-usage-period-presets">
                  <button
                    type="button"
                    className={`mg-usage-period-chip ${manageUsageMode === '24h' ? 'active' : ''}`}
                    onClick={() => {
                      setManageUsageMode('24h')
                      void requestManageMetrics(true, true, '24h')
                    }}
                  >
                    Last 24h
                  </button>
                  <button
                    type="button"
                    className={`mg-usage-period-chip ${manageUsageMode === '7d' ? 'active' : ''}`}
                    onClick={() => {
                      setManageUsageMode('7d')
                      void requestManageMetrics(true, true, '7d')
                    }}
                  >
                    7 days
                  </button>
                  <button
                    type="button"
                    className={`mg-usage-period-chip ${manageUsageMode === '30d' ? 'active' : ''}`}
                    onClick={() => {
                      setManageUsageMode('30d')
                      void requestManageMetrics(true, true, '30d')
                    }}
                  >
                    30 days
                  </button>
                  <button
                    type="button"
                    className={`mg-usage-period-chip ${manageUsageMode === 'custom' ? 'active' : ''}`}
                    onClick={() => {
                      const defaultStart = defaultCustomUsageStartYmd(minSelectableYmd, maxSelectableYmd)
                      const nextStart = clampYmdToBounds(
                        manageUsageCustomStart || defaultStart,
                        minSelectableYmd,
                        maxSelectableYmd,
                      )
                      const nextEnd = clampYmdToBounds(
                        manageUsageCustomEnd || maxSelectableYmd,
                        nextStart || minSelectableYmd,
                        maxSelectableYmd,
                      )
                      setManageUsageCustomStart(nextStart || defaultStart)
                      setManageUsageCustomEnd(nextEnd || maxSelectableYmd)
                      setManageUsageMode('custom')
                    }}
                  >
                    Custom
                  </button>
                </div>
                {manageUsageMode === 'custom' ? (
                  <div className="mg-usage-period-custom">
                    <p className="mg-usage-period-calendar-hint">
                      Pick a bounded calendar range. {customRangeHint}
                    </p>
                    <div className="mg-usage-period-date-row">
                      <label className="mg-usage-period-field">
                        <span>Start date</span>
                        <input
                          type="date"
                          value={manageUsageCustomStart}
                          min={minSelectableYmd || undefined}
                          max={maxSelectableYmd}
                          onChange={(e) => {
                            const v = clampYmdToBounds(e.target.value, minSelectableYmd, maxSelectableYmd)
                            setManageUsageCustomStart(v)
                            setManageUsageCustomEnd((prev) => clampYmdToBounds(prev || maxSelectableYmd, v || minSelectableYmd, maxSelectableYmd))
                          }}
                        />
                        <span className="mg-usage-period-locale">
                          {manageUsageCustomStart ? formatYmdLocaleLong(manageUsageCustomStart) : '—'}
                        </span>
                      </label>
                      <label className="mg-usage-period-field">
                        <span>End date</span>
                        <span className="mg-usage-period-required">required</span>
                        <input
                          type="date"
                          value={manageUsageCustomEnd}
                          min={(manageUsageCustomStart || minSelectableYmd) || undefined}
                          max={maxSelectableYmd}
                          onChange={(e) => setManageUsageCustomEnd(clampYmdToBounds(e.target.value, manageUsageCustomStart || minSelectableYmd, maxSelectableYmd))}
                        />
                        <span className="mg-usage-period-locale">
                          {manageUsageCustomEnd ? formatYmdLocaleLong(manageUsageCustomEnd) : '—'}
                        </span>
                      </label>
                      <button
                        type="button"
                        className="mg-usage-period-apply"
                        disabled={!customRangeValid || manageMetricsRefreshing}
                        onClick={() => void requestManageMetrics(true, true, 'custom')}
                      >
                        Apply range
                      </button>
                    </div>
                  </div>
                ) : null}
              </div>
            </div>
            <div className="mg-ring-wrap">
              <svg viewBox="0 0 100 100" className="mg-ring-svg">
                <circle cx="50" cy="50" r="42" fill="none" stroke="rgba(255,255,255,0.045)" strokeWidth="7" />
                <circle cx="50" cy="50" r="42" fill="none" className={`mg-ring-arc ${meterAccent(budgetUsagePercent)}`}
                  strokeWidth="7" strokeDasharray={arcPath(budgetUsagePercent, 42)}
                  strokeLinecap="round" transform="rotate(-90 50 50)" />
              </svg>
              <div className="mg-ring-center">
                <strong>{budgetUsagePercent}%</strong>
              </div>
            </div>
          </div>
          <div className="mg-provider-row">
            {manageSnapshot.providers.length === 0 ? (
              <div className="mg-empty-hint" role="status">No provider spend in this window.</div>
            ) : (
              manageSnapshot.providers.map((p) => (
              <div key={p.name} className="mg-provider-chip">
                <span className={`mg-provider-dot ${p.accent}`} />
                <div className="mg-provider-info">
                  <span className="mg-provider-name">{p.name}</span>
                  <span className="mg-provider-sub">{p.tokens}</span>
                </div>
                <span className="mg-provider-cost">{p.cost}</span>
              </div>
            ))
            )}
          </div>
        </section>

        {/* ── Hardware gauges ── */}
        <section className="mg-hw">
          <div className="mg-section-top">
            <h3>Infrastructure</h3>
            <div className="mg-hw-badges">
              <span className={`mg-pill ${manageSnapshot.hasLiveData ? 'good' : 'muted'}`}>
                <span className="mg-pill-dot" />{manageSnapshot.hasLiveData ? 'Live' : 'Static'}
              </span>
              <span className="mg-pill muted">{manageSnapshot.region} &middot; {manageSnapshot.uptime}</span>
            </div>
          </div>
          <div className="mg-hw-instance">
            <strong>{manageSnapshot.instanceTitle}</strong>
            <span>{manageSnapshot.instanceMeta}</span>
          </div>
          <div className="mg-hw-meters">
            {hardwareRows.map((m) => (
              <div key={m.label} className="mg-meter">
                <div className="mg-meter-head">
                  <span className="mg-meter-label">{m.label}</span>
                  <span className="mg-meter-val">{m.value}</span>
                </div>
                <div className="mg-meter-track">
                  <div className={`mg-meter-fill ${meterAccent(m.pct)}`} style={{ width: `${m.pct}%` }} />
                </div>
              </div>
            ))}
          </div>
        </section>

        {/* ── Usage + Services ── */}
        <div className="mg-split">
          <section className="mg-card">
            <div className="mg-section-top">
              <h3>Usage by feature</h3>
            </div>
            <div className="mg-usage-list">
              {manageSnapshot.usage.length === 0 ? (
                <div className="mg-empty-hint" role="status">No feature usage in this window.</div>
              ) : (
                manageSnapshot.usage.map((u) => (
                <div key={u.label} className="mg-usage-row">
                  <span className={`mg-usage-dot ${u.accent}`} />
                  <span className="mg-usage-name">{u.label}</span>
                  <span className="mg-usage-calls">{u.calls}</span>
                  <div className="mg-usage-bar">
                    <div className={`mg-usage-fill ${u.accent}`} style={{ width: `${u.pct}%` }} />
                  </div>
                  <span className="mg-usage-pct">{u.pct}%</span>
                </div>
              ))
              )}
            </div>
          </section>

          <section className="mg-card">
            <div className="mg-section-top">
              <h3>Services</h3>
              <span className={`mg-pill ${degradedCount > 0 ? 'warm' : 'good'}`}>
                <span className="mg-pill-dot" />{liveServiceCount} live
              </span>
            </div>
            <div className="mg-service-list">
              {manageSnapshot.services.map((s) => (
                <div key={s.name} className={`mg-service-row ${s.status}`}>
                  <span className={`mg-service-dot ${s.status}`} />
                  <span className="mg-service-name">{s.name}</span>
                  <span className="mg-service-status">{s.status}</span>
                  <span className="mg-service-mem">{s.mem}</span>
                </div>
              ))}
            </div>
          </section>
        </div>

        {/* ── Footer bar ── */}
        <div className="mg-footer">
          <span className="mg-footer-source">{manageSnapshot.sourceEndpoint}</span>
          <span className="mg-footer-sep">&middot;</span>
          <span>Updated {lastUpdatedLabel}</span>
          <span className="mg-footer-sep">&middot;</span>
          <span>Refresh {MANAGE_REFRESH_MS / 1000}s</span>
          <button type="button" className="mg-refresh-btn"
            onClick={() => requestManageMetrics(true, true)} disabled={manageMetricsRefreshing}>
            {manageMetricsRefreshing ? 'Refreshing...' : 'Refresh now'}
          </button>
          {manageMetricsError ? <span className="mg-error">{manageMetricsError}</span> : null}
        </div>
      </div>
    )
  }

  const renderAgentsPage = () => {
    const lastUpdatedLabel = registryAgentsFetchedAt
      ? new Date(registryAgentsFetchedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
      : '—'
    const cardAccents: AccentTone[] = ['azure', 'mint', 'gold', 'rose', 'slate']
    const connectedCount = registryAgents.filter((a) => a.healthy_instance).length
    const uniqueIntentCount = new Set(registryAgents.flatMap((a) => a.intents)).size

    return (
      <div className="spaces-page spaces-agents-page">
        <header className="spaces-agents-hero">
          <div className="spaces-agents-hero-main">
            <h2 className="spaces-agents-hero-title">Agents</h2>
            <p className="spaces-agents-hero-lead">Read-only roster synced from the orchestrator registry.</p>
          </div>
          <div className="spaces-agents-hero-aside">
            <div className={`spaces-agents-pill spaces-agents-pill--gateway ${gatewayStatus.tone}`}>
              <span className="spaces-agents-pill-dot" aria-hidden />
              {gatewayStatus.label}
            </div>
            <div className="spaces-agents-pill spaces-agents-pill--badge">View only</div>
          </div>
        </header>

        {registryAgents.length > 0 ? (
          <div className="spaces-agents-stats" role="group" aria-label="Registry summary">
            <div className="spaces-agents-stat">
              <span className="spaces-agents-stat-value">{registryAgents.length}</span>
              <span className="spaces-agents-stat-label">Registered</span>
            </div>
            <div className="spaces-agents-stat">
              <span className="spaces-agents-stat-value">{connectedCount}</span>
              <span className="spaces-agents-stat-label">Connected</span>
            </div>
            <div className="spaces-agents-stat">
              <span className="spaces-agents-stat-value">{uniqueIntentCount}</span>
              <span className="spaces-agents-stat-label">Capabilities</span>
            </div>
          </div>
        ) : null}

        <div className="spaces-agents-controls">
          <div className="spaces-agents-controls-meta">
            <span className="spaces-agents-controls-updated">Updated {lastUpdatedLabel}</span>
            <span className="spaces-agents-controls-sep" aria-hidden>
              ·
            </span>
            <span className="spaces-agents-controls-interval">Auto-refresh every {SPACES_REGISTRY_REFRESH_MS / 1000}s</span>
          </div>
          <button
            type="button"
            className="spaces-agents-refresh"
            onClick={() => requestRegistryAgents(true)}
            disabled={registryAgentsRefreshing}
          >
            {registryAgentsRefreshing ? (
              <>
                <span className="spaces-agents-refresh-spinner" aria-hidden />
                Refreshing
              </>
            ) : (
              <>
                <span className="spaces-agents-refresh-glyph" aria-hidden>
                  ↻
                </span>
                Refresh
              </>
            )}
          </button>
        </div>

        {registryAgentsError ? <div className="spaces-agents-error">{registryAgentsError}</div> : null}

        {registryAgentsRefreshing && registryAgents.length === 0 && !registryAgentsError ? (
          <div className="spaces-agents-loading" role="status">
            <span className="spaces-agents-loading-line" />
            <span className="spaces-agents-loading-text">Loading registry</span>
          </div>
        ) : null}

        {registryAgents.length === 0 && !registryAgentsError && !registryAgentsRefreshing ? (
          <div className="spaces-agents-empty">
            <p className="spaces-agents-empty-title">No agents yet</p>
            <p className="spaces-agents-empty-body">When specialists register with your gateway, they will show up here with their capabilities.</p>
          </div>
        ) : null}

        {registryAgents.length > 0 ? (
          <section className="spaces-agents-catalog" aria-label="Registered specialist agents">
            <h3 className="spaces-agents-catalog-heading">All specialists</h3>
            <div className="spaces-agents-grid">
              {registryAgents.map((agent, index) => {
                const accent = cardAccents[index % cardAccents.length]
                const statusTone: MetricTone = agent.healthy_instance ? 'good' : 'warm'
                const statusLabel = agent.healthy_instance ? 'Connected' : 'Unavailable'
                const description =
                  agent.description ||
                  (agent.intents.length
                    ? `${agent.intents.slice(0, 4).join(' · ')}${agent.intents.length > 4 ? ' · …' : ''}`
                    : 'No description provided on this agent card.')
                return (
                  <article key={agent.agent_id} className={`spaces-registry-card ${accent}`}>
                    <div className="spaces-registry-card-top">
                      <div className="spaces-registry-card-title-block">
                        <h3 className="spaces-registry-card-title">{agent.display_name}</h3>
                        <div className="spaces-registry-card-id-wrap">
                          <code className="spaces-registry-card-id" title={agent.agent_id}>
                            {agent.agent_id}
                          </code>
                        </div>
                      </div>
                      <div className={`spaces-registry-status ${statusTone}`}>
                        <span className="spaces-registry-status-dot" aria-hidden />
                        <span className="spaces-registry-status-label">{statusLabel}</span>
                      </div>
                    </div>
                    <p className="spaces-registry-card-desc">{description}</p>
                    {agent.intents.length > 0 ? (
                      <div className="spaces-registry-foot">
                        <span className="spaces-registry-foot-label">Capabilities</span>
                        <div className="spaces-registry-intents" aria-label="Intents">
                          {agent.intents.map((intent) => (
                            <span key={`${agent.agent_id}-${intent}`} className="spaces-registry-intent-pill">
                              {intent}
                            </span>
                          ))}
                        </div>
                      </div>
                    ) : null}
                  </article>
                )
              })}
            </div>
          </section>
        ) : null}
      </div>
    )
  }

  const renderGmailPage = () => {
    const terminalCount = gmailApprovals.filter((approval) => approval.status !== 'pending').length
    const renderGmailEmptyState = (title: string, description: string) => (
      <div className="agent-email-empty-state">
        <strong>{title}</strong>
        <span>{description}</span>
      </div>
    )
    return (
      <div className="spaces-page agent-email-page">
        <section className="agent-email-minimal-tabs" aria-label="Gmail approval summary">
          <button type="button" className="agent-email-minimal-tab active">
            <span className="agent-email-tab-label">Approvals</span>
            <span className="agent-email-tab-signal">{pendingGmailApprovals} waiting</span>
          </button>
          <button type="button" className="agent-email-minimal-tab" onClick={() => void requestGmailApprovals(true)}>
            <span className="agent-email-tab-label">Refresh</span>
            <span className="agent-email-tab-signal">{gmailLoading ? 'Syncing' : `${terminalCount} reviewed`}</span>
          </button>
        </section>

        <section className="agent-email-minimal-shell">
          <div className="agent-email-minimal-shell-head">
            <div className="agent-email-minimal-shell-head-text">
              <span className="agent-email-shell-eyebrow">User Gmail</span>
              <h3>Outbound approval queue</h3>
              <p className="agent-email-shell-subtitle">
                Drafts created in connected Gmail accounts stay here until you approve or reject them.
              </p>
            </div>
            <div className="agent-email-shell-context" aria-label="Gmail approvals context">
              <span className="agent-email-context-chip agent-email-context-chip-warm">{pendingGmailApprovals} pending</span>
              <span className="agent-email-context-chip">{gmailApprovals.length} total</span>
            </div>
          </div>

          {gmailBanner ? <div className={`agent-email-banner ${gmailBanner.tone}`}>{gmailBanner.message}</div> : null}
          {gmailError ? <div className="agent-email-banner warning">{gmailError}</div> : null}

          {!gatewayConnected ? (
            renderGmailEmptyState('VM offline', 'Reconnect to the gateway to review Gmail drafts.')
          ) : gmailLoading && !gmailApprovals.length ? (
            <div className="agent-email-empty-state">
              <strong>Loading Gmail approvals</strong>
              <span>Checking the gateway approval queue.</span>
            </div>
          ) : !gmailApprovals.length ? (
            renderGmailEmptyState('No Gmail approvals waiting', 'When Cosmic creates a Gmail draft, it will appear here before sending.')
          ) : (
            <div className="agent-email-console-approvals">
              <div className="agent-email-console-approvals-list">
                <div className="agent-email-console-filterbar">
                  <span className="agent-email-console-copy">{pendingGmailApprovals} pending review</span>
                </div>
                <div className="agent-email-minimal-list compact">
                  {gmailApprovals.map((approval) => (
                    <button
                      key={approval.id}
                      type="button"
                      className={`agent-email-minimal-row ${gmailSelectedApproval?.id === approval.id ? 'active' : ''}`}
                      onClick={() => setGmailSelectedApprovalId(approval.id)}
                    >
                      <div className="agent-email-minimal-row-top">
                        <strong>{approval.subject}</strong>
                        <span className={`agent-email-minimal-pill ${approval.accent}`}>{approval.state}</span>
                      </div>
                      <div className="agent-email-minimal-row-meta">
                        <span>{approval.account}</span>
                        <span>{approval.time}</span>
                      </div>
                      <p>{approval.notes}</p>
                    </button>
                  ))}
                </div>
              </div>

              <div className="agent-email-console-approvals-detail">
                {gmailSelectedApproval ? (
                  <div className="agent-email-console-card">
                    <div className="agent-email-console-card-head">
                      <h4>{gmailSelectedApproval.subject}</h4>
                      <span className={`agent-email-minimal-pill ${gmailSelectedApproval.accent}`}>{gmailSelectedApproval.state}</span>
                    </div>
                    <div className="agent-email-console-detail-rows">
                      <div className="agent-email-console-detail-row"><span>Account</span><strong>{gmailSelectedApproval.account}</strong></div>
                      <div className="agent-email-console-detail-row"><span>To</span><strong>{gmailSelectedApproval.recipients}</strong></div>
                      {gmailSelectedApproval.cc ? (
                        <div className="agent-email-console-detail-row"><span>Cc</span><strong>{gmailSelectedApproval.cc}</strong></div>
                      ) : null}
                      {gmailSelectedApproval.bcc ? (
                        <div className="agent-email-console-detail-row"><span>Bcc</span><strong>{gmailSelectedApproval.bcc}</strong></div>
                      ) : null}
                      <div className="agent-email-console-detail-row"><span>Draft</span><strong className="agent-email-console-mono">{gmailSelectedApproval.draftId || '—'}</strong></div>
                      <div className="agent-email-console-detail-row"><span>Created</span><strong>{gmailSelectedApproval.time}</strong></div>
                    </div>
                    <div className="agent-email-console-text-block">
                      <span>Reason</span>
                      <p>{gmailSelectedApproval.notes}</p>
                    </div>
                    <div className="agent-email-console-text-block">
                      <span>Draft preview</span>
                      <p>{gmailSelectedApproval.excerpt}</p>
                    </div>
                    <div className="agent-email-detail-actions">
                      <button
                        type="button"
                        className="agent-email-console-primary"
                        onClick={() => void handleGmailApprove(gmailSelectedApproval.id)}
                        disabled={gmailActionId === gmailSelectedApproval.id || gmailSelectedApproval.status !== 'pending'}
                      >
                        {gmailActionId === gmailSelectedApproval.id ? 'Processing...' : 'Approve and send'}
                      </button>
                      <button
                        type="button"
                        className="agent-email-console-secondary"
                        onClick={() => void handleGmailReject(gmailSelectedApproval.id)}
                        disabled={gmailActionId === gmailSelectedApproval.id || gmailSelectedApproval.status !== 'pending'}
                      >
                        Reject
                      </button>
                    </div>
                  </div>
                ) : (
                  renderGmailEmptyState('Select an approval', 'Choose a Gmail draft to inspect it and release or reject it.')
                )}
              </div>
            </div>
          )}
        </section>
      </div>
    )
  }

  const renderSessionsPage = () => {
    const lastUpdatedLabel = sessionsFetchedAt
      ? new Date(sessionsFetchedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
      : '—'
    const selectedMessageCount = selectedSessionConversationMessages.length
    const selectedTraceCount = selectedSessionRequestTraces.length
    const selectedLastUpdatedLabel = selectedSessionFetchedAt
      ? new Date(selectedSessionFetchedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
      : '—'
    const sessionDetailMetaLine = [
      `Started ${formatSessionMetaStamp(selectedSession?.first_message_at ?? selectedSession?.created_at ?? null)}`,
      `Last ${formatSessionMetaStamp(selectedSession?.last_message_at ?? selectedSession?.updated_at ?? null)}`,
      `${selectedMessageCount} conversation ${selectedMessageCount === 1 ? 'message' : 'messages'}`,
      selectedSessionHiddenMessageCount > 0
        ? `${selectedSessionHiddenMessageCount} system ${selectedSessionHiddenMessageCount === 1 ? 'entry' : 'entries'} hidden`
        : null,
      selectedTraceCount > 0 ? `${selectedTraceCount} diagnostics` : null,
      selectedSessionCompactedSummary ? 'Summary on file' : 'No summary',
      `History ${selectedLastUpdatedLabel}`,
    ].filter(Boolean).join(' · ')

    return (
      <div className="spaces-page spaces-sessions-page">
        {sessionsError ? <div className="spaces-agents-error">{sessionsError}</div> : null}

        {sessionsRefreshing && sessionsList.length === 0 && !sessionsError ? (
          <div className="spaces-agents-loading" role="status">
            <span className="spaces-agents-loading-line" />
            <span className="spaces-agents-loading-text">Loading session lanes</span>
          </div>
        ) : null}

        {sessionsList.length === 0 && !sessionsError && !sessionsRefreshing ? (
          <div className="spaces-sessions-page-empty">
            <p className="spaces-agents-empty-title">No prior sessions yet</p>
            <p className="spaces-agents-empty-body">
              Once COSMIC rolls over or stores more daily lanes, they will appear here by date.
            </p>
          </div>
        ) : null}

        {sessionsList.length > 0 ? (
          <div
            className={`agent-email-inbox-layout spaces-sessions-archive-root ${sessionsListCollapsed ? 'spaces-sessions-rail-collapsed' : ''}`}
          >
            <aside
              className={`agent-email-inbox-sidebar spaces-sessions-rail ${sessionsListCollapsed ? 'is-collapsed' : ''}`}
              aria-label="Sessions by date"
            >
              {sessionsListCollapsed ? (
                <button
                  type="button"
                  className="spaces-sessions-rail-reveal"
                  onClick={() => setSessionsListCollapsed(false)}
                  aria-expanded="false"
                  aria-label="Expand session list"
                >
                  <span aria-hidden>›</span>
                </button>
              ) : (
                <>
                  <div className="agent-email-inbox-sidebar-top">
                    <div className="agent-email-inbox-sidebar-head">
                      <div className="agent-email-inbox-sidebar-title">
                        <div className="agent-email-inbox-mailbox-label-row">
                          <span className="agent-email-inbox-mailbox-label">Sessions</span>
                          <span className="agent-email-inbox-unread-badge" aria-label={`${sessionsList.length} lanes`}>
                            {sessionsList.length > 99 ? '99+' : sessionsList.length}
                          </span>
                        </div>
                        <h4 title={`Gateway list · Auto-refresh every ${SPACES_SESSIONS_REFRESH_MS / 1000}s`}>
                          Updated {lastUpdatedLabel} · {SPACES_SESSIONS_REFRESH_MS / 1000}s refresh
                        </h4>
                      </div>
                      <div className="spaces-sessions-rail-actions">
                        <button
                          type="button"
                          className="spaces-sessions-rail-collapse"
                          onClick={() => setSessionsListCollapsed(true)}
                          aria-expanded="true"
                          aria-label="Collapse session list"
                        >
                          <span aria-hidden>‹</span>
                        </button>
                        <button
                          type="button"
                          className="agent-email-inbox-sync"
                          onClick={() => requestSessionsList(true)}
                          disabled={sessionsRefreshing}
                        >
                          {sessionsRefreshing ? '…' : 'Sync'}
                        </button>
                      </div>
                    </div>
                  </div>
                  <div className="agent-email-inbox-list-shell">
                    <div className="agent-email-inbox-thread-list">
                      {groupedSessions.map((group) => (
                        <div key={group.key} className="spaces-sessions-day-group">
                          <div className="spaces-sessions-day-heading">{group.label}</div>
                          {group.sessions.map((session) => {
                            const isActive = session.id === selectedSessionId
                            const activityAt = session.last_message_at || session.updated_at || session.created_at
                            const relative = formatAgentEmailRelative(activityAt)
                            const clock = formatSessionRowTime(activityAt)
                            return (
                              <button
                                key={session.id}
                                type="button"
                                className={`agent-email-thread-item ${isActive ? 'active' : ''}`}
                                onClick={() => setSelectedSessionId(session.id)}
                              >
                                <div className="agent-email-thread-row">
                                  <div className="agent-email-thread-avatar" aria-hidden>
                                    {getAgentEmailInitials(session.title)}
                                  </div>
                                  <div className="agent-email-thread-main">
                                    <div className="agent-email-thread-line1">
                                      <span className="agent-email-thread-subject">{session.title}</span>
                                      <span className="agent-email-thread-time">{clock}</span>
                                    </div>
                                    <div className="agent-email-thread-line2">
                                      <span className="agent-email-thread-from">{relative}</span>
                                    </div>
                                  </div>
                                </div>
                              </button>
                            )
                          })}
                        </div>
                      ))}
                    </div>
                  </div>
                </>
              )}
            </aside>

            <section className="agent-email-inbox-pane" aria-label="Selected session detail">
              {selectedSession ? (
                <div className="agent-email-inbox-pane-column">
                  <header className="agent-email-inbox-read-head">
                    <div className="agent-email-inbox-read-hero">
                      <div className="agent-email-inbox-read-avatar" aria-hidden>
                        {getAgentEmailInitials(selectedSession.title)}
                      </div>
                      <div className="agent-email-inbox-read-hero-main">
                        <div className="agent-email-inbox-read-title-row">
                          <h4 className="agent-email-inbox-read-subject">{selectedSession.title}</h4>
                          <span className="agent-email-inbox-status is-ok">
                            {selectedSessionCompactedSummary ? 'Compacted' : 'Live'}
                          </span>
                        </div>
                        <div className="spaces-sessions-read-sub">
                          <p className="spaces-sessions-read-session-id" title={selectedSession.id}>
                            {selectedSession.id}
                          </p>
                          <p className="spaces-sessions-read-meta-line">{sessionDetailMetaLine}</p>
                        </div>
                      </div>
                    </div>
                  </header>

                  <div className="spaces-sessions-detail-scroll-wrap">
                    <div
                      ref={sessionsDetailScrollRef}
                      className="agent-email-inbox-messages-scroll"
                      onScroll={updateSessionsJumpVisibility}
                    >
                      {selectedSessionError ? <div className="spaces-agents-error">{selectedSessionError}</div> : null}

                      {selectedSessionLoading ? (
                        <div className="spaces-sessions-detail-placeholder">
                          <strong>Loading session detail</strong>
                          <p>Pulling the selected lane from Gateway.</p>
                        </div>
                      ) : null}

                      {!selectedSessionLoading && selectedSessionCompactedSummary ? (
                        <section className="agent-email-console-card spaces-sessions-summary-card">
                          <div className="agent-email-console-card-head">
                            <h4>Compacted summary</h4>
                            <span className="agent-email-console-muted">Rollover memory</span>
                          </div>
                          <div className="spaces-sessions-summary">{selectedSessionCompactedSummary}</div>
                        </section>
                      ) : null}

                      {!selectedSessionLoading ? (
                        <section className="agent-email-console-card spaces-sessions-transcript-card">
                          <div className="agent-email-console-card-head">
                            <h4>Conversation</h4>
                            <span className="agent-email-console-muted">
                              {selectedSessionConversationMessages.length} clean messages
                              {selectedSessionHiddenMessageCount > 0
                                ? ` · ${selectedSessionHiddenMessageCount} system entries hidden`
                                : ''}
                            </span>
                          </div>
                          {selectedSessionConversationMessages.length > 0 ? (
                            <div className="spaces-sessions-transcript spaces-sessions-transcript--chat">
                              {selectedSessionConversationMessages.map((message) => {
                                const roleKey = String(message.role || '').toLowerCase()
                                const chatLane = roleKey === 'user' ? 'user' : 'assistant'
                                const channelLabel = sessionChannelLabel(message.channel)
                                const requestLabel = message.request_id ? message.request_id.slice(0, 12) : null
                                return (
                                  <article
                                    key={message.id}
                                    className={`spaces-sessions-msg spaces-sessions-msg--${chatLane}`}
                                  >
                                    <div className="spaces-sessions-msg-inner">
                                      <header className="spaces-sessions-msg-head">
                                        <div className="spaces-sessions-msg-who-wrap">
                                          <span className="spaces-sessions-msg-who">{sessionRoleLabel(message.role)}</span>
                                          <span className="spaces-sessions-msg-pill">{channelLabel}</span>
                                          {requestLabel ? (
                                            <span className="spaces-sessions-msg-pill spaces-sessions-msg-pill--subtle">
                                              {requestLabel}
                                            </span>
                                          ) : null}
                                        </div>
                                        <time className="spaces-sessions-msg-time">{formatSessionAbsolute(message.created_at)}</time>
                                      </header>
                                      <div className="spaces-sessions-msg-body">
                                        <SessionMessageMarkdown source={message.content} />
                                      </div>
                                    </div>
                                  </article>
                                )
                              })}
                            </div>
                          ) : (
                            <div className="spaces-sessions-detail-placeholder">
                              <strong>No conversation messages</strong>
                              <p>Only system, rollover, or transport records were found for this session.</p>
                            </div>
                          )}
                        </section>
                      ) : null}

                      {!selectedSessionLoading ? (
                        <section className="agent-email-console-card spaces-sessions-traces-card">
                          <button
                            type="button"
                            className="spaces-sessions-diagnostics-toggle"
                            onClick={() => setSessionsDiagnosticsOpen((open) => !open)}
                            aria-expanded={sessionsDiagnosticsOpen}
                          >
                            <span className="spaces-sessions-diagnostics-main">
                              <span className="spaces-sessions-diagnostics-title">Diagnostics</span>
                              <span className="spaces-sessions-diagnostics-sub">
                                {selectedSessionTracesLoading
                                  ? 'Loading request traces'
                                  : selectedSessionTraceLoadedForId === selectedSessionId
                                    ? `${selectedSessionRequestTraces.length} request ${selectedSessionRequestTraces.length === 1 ? 'trace' : 'traces'}`
                                    : 'Open to load request traces'}
                                {selectedSessionTraceError ? ' · load issue' : ''}
                              </span>
                            </span>
                            <span className="spaces-sessions-diagnostics-action">
                              {sessionsDiagnosticsOpen ? 'Hide' : 'Show'}
                            </span>
                          </button>
                          {sessionsDiagnosticsOpen && selectedSessionTraceError ? (
                            <div className="spaces-agents-error">{selectedSessionTraceError}</div>
                          ) : null}
                          {sessionsDiagnosticsOpen && selectedSessionTracesLoading ? (
                            <div className="spaces-sessions-detail-placeholder">
                              <strong>Loading diagnostics</strong>
                              <p>Fetching request traces only because Diagnostics is open.</p>
                            </div>
                          ) : sessionsDiagnosticsOpen && !selectedSessionTraceError && selectedSessionRequestTraces.length > 0 ? (
                            <div className="spaces-session-trace-list">
                              {selectedSessionRequestTraces.map((trace) => {
                                const deliveryStatus = typeof trace.delivery?.status === 'string' ? trace.delivery.status : ''
                                return (
                                  <article key={trace.request_id} className="spaces-session-trace-card">
                                    <header className="spaces-session-trace-head">
                                      <div>
                                        <div className="spaces-session-trace-kicker">Request</div>
                                        <h5 title={trace.request_id}>{trace.user_query_excerpt || trace.request_id}</h5>
                                      </div>
                                      <div className="spaces-session-trace-badges">
                                        <span className={`spaces-session-trace-pill is-${trace.status}`}>{trace.status}</span>
                                        {deliveryStatus ? (
                                          <span className="spaces-session-trace-pill is-delivery">{deliveryStatus}</span>
                                        ) : null}
                                      </div>
                                    </header>
                                    <div className="spaces-session-trace-meta">
                                      <span title={trace.request_id}>{trace.request_id}</span>
                                      <span>{trace.route}</span>
                                      <span>{trace.channel}</span>
                                      {trace.task_id ? <span title={trace.task_id}>task {trace.task_id}</span> : null}
                                      {trace.updated_at ? <span>{formatSessionAbsolute(trace.updated_at)}</span> : null}
                                    </div>
                                    {trace.specialist_receipts.length > 0 ? (
                                      <div className="spaces-session-trace-specialists">
                                        {trace.specialist_receipts.map((receipt, index) => {
                                          const label = String(receipt.agent_label || receipt.intent || receipt.agent_id || `specialist-${index + 1}`)
                                          const provider = typeof receipt.provider === 'string' ? receipt.provider : ''
                                          const model = typeof receipt.model === 'string' ? receipt.model : ''
                                          const suffix = [provider, model].filter(Boolean).join(':')
                                          return (
                                            <span key={`${trace.request_id}-receipt-${index}`} className="spaces-session-trace-specialist-pill">
                                              {suffix ? `${label} · ${suffix}` : label}
                                            </span>
                                          )
                                        })}
                                      </div>
                                    ) : null}
                                    {trace.events.length > 0 ? (
                                      <div className="spaces-session-trace-timeline">
                                        {trace.events.map((event, index) => (
                                          <div key={`${trace.request_id}-event-${index}`} className="spaces-session-trace-event">
                                            <div className="spaces-session-trace-event-head">
                                              <span className="spaces-session-trace-event-title">{event.title}</span>
                                              <span className={`spaces-session-trace-event-pill is-${event.status}`}>{event.status}</span>
                                            </div>
                                            <div className="spaces-session-trace-event-meta">
                                              <span>{event.stage}</span>
                                              {event.at ? <span>{formatSessionAbsolute(event.at)}</span> : null}
                                            </div>
                                            {event.detail ? <p className="spaces-session-trace-event-detail">{event.detail}</p> : null}
                                          </div>
                                        ))}
                                      </div>
                                    ) : null}
                                  </article>
                                )
                              })}
                            </div>
                          ) : sessionsDiagnosticsOpen && !selectedSessionTraceError ? (
                            <div className="spaces-sessions-detail-placeholder">
                              <strong>No request traces yet</strong>
                              <p>Gateway has not recorded request-level execution detail for this session yet.</p>
                            </div>
                          ) : null}
                        </section>
                      ) : null}
                      <div ref={sessionsDetailAnchorRef} className="spaces-sessions-scroll-anchor" aria-hidden />
                    </div>
                    {sessionsJumpToBottomVisible ? (
                      <button
                        type="button"
                        className="spaces-sessions-jump-bottom"
                        onClick={() => scrollSessionsDetailToBottom(true)}
                        aria-label="Jump to latest message"
                      >
                        <span className="spaces-sessions-jump-bottom-icon" aria-hidden>
                          ↓
                        </span>
                        <span>Latest</span>
                      </button>
                    ) : null}
                  </div>
                </div>
              ) : (
                <div className="agent-email-inbox-pane-empty">
                  <div className="spaces-sessions-detail-placeholder">
                    <strong>Select a session</strong>
                    <p>Pick a lane on the left to load its summary and transcript.</p>
                  </div>
                </div>
              )}
            </section>
          </div>
        ) : null}
      </div>
    )
  }

  const renderCurrentPage = () => {
    if (page === 'tools') {
      return renderToolsPage()
    }
    if (page === 'calendar') {
      return renderCalendarPage()
    }
    if (page === 'prophet') {
      return renderProphetPage()
    }
    if (page === 'autopilot') {
      return renderAutopilotPage()
    }
    if (page === 'pulse') {
      return renderPulsePage()
    }
    if (page === 'manage') {
      return renderManagePage()
    }
    if (page === 'agents') {
      return renderAgentsPage()
    }
    if (page === 'sessions') {
      return renderSessionsPage()
    }
    if (page === 'agent-email') {
      return renderAgentEmailPage()
    }
    if (page === 'gmail') {
      return renderGmailPage()
    }
    return renderCommandPage()
  }

  return (
    <div
      ref={containerRef}
      className={`response-container spaces-shell ${active ? 'spaces-open' : 'spaces-closed'} ${containerClassName || ''}`}
      style={containerStyle}
      aria-hidden={!active}
    >
      <LiquidGlass disableTilt={true} cornerRadius={32} visualTone="stealth" style={{ width: '100%', height: '100%' }}>
        <div className="response-wrapper">
          <div className="spaces-root">
            <header className="spaces-header">
              <div className="spaces-header-left">
                <div className="spaces-header-kicker">Launcher surface</div>
                <div className="spaces-header-page">{currentPage.label}</div>
                <p className="spaces-header-copy">{currentPage.kicker}</p>
              </div>
              <div className="spaces-header-brand" aria-label="COSMIC">
                C O S M I C
              </div>
              <div className="spaces-header-actions">
                <button type="button" className="spaces-action-btn" onClick={onBackToChat}>
                  <SpacesActionIcon kind="chat" />
                  <span>Open chat</span>
                </button>
                <div className="spaces-window-controls" aria-label="Spaces window controls">
                  <button
                    type="button"
                    className="spaces-window-control minimize"
                    aria-label="Minimize to launcher"
                    title="Minimize to launcher"
                    onClick={onMinimize}
                  >
                    <SpacesActionIcon kind="minimize" />
                  </button>
                  <button
                    type="button"
                    className="spaces-window-control close"
                    aria-label="Close spaces"
                    title="Close spaces"
                    onClick={onClose}
                  >
                    <SpacesActionIcon kind="close" />
                  </button>
                </div>
              </div>
            </header>

            <div className="spaces-body">
              <aside className={`spaces-rail ${railCollapsed ? 'collapsed' : ''}`} aria-label="Space Control Center sections">
                <button
                  type="button"
                  className="spaces-rail-toggle"
                  onClick={() => setRailCollapsed(!railCollapsed)}
                  title={railCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
                  aria-label={railCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
                >
                  <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                    <path d="M9 3L5 7L9 11" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </button>
                <div className="spaces-rail-head">
                  <div className="spaces-card-kicker">Control map</div>
                  <div className="spaces-rail-copy">Start broad, then drill deeper.</div>
                </div>
                <div className="spaces-rail-list">
                  {SPACE_PAGES.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      className={`spaces-rail-button ${item.accent} ${page === item.id ? 'active' : ''}`}
                      onClick={() => setPage(item.id)}
                      aria-current={page === item.id ? 'page' : undefined}
                      onMouseEnter={active && railCollapsed && onShowTooltip ? (e) => onShowTooltip(item.label, e.currentTarget) : undefined}
                      onMouseLeave={active && railCollapsed && onHideTooltip ? onHideTooltip : undefined}
                    >
                      <span className="spaces-rail-icon">
                        <SpacesNavIcon page={item.id} />
                        {item.id === 'agent-email' && agentEmailSidebarAttentionCount > 0 ? (
                          <span className="spaces-rail-badge" aria-label={`${agentEmailSidebarAttentionCount} unread or pending`}>
                            {agentEmailSidebarAttentionCount > 99 ? '99+' : agentEmailSidebarAttentionCount}
                          </span>
                        ) : null}
                        {item.id === 'gmail' && pendingGmailApprovals > 0 ? (
                          <span className="spaces-rail-badge" aria-label={`${pendingGmailApprovals} Gmail approvals pending`}>
                            {pendingGmailApprovals > 99 ? '99+' : pendingGmailApprovals}
                          </span>
                        ) : null}
                      </span>
                      <span className="spaces-rail-text">
                        <span className="spaces-rail-label">{item.label}</span>
                        <span className="spaces-rail-kicker">{item.kicker}</span>
                      </span>
                      <span className="spaces-rail-meta">{item.countLabel}</span>
                    </button>
                  ))}
                </div>
              </aside>

              <main className="spaces-main" role="main">
                {renderCurrentPage()}
              </main>
            </div>
          </div>
        </div>
      </LiquidGlass>
    </div>
  )
}

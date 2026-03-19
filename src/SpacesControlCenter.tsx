import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, RefObject } from 'react'
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

type SpacesPageId = 'command' | 'calendar' | 'prophet' | 'autopilot' | 'pulse' | 'manage'
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
  onMinimize: () => void
  onClose: () => void
  onShowTooltip?: (label: string, element: HTMLElement) => void
  onHideTooltip?: () => void
  containerRef?: RefObject<HTMLDivElement | null>
  containerClassName?: string
  containerStyle?: CSSProperties
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
  vm?: Record<string, unknown> | null
}

const MANAGE_REFRESH_MS = 30_000
const MANAGE_REFRESH_TIMEOUT_MS = 8_000
const MANAGE_PROVIDER_ACCENTS: AccentTone[] = ['azure', 'gold', 'mint', 'rose', 'slate']

const MANAGE_PROVIDER_FALLBACK: ManageProviderDatum[] = [
  { name: 'Anthropic', role: 'Orchestration', tokens: '1.82M tokens', cost: '$14.20', pct: 70, accent: 'azure' },
  { name: 'Perplexity', role: 'Search & vectors', tokens: '420K tokens', cost: '$3.60', pct: 18, accent: 'gold' },
  { name: 'Deepgram', role: 'Voice', tokens: '6.2 hrs audio', cost: '$2.10', pct: 10, accent: 'mint' },
  { name: 'Groq', role: 'Fast inference', tokens: '310K tokens', cost: '$0.45', pct: 2, accent: 'rose' },
]

const MANAGE_USAGE_FALLBACK: ManageUsageDatum[] = [
  { label: 'Chat', calls: '1,240', pct: 48, accent: 'azure' },
  { label: 'Tasks', calls: '386', pct: 22, accent: 'mint' },
  { label: 'Cron jobs', calls: '214', pct: 16, accent: 'gold' },
  { label: 'Voice', calls: '78', pct: 9, accent: 'rose' },
  { label: 'Search', calls: '52', pct: 5, accent: 'slate' },
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

function toErrorMessage(error: unknown): string {
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
  return 'Unable to fetch live VM metrics.'
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
  const records = toArray(toRecord(source)?.providers || source)
  if (!records.length) return MANAGE_PROVIDER_FALLBACK
  return records.slice(0, 5).map((record, index) => {
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

function parseLiveUsage(source: unknown): ManageUsageDatum[] {
  const rows = toArray(toRecord(source)?.usage_by_feature || source)
  if (!rows.length) return MANAGE_USAGE_FALLBACK
  return rows.slice(0, 6).map((record, index) => {
    const sourceRecord = toRecord(record)
    const calls = pickNumber(sourceRecord, ['count', 'calls', 'requests']) || 0
    const pct = pickNumber(sourceRecord, ['percent', 'share', 'ratio']) || 0
    const label = pickString(sourceRecord, ['label', 'feature', 'name']) || `Feature ${index + 1}`
    return {
      label,
      calls: formatNumberShort(calls, 'calls'),
      pct: normalizePercent(pct),
      accent: MANAGE_PROVIDER_ACCENTS[index % MANAGE_PROVIDER_ACCENTS.length],
    }
  })
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
  { id: 'calendar', label: 'My Calendar', kicker: 'Your schedule at a glance', countLabel: '07 days', accent: 'gold' },
  { id: 'prophet', label: 'My Prophet', kicker: 'Your curated daily edition', countLabel: 'Live', accent: 'rose' },
  { id: 'autopilot', label: 'Autopilot', kicker: 'Autonomous routines', countLabel: '04 routines', accent: 'mint' },
  { id: 'pulse', label: 'Pulse', kicker: 'Health and usage', countLabel: '04 signals', accent: 'rose' },
  { id: 'manage', label: 'Manage', kicker: 'Resources & billing', countLabel: 'Live', accent: 'slate' },
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
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3.5 12h3.5l2-5.5 3 11 2.5-5.5h6" />
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
  onMinimize,
  onClose,
  onShowTooltip,
  onHideTooltip,
  containerRef,
  containerClassName,
  containerStyle,
}: SpacesControlCenterProps) {
  const [page, setPage] = useState<SpacesPageId>('command')
  const [railCollapsed, setRailCollapsed] = useState(false)

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
  const manageLastUpdatedAtRef = useRef<number | null>(null)
  const manageMetricsRequestRef = useRef(0)

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
    if (calendarRefreshTimeoutRef.current) clearTimeout(calendarRefreshTimeoutRef.current)
    }
  }, [requestCalendarAgenda])

  const requestManageMetrics = useCallback(async (showSpinner = false) => {
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

    try {
      const snapshot = await window.cosmic.getGatewaySystemMetrics()
      if (requestId !== manageMetricsRequestRef.current) {
        return
      }
      setManageMetrics(snapshot ? (snapshot as GatewaySystemMetrics) : null)
      setManageMetricsError(null)
      const now = Date.now()
      setManageLastUpdatedAt(now)
      manageLastUpdatedAtRef.current = now
    } catch (error: unknown) {
      if (requestId !== manageMetricsRequestRef.current) {
        return
      }
      setManageMetricsError(toErrorMessage(error))
      setManageMetrics(null)
      manageLastUpdatedAtRef.current = null
      setManageLastUpdatedAt(null)
    } finally {
      if (requestId === manageMetricsRequestRef.current) {
        if (manageMetricsRefreshRef.current) {
          clearTimeout(manageMetricsRefreshRef.current)
          manageMetricsRefreshRef.current = null
        }
        setManageMetricsRefreshing(false)
      }
    }
  }, [gatewayConnected, gatewayDetail])

  useEffect(() => {
    if (!active || page !== 'manage') return
    requestManageMetrics(true)
    if (manageMetricsIntervalRef.current) {
      clearInterval(manageMetricsIntervalRef.current)
      manageMetricsIntervalRef.current = null
    }
    manageMetricsIntervalRef.current = setInterval(() => requestManageMetrics(false), MANAGE_REFRESH_MS)

    const offShown = window.cosmic?.onShown?.(() => {
      if (Date.now() - (manageLastUpdatedAtRef.current || 0) > MANAGE_REFRESH_MS) {
        requestManageMetrics(false)
      }
    })

    return () => {
      if (manageMetricsIntervalRef.current) {
        clearInterval(manageMetricsIntervalRef.current)
        manageMetricsIntervalRef.current = null
      }
      if (manageMetricsRefreshRef.current) {
        clearTimeout(manageMetricsRefreshRef.current)
        manageMetricsRefreshRef.current = null
      }
      offShown?.()
    }
    }, [active, page, requestManageMetrics])

  const gatewayStatus = useMemo(() => normalizeGatewayState(gatewayState), [gatewayState])
  const today = useMemo(() => new Date(), [])
  const manageSnapshot = useMemo(() => buildManageSnapshot(manageMetrics), [manageMetrics])

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

  const currentPage = SPACE_PAGES.find((item) => item.id === page) || SPACE_PAGES[0]

  /* ── PAGE RENDERERS ───────────────────────────────────── */

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
                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/>
                </svg>
                Back to Calendar
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

        {/* Stories by section — clean two-column grid */}
        <div className="prophet-body">
          {prophetSections.map((section) => (
            <section key={section.key} className="prophet-section">
              <div className="prophet-section-head">
                <h2 className="prophet-section-title">{section.label}</h2>
                <div className="prophet-section-rule" />
              </div>
              <div className="prophet-section-stories">
                {section.articles.map((article) => (
                  <article key={article.id} className="prophet-story">
                    <h3 className="prophet-story-headline">{article.headline}</h3>
                    <p className="prophet-story-body">{article.summary}</p>
                    <div className="prophet-story-byline">
                      <span>{article.source}</span>
                      <span className="prophet-byline-dot" />
                      <span>{article.timeAgo}</span>
                    </div>
                  </article>
                ))}
              </div>
            </section>
          ))}
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

  const renderManagePage = () => {
    const budgetUsagePercent = manageSnapshot.budgetTotal
      ? Math.min(100, Math.max(0, Math.round((manageSnapshot.budgetUsed / manageSnapshot.budgetTotal) * 100)))
      : 0
    const currencySymbol = manageSnapshot.budgetCurrency === 'USD' ? '$' : `${manageSnapshot.budgetCurrency} `
    const budgetCap = `${currencySymbol}${manageSnapshot.budgetTotal.toFixed(2)}`
    const lastUpdatedLabel = manageLastUpdatedAt
      ? new Date(manageLastUpdatedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
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
      { label: 'Memory', value: `${manageSnapshot.memoryUsedText} / ${manageSnapshot.memoryTotalText}`, pct: manageSnapshot.memoryPercent },
      { label: 'Disk', value: `${manageSnapshot.diskUsedText} / ${manageSnapshot.diskTotalText}`, pct: manageSnapshot.diskPercent },
      { label: 'Network', value: manageSnapshot.networkThroughput, pct: networkPercent },
    ]

    const arcPath = (pct: number, r: number) => {
      const c = 2 * Math.PI * r
      return `${(pct / 100) * c} ${c}`
    }

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
            {manageSnapshot.providers.map((p) => (
              <div key={p.name} className="mg-provider-chip">
                <span className={`mg-provider-dot ${p.accent}`} />
                <div className="mg-provider-info">
                  <span className="mg-provider-name">{p.name}</span>
                  <span className="mg-provider-sub">{p.tokens}</span>
                </div>
                <span className="mg-provider-cost">{p.cost}</span>
              </div>
            ))}
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
              {manageSnapshot.usage.map((u) => (
                <div key={u.label} className="mg-usage-row">
                  <span className={`mg-usage-dot ${u.accent}`} />
                  <span className="mg-usage-name">{u.label}</span>
                  <span className="mg-usage-calls">{u.calls}</span>
                  <div className="mg-usage-bar">
                    <div className={`mg-usage-fill ${u.accent}`} style={{ width: `${u.pct}%` }} />
                  </div>
                  <span className="mg-usage-pct">{u.pct}%</span>
                </div>
              ))}
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
            onClick={() => requestManageMetrics(true)} disabled={manageMetricsRefreshing}>
            {manageMetricsRefreshing ? 'Refreshing...' : 'Refresh now'}
          </button>
          {manageMetricsError ? <span className="mg-error">{manageMetricsError}</span> : null}
        </div>
      </div>
    )
  }

  const renderCurrentPage = () => {
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
                      onMouseEnter={railCollapsed && onShowTooltip ? (e) => onShowTooltip(item.label, e.currentTarget) : undefined}
                      onMouseLeave={railCollapsed && onHideTooltip ? onHideTooltip : undefined}
                    >
                      <span className="spaces-rail-icon">
                        <SpacesNavIcon page={item.id} />
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

import { BrowserWindow } from 'electron'
import WebSocket, { RawData } from 'ws'

export interface GatewayConnectionConfig {
  baseUrl: string
  apiToken: string
  deviceId: string
}

export interface GatewayConnectionStatus {
  state: 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'error'
  connected: boolean
  detail?: string
  attempt?: number
  deviceId?: string | null
  sessionId?: string | null
}

interface DesktopQueryAttachment {
  artifact_id?: string
  kind?: string
  mime?: string
  mime_type?: string
  filename?: string
  size_bytes?: number | null
  sha256?: string | null
  path?: string | null
  ingest_state?: string | null
  parse_task_id?: string | null
  parse_bundle_id?: string | null
  parsed_summary?: Record<string, unknown> | null
  metadata?: Record<string, unknown> | null
}

interface ForegroundStreamSnapshot {
  stream_key: string
  request_id?: string | null
  task_id?: string | null
  session_id?: string | null
  route?: string | null
  message_id?: string | null
  content?: string
  thinking_text?: string
  activity?: string
  activity_log?: any[]
  docs_progress?: unknown
  tabular_progress?: unknown
  produced_artifacts?: any[]
  response_blocks?: any[]
  sources?: any[]
  channel?: string | null
  source?: string | null
  source_id?: string | null
  awaiting_reply?: boolean
  completed?: boolean
  failed?: boolean
  error?: string | null
  updated_at: string
}

const CONNECT_TIMEOUT_MS = 15000
const HEARTBEAT_INTERVAL_MS = 25000
const HEARTBEAT_STALE_MS = 70000
const RESUME_TIMEOUT_MS = 10000
const MAX_RESUME_ATTEMPTS = 2
const FOREGROUND_STREAM_TTL_MS = 10 * 60 * 1000

function appendCachedStreamText(current: string | undefined, incoming: unknown): string {
  const prev = String(current || '')
  const next = String(incoming || '')
  if (!next) {
    return prev
  }
  if (!prev) {
    return next
  }

  const prevEnd = prev.slice(-1)
  const nextStart = next.slice(0, 1)
  if (!prevEnd || !nextStart || /\s/.test(prevEnd) || /\s/.test(nextStart)) {
    return `${prev}${next}`
  }
  if (/[\.\!\?\:\u2026]/.test(prevEnd) && /[A-Z0-9"'`(\[]/.test(nextStart)) {
    return `${prev}\n\n${next}`
  }
  if (/[A-Za-z0-9]/.test(prevEnd) && /[A-Za-z0-9]/.test(nextStart)) {
    return `${prev} ${next}`
  }
  return `${prev}${next}`
}

function mergeCachedCompletedText(current: string | undefined, completed: unknown): string {
  const prev = String(current || '')
  const finalText = String(completed || '')
  if (!prev) {
    return finalText
  }
  if (!finalText) {
    return prev
  }
  const normalizedPrev = prev.replace(/\s+/g, ' ').trim()
  const normalizedFinal = finalText.replace(/\s+/g, ' ').trim()
  if (normalizedPrev && normalizedFinal && normalizedPrev === normalizedFinal) {
    return prev
  }
  if (normalizedPrev && normalizedFinal && normalizedFinal.startsWith(normalizedPrev)) {
    return finalText
  }
  return finalText
}

function appendCachedActivityLog(
  current: any[] | undefined,
  entry: {
    label: string
    detail?: string
    status?: string | null
    stage?: string | null
    kind?: string | null
    flow_role?: string | null
    delegated_task_id?: string | null
    parent_delegated_task_id?: string | null
    specialist_task_id?: string | null
    agent_id?: string | null
    agent_label?: string | null
    intent?: string | null
    specialist_event_type?: string | null
  },
) {
  const label = String(entry.label || '').trim()
  if (!label) {
    return Array.isArray(current) ? current : undefined
  }
  const existing = Array.isArray(current) ? current : []
  const nextEntry = {
    id: `activity_${crypto.randomUUID()}`,
    createdAt: new Date().toISOString(),
    label,
    detail: typeof entry.detail === 'string' && entry.detail.trim() ? entry.detail.trim() : undefined,
    status: typeof entry.status === 'string' && entry.status.trim() ? entry.status.trim() : null,
    stage: typeof entry.stage === 'string' && entry.stage.trim() ? entry.stage.trim() : null,
    kind: typeof entry.kind === 'string' && entry.kind.trim() ? entry.kind.trim() : null,
    flow_role: typeof entry.flow_role === 'string' && entry.flow_role.trim() ? entry.flow_role.trim() : null,
    delegated_task_id: typeof entry.delegated_task_id === 'string' && entry.delegated_task_id.trim() ? entry.delegated_task_id.trim() : null,
    parent_delegated_task_id: typeof entry.parent_delegated_task_id === 'string' && entry.parent_delegated_task_id.trim() ? entry.parent_delegated_task_id.trim() : null,
    specialist_task_id: typeof entry.specialist_task_id === 'string' && entry.specialist_task_id.trim() ? entry.specialist_task_id.trim() : null,
    agent_id: typeof entry.agent_id === 'string' && entry.agent_id.trim() ? entry.agent_id.trim() : null,
    agent_label: typeof entry.agent_label === 'string' && entry.agent_label.trim() ? entry.agent_label.trim() : null,
    intent: typeof entry.intent === 'string' && entry.intent.trim() ? entry.intent.trim() : null,
    specialist_event_type: typeof entry.specialist_event_type === 'string' && entry.specialist_event_type.trim() ? entry.specialist_event_type.trim() : null,
  }
  const last = existing[existing.length - 1]
  if (
    last &&
    String(last.label || '') === nextEntry.label &&
    String(last.detail || '') === String(nextEntry.detail || '') &&
    String(last.status || '') === String(nextEntry.status || '') &&
    String(last.stage || '') === String(nextEntry.stage || '') &&
    String(last.kind || '') === String(nextEntry.kind || '') &&
    String((last as any).flow_role || '') === String(nextEntry.flow_role || '') &&
    String((last as any).delegated_task_id || '') === String(nextEntry.delegated_task_id || '') &&
    String((last as any).parent_delegated_task_id || '') === String(nextEntry.parent_delegated_task_id || '') &&
    String((last as any).specialist_task_id || '') === String(nextEntry.specialist_task_id || '')
  ) {
    return existing
  }
  return [...existing, nextEntry]
}

function formatSpecialistAgentLabel(value: unknown) {
  const raw = String(value || '').trim()
  if (!raw) {
    return 'Specialist'
  }
  const normalized = raw
    .replace(/^cosmic\//i, '')
    .replace(/:.*$/, '')
    .replace(/[-_]/g, ' ')
    .trim()
  if (!normalized) {
    return raw
  }
  return normalized.charAt(0).toUpperCase() + normalized.slice(1)
}

function buildCachedProgressEntries(
  payload: any,
  activityText: string,
  statusMessage: string,
  progressStage: string | null,
  progressKind: string,
) {
  const specialistDelegations = Array.isArray(payload?.specialist_delegations)
    ? payload.specialist_delegations
    : []
  if (specialistDelegations.length > 0) {
    return specialistDelegations.map((item: any) => ({
      label: String(item?.activity || '').trim() || `Delegated ${String(item?.intent || 'specialist work').trim()}`,
      detail: statusMessage || undefined,
      status: typeof payload?.status === 'string' ? payload.status.trim() : null,
      stage: progressStage,
      kind: 'delegation',
      flow_role: 'delegation',
      delegated_task_id: String(item?.task_id || '').trim() || null,
      agent_id: String(item?.agent_id || '').trim() || null,
      agent_label: String(item?.agent_label || '').trim() || formatSpecialistAgentLabel(item?.agent_id),
      intent: String(item?.intent || '').trim() || null,
    }))
  }
  const specialist = payload?.specialist && typeof payload.specialist === 'object'
    ? payload.specialist
    : null
  if (specialist) {
    return [{
      label: activityText,
      detail: statusMessage && statusMessage !== activityText ? statusMessage : undefined,
      status: typeof payload?.status === 'string' ? payload.status.trim() : null,
      stage: progressStage,
      kind: 'specialist_flow',
      flow_role: 'specialist',
      parent_delegated_task_id: String(specialist.attach_to_task_id || '').trim() || null,
      specialist_task_id: String(specialist.task_id || '').trim() || null,
      agent_id: String(specialist.agent_id || '').trim() || null,
      agent_label: String(specialist.agent_label || '').trim() || formatSpecialistAgentLabel(specialist.agent_id),
      intent: String(specialist.intent || '').trim() || null,
      specialist_event_type: String(specialist.event_type || '').trim() || null,
    }]
  }
  return [{
    label: activityText,
    detail: statusMessage || undefined,
    status: typeof payload?.status === 'string' ? payload.status.trim() : null,
    stage: progressStage,
    kind: progressKind,
  }]
}

function mergeCachedActivityLogs(current: any[] | undefined, incoming: any[] | undefined) {
  let merged = Array.isArray(current) ? current : undefined
  for (const item of incoming || []) {
    if (!item || typeof item !== 'object') {
      continue
    }
    merged = appendCachedActivityLog(merged, item as any)
  }
  return merged
}

function normalizeGatewayBaseUrl(rawBaseUrl: string) {
  const trimmed = String(rawBaseUrl || '').trim()
  if (!trimmed) {
    throw new Error('Gateway URL is required.')
  }

  const hasScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed)
  const withScheme = hasScheme
    ? trimmed
    : /^(localhost|127(?:\.\d{1,3}){3}|\[::1\])/i.test(trimmed)
      ? `http://${trimmed}`
      : `https://${trimmed}`

  const url = new URL(withScheme)
  return url.toString().replace(/\/$/, '')
}

function buildGatewayWebSocketUrl(config: GatewayConnectionConfig) {
  const httpUrl = new URL(`${normalizeGatewayBaseUrl(config.baseUrl)}/`)
  const wsScheme = httpUrl.protocol === 'http:' ? 'ws:' : 'wss:'
  const wsUrl = new URL(httpUrl.toString())
  wsUrl.protocol = wsScheme
  wsUrl.pathname = '/ws'
  wsUrl.searchParams.set('token', config.apiToken)
  wsUrl.searchParams.set('device_id', config.deviceId)
  return wsUrl.toString()
}

function buildGatewayWebSocketHeaders(config: GatewayConnectionConfig) {
  return {
    Authorization: `Bearer ${config.apiToken}`,
    'X-Device-Id': config.deviceId,
  }
}

function toEventPayload(data: RawData) {
  if (typeof data === 'string') {
    return data
  }
  if (Buffer.isBuffer(data)) {
    return data.toString('utf-8')
  }
  if (Array.isArray(data)) {
    return Buffer.concat(data).toString('utf-8')
  }
  if (data instanceof ArrayBuffer) {
    return Buffer.from(data).toString('utf-8')
  }
  return ''
}

export class GatewayConnectionManager {
  private config: GatewayConnectionConfig | null = null
  private socket: WebSocket | null = null
  private reconnectTimer: NodeJS.Timeout | null = null
  private heartbeatTimer: NodeJS.Timeout | null = null
  private connectTimeoutTimer: NodeJS.Timeout | null = null
  private resumeTimer: NodeJS.Timeout | null = null
  private reconnectAttempt = 0
  private manuallyStopped = false
  private pendingResumeRequestId: string | null = null
  private resumeAttemptCount = 0
  private lastSocketActivityAt = 0
  private currentSessionId: string | null = null
  private historyTail: any[] = []
  private knownTaskIds = new Set<string>()
  private foregroundStreams = new Map<string, ForegroundStreamSnapshot>()
  private foregroundStreamRequestIndex = new Map<string, string>()
  private foregroundStreamTaskIndex = new Map<string, string>()
  private backgroundedRequestIds = new Set<string>()
  private status: GatewayConnectionStatus = {
    state: 'idle',
    connected: false,
    deviceId: null,
    sessionId: null,
  }

  constructor(private readonly window: BrowserWindow) { }

  configure(config: GatewayConnectionConfig | null) {
    const hadLiveSocket = Boolean(
      this.socket && (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING),
    )
    const normalized = config
      ? {
        ...config,
        baseUrl: normalizeGatewayBaseUrl(config.baseUrl),
        apiToken: String(config.apiToken || '').trim(),
        deviceId: String(config.deviceId || '').trim(),
      }
      : null

    const changed =
      (this.config?.baseUrl || '') !== (normalized?.baseUrl || '') ||
      (this.config?.apiToken || '') !== (normalized?.apiToken || '') ||
      (this.config?.deviceId || '') !== (normalized?.deviceId || '')

    if (!normalized) {
      this.config = null
      this.stop()
      return
    }

    if (changed && hadLiveSocket) {
      this.stop()
    }

    this.config = normalized
    this.status = {
      ...this.status,
      deviceId: normalized?.deviceId || null,
    }

    if (changed) {
      this.manuallyStopped = false
      this.connect()
    }
  }

  connect() {
    if (!this.config) {
      return
    }
    if (this.socket && (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING)) {
      return
    }

    this.clearReconnectTimer()
    this.clearConnectTimeout()
    this.clearResumeTimeout()
    this.manuallyStopped = false
    this.setStatus(this.reconnectAttempt > 0 ? 'reconnecting' : 'connecting', {
      attempt: this.reconnectAttempt || undefined,
      detail: this.reconnectAttempt > 0 ? 'Reconnecting to your VM.' : 'Connecting to your VM.',
    })

    const ws = new WebSocket(buildGatewayWebSocketUrl(this.config), {
      headers: buildGatewayWebSocketHeaders(this.config),
    })
    this.socket = ws
    this.startConnectTimeout(ws)

    ws.on('open', () => {
      if (this.socket !== ws) return
      this.clearConnectTimeout()
      this.reconnectAttempt = 0
      this.lastSocketActivityAt = Date.now()
      this.startHeartbeat()
      this.setStatus('connected', { detail: 'Connected to your VM.' })
      this.resumeAttemptCount = 0
      try {
        this.sendResume()
      } catch (error) {
        this.setStatus('reconnecting', {
          detail: error instanceof Error ? error.message : 'Failed to resume your VM session.',
        })
        ws.terminate()
      }
    })

    ws.on('message', (data: RawData) => {
      if (this.socket !== ws) return
      this.lastSocketActivityAt = Date.now()
      void this.handleMessage(data)
    })

    ws.on('error', (error) => {
      if (this.socket !== ws) return
      this.setStatus('error', {
        detail: error instanceof Error && error.message ? `Gateway socket error: ${error.message}` : 'Gateway socket error.',
      })
    })

    ws.on('close', () => {
      if (this.socket !== ws) return
      this.socket = null
      this.clearConnectTimeout()
      this.clearResumeTimeout()
      this.pendingResumeRequestId = null
      this.resumeAttemptCount = 0
      this.stopHeartbeat()
      if (this.manuallyStopped || !this.config) {
        this.setStatus('idle', { detail: 'Gateway socket stopped.' })
        return
      }

      this.reconnectAttempt += 1
      this.setStatus('reconnecting', {
        detail: 'Connection lost. Reconnecting to your VM.',
        attempt: this.reconnectAttempt,
      })
      this.scheduleReconnect()
    })
  }

  stop() {
    this.manuallyStopped = true
    this.clearReconnectTimer()
    this.clearConnectTimeout()
    this.clearResumeTimeout()
    this.stopHeartbeat()
    this.pendingResumeRequestId = null
    this.resumeAttemptCount = 0
    this.lastSocketActivityAt = 0
    this.currentSessionId = null
    this.historyTail = []
    this.knownTaskIds.clear()
    this.foregroundStreams.clear()
    this.foregroundStreamRequestIndex.clear()
    this.foregroundStreamTaskIndex.clear()
    this.backgroundedRequestIds.clear()
    if (this.socket) {
      const socket = this.socket
      this.socket = null
      if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
        socket.close()
      }
    }
    this.setStatus('idle', { detail: 'Gateway socket stopped.' })
  }

  requestResume() {
    this.resumeAttemptCount = 0
    if (this.socket?.readyState === WebSocket.OPEN) {
      try {
        this.sendResume()
      } catch {
        this.socket.terminate()
      }
      return
    }
    this.connect()
  }

  sendQuery(
    content: string,
    conversationContext: any[] = [],
    requestId?: string,
    routeOverride?: string,
    attachments: DesktopQueryAttachment[] = [],
  ) {
    const effectiveRequestId = String(requestId || '').trim() || `req_${crypto.randomUUID()}`
    const normalizedRouteOverride = String(routeOverride || '').trim()
    const normalizedAttachments = Array.isArray(attachments) ? attachments.filter((item) => item && typeof item === 'object') : []
    this.sendJson({
      type: 'query',
      request_id: effectiveRequestId,
      session_id: this.currentSessionId,
      content,
      conversation_context: Array.isArray(conversationContext) ? conversationContext : [],
      route_override: normalizedRouteOverride || undefined,
      attachments: normalizedAttachments.length > 0 ? normalizedAttachments : undefined,
    })
    this.recordDesktopQueryInHistory(content, effectiveRequestId, normalizedAttachments)
    return effectiveRequestId
  }

  submitTaskInputReply(inputRequestId: string, taskId: string, content: string) {
    const normalizedInputRequestId = String(inputRequestId || '').trim()
    const normalizedTaskId = String(taskId || '').trim()
    const normalizedContent = String(content || '').trim()
    if (!normalizedInputRequestId || !normalizedTaskId || !normalizedContent) {
      throw new Error('inputRequestId, taskId, and content are required to reply to a task.')
    }
    const requestId = `task_input_reply_${crypto.randomUUID()}`
    this.sendJson({
      type: 'task.input_reply',
      request_id: requestId,
      input_request_id: normalizedInputRequestId,
      task_id: normalizedTaskId,
      content: normalizedContent,
    })
    return { ok: true, requestId }
  }

  cancelResponse(requestId?: string, taskId?: string) {
    const normalizedRequestId = String(requestId || '').trim()
    const normalizedTaskId = String(taskId || '').trim()
    if (!normalizedRequestId && !normalizedTaskId) {
      throw new Error('A requestId or taskId is required to stop a response.')
    }
    this.sendJson({
      type: 'cancel',
      request_id: `cancel_${crypto.randomUUID()}`,
      target_request_id: normalizedRequestId || undefined,
      task_id: normalizedTaskId || undefined,
    })
    return { ok: true }
  }

  backgroundRequest(requestId: string) {
    const normalizedRequestId = String(requestId || '').trim()
    if (!normalizedRequestId) {
      throw new Error('A requestId is required to move a response to the background.')
    }
    this.sendJson({
      type: 'background',
      request_id: normalizedRequestId,
    })
    return { ok: true, requestId: normalizedRequestId }
  }

  foregroundRequest(requestId: string) {
    const normalizedRequestId = String(requestId || '').trim()
    if (!normalizedRequestId) {
      throw new Error('A requestId is required to bring a background response to the foreground.')
    }
    this.sendJson({
      type: 'foreground',
      request_id: normalizedRequestId,
    })
    return { ok: true, requestId: normalizedRequestId }
  }

  getState() {
    return {
      status: this.status,
      sessionId: this.currentSessionId,
      historyTail: this.historyTail,
      knownTaskIds: Array.from(this.knownTaskIds),
      foregroundStreams: this.getForegroundStreams(),
      config: this.config ? { ...this.config, apiToken: undefined } : null,
    }
  }

  private sendResume() {
    const requestId = `resume_${crypto.randomUUID()}`
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || undefined
    this.sendJson({
      type: 'resume',
      request_id: requestId,
      session_id: this.currentSessionId,
      known_task_ids: Array.from(this.knownTaskIds),
      timezone,
    })
    this.pendingResumeRequestId = requestId
    this.resumeAttemptCount += 1
    this.scheduleResumeTimeout(requestId)
  }

  private upsertHistoryMessage(message: any) {
    if (!message || typeof message !== 'object') {
      return
    }

    const role = String(message.role || '').trim()
    if (role !== 'user' && role !== 'assistant') {
      return
    }

    const content = String(message.content || '')
    if (!content.trim()) {
      return
    }

    const requestId =
      typeof message.request_id === 'string' && message.request_id.trim()
        ? message.request_id.trim()
        : typeof message?.metadata?.request_id === 'string' && message.metadata.request_id.trim()
          ? message.metadata.request_id.trim()
          : ''
    const messageId = typeof message.message_id === 'string' ? message.message_id.trim() : ''
    const nextMessage = {
      ...message,
      role,
      content,
      request_id: requestId || undefined,
      message_id: messageId || message.message_id,
      metadata: message?.metadata && typeof message.metadata === 'object' ? message.metadata : undefined,
    }

    let replaceIndex = -1
    if (messageId) {
      replaceIndex = this.historyTail.findIndex((item: any) => String(item?.message_id || '').trim() === messageId)
    }
    if (replaceIndex < 0 && requestId) {
      replaceIndex = this.historyTail.findIndex((item: any) => (
        String(item?.role || '').trim() === role &&
        (
          String(item?.request_id || '').trim() === requestId ||
          String(item?.metadata?.request_id || '').trim() === requestId
        )
      ))
    }

    if (replaceIndex >= 0) {
      this.historyTail = this.historyTail.map((item, index) => (index === replaceIndex ? nextMessage : item))
      return
    }

    this.historyTail = [...this.historyTail, nextMessage]
  }

  private recordDesktopQueryInHistory(content: string, requestId: string, attachments: DesktopQueryAttachment[] = []) {
    const normalizedAttachments = Array.isArray(attachments) ? attachments.filter((item) => item && typeof item === 'object') : []
    const normalizedContent = String(content || '') || (normalizedAttachments.length > 0 ? this.desktopAttachmentPlaceholder(normalizedAttachments) : '')
    const normalizedRequestId = String(requestId || '').trim()
    if (!normalizedContent.trim() || !normalizedRequestId) {
      return
    }

    this.upsertHistoryMessage({
      message_id: `pending_user_${normalizedRequestId}`,
      role: 'user',
      content: normalizedContent,
      request_id: normalizedRequestId,
      channel: this.config ? `desktop:${this.config.deviceId}` : null,
      created_at: new Date().toISOString(),
      metadata: {
        request_id: normalizedRequestId,
        pending: true,
        platform: 'desktop',
        message_type: 'query',
        attachments: normalizedAttachments.length > 0 ? normalizedAttachments : undefined,
      },
    })
  }

  private desktopAttachmentPlaceholder(attachments: DesktopQueryAttachment[]) {
    const count = attachments.length
    if (count <= 0) {
      return ''
    }
    const imageCount = attachments.filter((item) => {
      const mime = String(item?.mime || item?.mime_type || '').toLowerCase()
      return mime.startsWith('image/')
    }).length
    if (count === 1 && imageCount === 1) {
      return `[image]`
    }
    if (count === 1) {
      return `[attachment]`
    }
    if (imageCount === count) {
      return `[${count} images]`
    }
    return `[${count} attachments]`
  }

  private applyEventToHistory(payload: any, eventType: string) {
    if (!payload || typeof payload !== 'object') {
      return
    }

    if (eventType === 'resume.ok') {
      this.historyTail = Array.isArray(payload.history_tail) ? payload.history_tail : []
      return
    }

    if (eventType === 'response.complete') {
      const content = String(payload.content || '')
      const hasBlocks = Array.isArray(payload.response_blocks) && payload.response_blocks.length > 0
      if (!content.trim() && !hasBlocks) {
        return
      }
      const requestId = String(payload.request_id || '').trim()
      const metadata: Record<string, unknown> = {}
      if (typeof payload.thinking_text === 'string' && payload.thinking_text.trim()) {
        metadata.thinking_text = payload.thinking_text
      }
      if (Array.isArray(payload.sources) && payload.sources.length > 0) {
        metadata.sources = payload.sources
      }
      if (Array.isArray(payload.produced_artifacts) && payload.produced_artifacts.length > 0) {
        metadata.produced_artifacts = payload.produced_artifacts
      }
      if (hasBlocks) {
        metadata.response_blocks = payload.response_blocks
      }
      if (Array.isArray(payload.activity_log) && payload.activity_log.length > 0) {
        metadata.activity_log = payload.activity_log
      }
      if (payload.awaiting_reply === true) {
        metadata.awaiting_reply = true
      }
      this.upsertHistoryMessage({
        message_id: typeof payload.message_id === 'string' && payload.message_id.trim()
          ? payload.message_id.trim()
          : requestId
            ? `pending_assistant_${requestId}`
            : `pending_assistant_${crypto.randomUUID()}`,
        role: 'assistant',
        content,
        route: typeof payload.route === 'string' ? payload.route : undefined,
        request_id: requestId || undefined,
        channel: typeof payload.channel === 'string' ? payload.channel : null,
        created_at: new Date().toISOString(),
        metadata: Object.keys(metadata).length > 0 ? metadata : undefined,
      })
      return
    }

    if (eventType === 'crosschannel.message') {
      const role = String(payload.role || '').trim()
      const content = String(payload.content || '')
      const hasBlocks = Array.isArray(payload.response_blocks) && payload.response_blocks.length > 0
      if ((role !== 'user' && role !== 'assistant') || (!content.trim() && !hasBlocks)) {
        return
      }
      const lastItem = this.historyTail[this.historyTail.length - 1]
      if (
        lastItem &&
        String(lastItem.role || '').trim() === role &&
        String(lastItem.content || '') === content &&
        String(lastItem.channel || '') === String(payload.channel || '')
      ) {
        return
      }
      const metadata: Record<string, unknown> = {}
      if (Array.isArray(payload.sources) && payload.sources.length > 0) {
        metadata.sources = payload.sources
      }
      if (typeof payload.thinking_text === 'string' && payload.thinking_text.trim()) {
        metadata.thinking_text = payload.thinking_text
      }
      if (Array.isArray(payload.attachments) && payload.attachments.length > 0) {
        metadata.attachments = payload.attachments
      }
      if (Array.isArray(payload.input_artifacts) && payload.input_artifacts.length > 0) {
        metadata.input_artifacts = payload.input_artifacts
      }
      if (Array.isArray(payload.produced_artifacts) && payload.produced_artifacts.length > 0) {
        metadata.produced_artifacts = payload.produced_artifacts
      }
      if (Array.isArray(payload.response_blocks) && payload.response_blocks.length > 0) {
        metadata.response_blocks = payload.response_blocks
      }
      if (Array.isArray(payload.activity_log) && payload.activity_log.length > 0) {
        metadata.activity_log = payload.activity_log
      }
      this.historyTail = [
        ...this.historyTail,
        {
          message_id: typeof payload.message_id === 'string' && payload.message_id.trim()
            ? payload.message_id.trim()
            : `crosschannel_${crypto.randomUUID()}`,
          role,
          content,
          channel: typeof payload.channel === 'string' ? payload.channel : null,
          created_at: new Date().toISOString(),
          metadata: Object.keys(metadata).length > 0 ? metadata : undefined,
        },
      ]
    }
  }

  private resolveForegroundStreamKey(payload: any) {
    const requestId = typeof payload?.request_id === 'string' ? payload.request_id.trim() : ''
    const taskId = typeof payload?.task_id === 'string' ? payload.task_id.trim() : ''
    if (requestId) {
      const existing = this.foregroundStreamRequestIndex.get(requestId)
      if (existing) {
        return existing
      }
    }
    if (taskId) {
      const existing = this.foregroundStreamTaskIndex.get(taskId)
      if (existing) {
        return existing
      }
    }
    if (requestId) {
      return `request:${requestId}`
    }
    if (taskId) {
      return `task:${taskId}`
    }
    return null
  }

  private upsertForegroundStream(
    payload: any,
    patch: Partial<Omit<ForegroundStreamSnapshot, 'stream_key' | 'updated_at'>> = {},
  ) {
    const key = this.resolveForegroundStreamKey(payload)
    if (!key) {
      return null
    }
    const existing = this.foregroundStreams.get(key)
    const requestId =
      typeof patch.request_id === 'string'
        ? patch.request_id.trim()
        : typeof payload?.request_id === 'string'
          ? payload.request_id.trim()
          : typeof existing?.request_id === 'string'
            ? existing.request_id.trim()
            : ''
    const taskId =
      typeof patch.task_id === 'string'
        ? patch.task_id.trim()
        : typeof payload?.task_id === 'string'
          ? payload.task_id.trim()
          : typeof existing?.task_id === 'string'
            ? existing.task_id.trim()
            : ''

    const next: ForegroundStreamSnapshot = {
      ...(existing || {}),
      ...patch,
      stream_key: key,
      request_id: requestId || null,
      task_id: taskId || null,
      updated_at: new Date().toISOString(),
    }

    this.foregroundStreams.set(key, next)
    if (requestId) {
      this.foregroundStreamRequestIndex.set(requestId, key)
    }
    if (taskId) {
      this.foregroundStreamTaskIndex.set(taskId, key)
    }
    return next
  }

  private getForegroundStreamSnapshot(payload: any) {
    const key = this.resolveForegroundStreamKey(payload)
    if (!key) {
      return null
    }
    return this.foregroundStreams.get(key) || null
  }

  private removeForegroundStream(payload: any) {
    const key = this.resolveForegroundStreamKey(payload)
    if (!key) {
      return
    }
    const existing = this.foregroundStreams.get(key)
    if (!existing) {
      return
    }
    const requestId = typeof existing.request_id === 'string' ? existing.request_id.trim() : ''
    const taskId = typeof existing.task_id === 'string' ? existing.task_id.trim() : ''
    if (requestId) {
      this.foregroundStreamRequestIndex.delete(requestId)
    }
    if (taskId) {
      this.foregroundStreamTaskIndex.delete(taskId)
    }
    this.foregroundStreams.delete(key)
  }

  private pruneForegroundStreams() {
    const now = Date.now()
    for (const stream of Array.from(this.foregroundStreams.values())) {
      const updatedAt = Date.parse(String(stream.updated_at || ''))
      if (Number.isFinite(updatedAt) && now - updatedAt > FOREGROUND_STREAM_TTL_MS) {
        this.removeForegroundStream({ request_id: stream.request_id, task_id: stream.task_id })
      }
    }
  }

  private pruneForegroundStreamsFromHistory() {
    const assistantRequestIds = new Set<string>()
    for (const item of this.historyTail) {
      if (!item || typeof item !== 'object' || String(item?.role || '').trim() !== 'assistant') {
        continue
      }
      const requestId =
        typeof item?.request_id === 'string' && item.request_id.trim()
          ? item.request_id.trim()
          : typeof item?.metadata?.request_id === 'string' && item.metadata.request_id.trim()
            ? item.metadata.request_id.trim()
            : ''
      if (requestId) {
        assistantRequestIds.add(requestId)
      }
    }

    for (const requestId of assistantRequestIds) {
      const key = this.foregroundStreamRequestIndex.get(requestId)
      if (!key) {
        continue
      }
      const stream = this.foregroundStreams.get(key)
      if (!stream) {
        continue
      }
      const taskId = typeof stream.task_id === 'string' ? stream.task_id.trim() : ''
      if (stream.completed || stream.failed || !taskId || !this.knownTaskIds.has(taskId)) {
        this.removeForegroundStream({ request_id: requestId, task_id: taskId })
      }
    }
  }

  private getForegroundStreams() {
    this.pruneForegroundStreams()
    return Array.from(this.foregroundStreams.values())
      .filter((stream) => {
        const requestId = typeof stream.request_id === 'string' ? stream.request_id.trim() : ''
        return !requestId || !this.backgroundedRequestIds.has(requestId)
      })
      .sort((left, right) => Date.parse(String(left.updated_at || '')) - Date.parse(String(right.updated_at || '')))
  }

  private captureForegroundStreamEvent(payload: any, eventType: string) {
    if (!payload || typeof payload !== 'object') {
      return
    }
    const requestId = typeof payload.request_id === 'string' ? payload.request_id.trim() : ''
    if (eventType === 'task.backgrounded') {
      if (requestId) {
        this.backgroundedRequestIds.add(requestId)
      }
      this.removeForegroundStream(payload)
      return
    }
    if (eventType.startsWith('task.background.')) {
      return
    }
    if (requestId && this.backgroundedRequestIds.has(requestId) && eventType !== 'task.foregrounded') {
      return
    }

    if (eventType === 'task.foregrounded' && requestId) {
      this.backgroundedRequestIds.delete(requestId)
      this.upsertForegroundStream(payload, {
        session_id: typeof payload.session_id === 'string' ? payload.session_id : undefined,
        route: typeof payload.route === 'string' ? payload.route : undefined,
        content: typeof payload.partial_content === 'string' ? payload.partial_content : undefined,
        thinking_text: typeof payload.partial_thinking === 'string' ? payload.partial_thinking : undefined,
        activity: typeof payload.activity === 'string' ? payload.activity : undefined,
        activity_log: Array.isArray(payload.activity_log) ? payload.activity_log : undefined,
        docs_progress: payload.docs_progress,
        tabular_progress: payload.tabular_progress,
        produced_artifacts: Array.isArray(payload.produced_artifacts) ? payload.produced_artifacts : undefined,
        sources: Array.isArray(payload.sources) ? payload.sources : undefined,
        completed: Boolean(payload.completed),
        failed: Boolean(payload.failed),
        error: typeof payload.error === 'string' ? payload.error : undefined,
      })
      return
    }

    if (eventType === 'route_result' || eventType === 'task.created') {
      this.upsertForegroundStream(payload, {
        session_id: typeof payload.session_id === 'string' ? payload.session_id : undefined,
        route: typeof payload.route === 'string' ? payload.route : undefined,
      })
      return
    }

    if (eventType === 'task.progress') {
      const eventStatus = typeof payload.status === 'string' ? payload.status.trim() : ''
      const statusMessage = typeof payload.message === 'string' ? payload.message.trim() : ''
      const progressState = payload.tabular_progress ?? payload.docs_progress
      const progressLabel =
        progressState && typeof progressState === 'object' && typeof progressState.label === 'string'
          ? progressState.label.trim()
          : ''
      const progressStage =
        progressState && typeof progressState === 'object' && typeof progressState.stage === 'string'
          ? progressState.stage.trim()
          : null
      const progressKind =
        progressState && typeof progressState === 'object' && typeof (progressState as any).kind === 'string'
          ? String((progressState as any).kind).trim()
          : payload.tabular_progress
            ? 'tabular_parse'
            : payload.docs_progress
            ? 'docs_parse'
              : 'generic'
      const activityText = progressLabel || statusMessage || (eventStatus ? `Task ${eventStatus}...` : 'Working on your request...')
      const activityEntries = buildCachedProgressEntries(
        payload,
        activityText,
        statusMessage,
        progressStage,
        progressKind,
      )
      const existing = this.upsertForegroundStream(payload, {
        session_id: typeof payload.session_id === 'string' ? payload.session_id : undefined,
        route: typeof payload.route === 'string' ? payload.route : undefined,
      })
      this.upsertForegroundStream(payload, {
        session_id: typeof payload.session_id === 'string' ? payload.session_id : undefined,
        route: typeof payload.route === 'string' ? payload.route : undefined,
        activity: activityText,
        activity_log: mergeCachedActivityLogs(
          Array.isArray(payload.activity_log) ? payload.activity_log : existing?.activity_log,
          activityEntries,
        ),
        docs_progress: payload.docs_progress,
        tabular_progress: payload.tabular_progress,
        completed: false,
        failed: false,
      })
      return
    }

    if (eventType === 'response.chunk') {
      const existing = this.upsertForegroundStream(payload, {
        session_id: typeof payload.session_id === 'string' ? payload.session_id : undefined,
        route: typeof payload.route === 'string' ? payload.route : undefined,
        completed: false,
        failed: false,
      })
      if (existing) {
        this.upsertForegroundStream(payload, {
          content: appendCachedStreamText(existing.content, payload.content),
        })
      }
      return
    }

    if (eventType === 'response.thinking.chunk') {
      const existing = this.upsertForegroundStream(payload, {
        session_id: typeof payload.session_id === 'string' ? payload.session_id : undefined,
        route: typeof payload.route === 'string' ? payload.route : undefined,
        completed: false,
        failed: false,
      })
      if (existing) {
        this.upsertForegroundStream(payload, {
          thinking_text: appendCachedStreamText(existing.thinking_text, payload.content),
        })
      }
      return
    }

    if (eventType === 'response.complete') {
      const existing = this.upsertForegroundStream(payload, {
        session_id: typeof payload.session_id === 'string' ? payload.session_id : undefined,
        route: typeof payload.route === 'string' ? payload.route : undefined,
      })
      this.upsertForegroundStream(payload, {
        session_id: typeof payload.session_id === 'string' ? payload.session_id : undefined,
        route: typeof payload.route === 'string' ? payload.route : undefined,
        message_id: typeof payload.message_id === 'string' ? payload.message_id : undefined,
        activity_log: mergeCachedActivityLogs(
          existing?.activity_log,
          Array.isArray(payload.activity_log) ? payload.activity_log : undefined,
        ),
        produced_artifacts: Array.isArray(payload.produced_artifacts) ? payload.produced_artifacts : undefined,
        response_blocks: Array.isArray(payload.response_blocks) ? payload.response_blocks : undefined,
        sources: Array.isArray(payload.sources) ? payload.sources : undefined,
        channel: typeof payload.channel === 'string' ? payload.channel : undefined,
        source: typeof payload.source === 'string' ? payload.source : undefined,
        source_id: typeof payload.source_id === 'string' ? payload.source_id : undefined,
        awaiting_reply: payload.awaiting_reply === true,
        completed: true,
        failed: false,
      })
      if (existing) {
        this.upsertForegroundStream(payload, {
          content: mergeCachedCompletedText(existing.content, payload.content),
        })
      }
      return
    }

    if (eventType === 'task.failed') {
      const errorMessage =
        typeof payload?.error?.message === 'string' && payload.error.message.trim()
          ? payload.error.message.trim()
          : typeof payload.message === 'string' && payload.message.trim()
            ? payload.message.trim()
            : 'Opus task failed.'
      this.upsertForegroundStream(payload, {
        session_id: typeof payload.session_id === 'string' ? payload.session_id : undefined,
        route: typeof payload.route === 'string' ? payload.route : undefined,
        content: errorMessage,
        completed: true,
        failed: true,
        error: errorMessage,
      })
      return
    }

    if (eventType === 'task.cancelled') {
      this.upsertForegroundStream(payload, {
        session_id: typeof payload.session_id === 'string' ? payload.session_id : undefined,
        route: typeof payload.route === 'string' ? payload.route : undefined,
        completed: true,
      })
      return
    }

    if (eventType === 'task.completed') {
      this.upsertForegroundStream(payload, {
        session_id: typeof payload.session_id === 'string' ? payload.session_id : undefined,
        route: typeof payload.route === 'string' ? payload.route : undefined,
        completed: true,
        failed: false,
      })
    }
  }

  private sendPing() {
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || undefined
    this.sendJson({
      type: 'ping',
      ts_unix_ms: Date.now(),
      timezone,
    })
  }

  private sendJson(payload: Record<string, unknown>) {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      throw new Error('Gateway socket is not connected.')
    }
    this.socket.send(JSON.stringify(payload))
  }

  private async handleMessage(raw: RawData) {
    const payloadText = toEventPayload(raw)
    if (!payloadText) {
      return
    }

    let payload: any
    try {
      payload = JSON.parse(payloadText)
    } catch {
      return
    }
    if (!payload || typeof payload !== 'object') {
      return
    }

    const eventType = String(payload.type || '').trim()
    const nextSessionId = typeof payload.session_id === 'string' ? String(payload.session_id) : null
    if (nextSessionId) {
      if (this.currentSessionId && nextSessionId !== this.currentSessionId) {
        const requestId = String(payload.request_id || '').trim()
        this.historyTail = requestId
          ? this.historyTail.filter((item: any) => (
            String(item?.request_id || item?.metadata?.request_id || '').trim() === requestId
          ))
          : []
      }
      this.currentSessionId = nextSessionId
    }
    if (eventType === 'pong') {
      this.lastSocketActivityAt = Date.now()
    }
    if (eventType === 'resume.ok') {
      const responseRequestId = String(payload.request_id || '').trim()
      if (this.pendingResumeRequestId && responseRequestId && responseRequestId !== this.pendingResumeRequestId) {
        return
      }
      this.clearResumeTimeout()
      this.pendingResumeRequestId = null
      this.resumeAttemptCount = 0
      this.currentSessionId = typeof payload.session_id === 'string' ? payload.session_id : this.currentSessionId
      this.knownTaskIds = new Set(
        Array.isArray(payload.active_tasks)
          ? payload.active_tasks
            .map((item: any) => String(item?.task_id || '').trim())
            .filter(Boolean)
          : [],
      )
      this.setStatus('connected', { detail: 'Connected to your VM.' })
    }
    if (eventType === 'task.created' && payload.task_id) {
      this.knownTaskIds.add(String(payload.task_id))
    }
    if ((eventType === 'task.completed' || eventType === 'task.failed' || eventType === 'task.cancelled') && payload.task_id) {
      this.knownTaskIds.delete(String(payload.task_id))
    }

    this.captureForegroundStreamEvent(payload, eventType)
    const foregroundSnapshot = this.getForegroundStreamSnapshot(payload)
    if (foregroundSnapshot) {
      payload = {
        ...payload,
        activity_log:
          Array.isArray(foregroundSnapshot.activity_log) && foregroundSnapshot.activity_log.length > 0
            ? foregroundSnapshot.activity_log
            : payload.activity_log,
      }
    }
    this.applyEventToHistory(payload, eventType)
    this.pruneForegroundStreamsFromHistory()
    if (eventType === 'response.complete') {
      this.removeForegroundStream(payload)
    }
    if (eventType === 'resume.ok') {
      payload = {
        ...payload,
        foreground_streams: this.getForegroundStreams(),
      }
    }

    this.emitToRenderer('gateway:event', payload)
  }

  private scheduleReconnect() {
    this.clearReconnectTimer()
    const attempt = Math.max(1, this.reconnectAttempt)
    const jitterMs = Math.floor(Math.random() * 350)
    const delayMs = Math.min(30000, 1000 * (2 ** Math.min(attempt - 1, 5))) + jitterMs
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this.connect()
    }, delayMs)
  }

  private clearReconnectTimer() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
  }

  private startConnectTimeout(ws: WebSocket) {
    this.clearConnectTimeout()
    this.connectTimeoutTimer = setTimeout(() => {
      if (this.socket !== ws || ws.readyState !== WebSocket.CONNECTING) {
        return
      }
      this.setStatus('reconnecting', { detail: 'Gateway connection timed out. Retrying your VM connection.' })
      ws.terminate()
    }, CONNECT_TIMEOUT_MS)
  }

  private clearConnectTimeout() {
    if (this.connectTimeoutTimer) {
      clearTimeout(this.connectTimeoutTimer)
      this.connectTimeoutTimer = null
    }
  }

  private scheduleResumeTimeout(requestId: string) {
    this.clearResumeTimeout()
    this.resumeTimer = setTimeout(() => {
      if (
        !this.socket ||
        this.socket.readyState !== WebSocket.OPEN ||
        this.pendingResumeRequestId !== requestId
      ) {
        return
      }

      if (this.resumeAttemptCount < MAX_RESUME_ATTEMPTS) {
        this.setStatus('connected', { detail: 'Re-syncing your VM session.' })
        try {
          this.sendResume()
          return
        } catch (error) {
          this.setStatus('reconnecting', {
            detail: error instanceof Error ? error.message : 'Failed to resume your VM session.',
          })
        }
      }

      this.setStatus('reconnecting', { detail: 'Session resume stalled. Reconnecting to your VM.' })
      this.socket.terminate()
    }, RESUME_TIMEOUT_MS)
  }

  private clearResumeTimeout() {
    if (this.resumeTimer) {
      clearTimeout(this.resumeTimer)
      this.resumeTimer = null
    }
  }

  private startHeartbeat() {
    this.stopHeartbeat()
    this.lastSocketActivityAt = Date.now()
    this.heartbeatTimer = setInterval(() => {
      if (this.socket?.readyState === WebSocket.OPEN) {
        if (Date.now() - this.lastSocketActivityAt > HEARTBEAT_STALE_MS) {
          this.setStatus('reconnecting', { detail: 'Connection stalled. Reconnecting to your VM.' })
          this.socket.terminate()
          return
        }
        try {
          this.sendPing()
        } catch {
          return
        }
      }
    }, HEARTBEAT_INTERVAL_MS)
  }

  private stopHeartbeat() {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer)
      this.heartbeatTimer = null
    }
  }

  private setStatus(
    state: GatewayConnectionStatus['state'],
    overrides: Partial<GatewayConnectionStatus> = {},
  ) {
    this.status = {
      state,
      connected: state === 'connected',
      deviceId: this.config?.deviceId || null,
      sessionId: this.currentSessionId,
      ...overrides,
    }
    this.emitToRenderer('gateway:status', this.status)
  }

  private emitToRenderer(channel: string, payload: unknown) {
    if (this.window.isDestroyed()) {
      return
    }
    this.window.webContents.send(channel, payload)
  }
}

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

const CONNECT_TIMEOUT_MS = 15000
const HEARTBEAT_INTERVAL_MS = 25000
const HEARTBEAT_STALE_MS = 70000
const RESUME_TIMEOUT_MS = 10000
const MAX_RESUME_ATTEMPTS = 2

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

  sendQuery(content: string, conversationContext: any[] = [], requestId?: string, routeOverride?: string) {
    const effectiveRequestId = String(requestId || '').trim() || `req_${crypto.randomUUID()}`
    const normalizedRouteOverride = String(routeOverride || '').trim()
    this.sendJson({
      type: 'query',
      request_id: effectiveRequestId,
      session_id: this.currentSessionId,
      content,
      conversation_context: Array.isArray(conversationContext) ? conversationContext : [],
      route_override: normalizedRouteOverride || undefined,
    })
    return effectiveRequestId
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

  getState() {
    return {
      status: this.status,
      sessionId: this.currentSessionId,
      historyTail: this.historyTail,
      knownTaskIds: Array.from(this.knownTaskIds),
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
    if (payload.session_id) {
      this.currentSessionId = String(payload.session_id)
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
      this.historyTail = Array.isArray(payload.history_tail) ? payload.history_tail : []
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

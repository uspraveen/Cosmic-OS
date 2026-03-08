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
  private reconnectAttempt = 0
  private manuallyStopped = false
  private resumeRequested = true
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
      this.resumeRequested = true
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
    this.manuallyStopped = false
    this.setStatus(this.reconnectAttempt > 0 ? 'reconnecting' : 'connecting', {
      attempt: this.reconnectAttempt || undefined,
      detail: this.reconnectAttempt > 0 ? 'Reconnecting to your VM.' : 'Connecting to your VM.',
    })

    const ws = new WebSocket(buildGatewayWebSocketUrl(this.config))
    this.socket = ws

    ws.on('open', () => {
      if (this.socket !== ws) return
      this.reconnectAttempt = 0
      this.startHeartbeat()
      this.setStatus('connected', { detail: 'Connected to your VM.' })
      if (this.resumeRequested) {
        this.sendResume()
      }
    })

    ws.on('message', (data: RawData) => {
      void this.handleMessage(data)
    })

    ws.on('error', () => {
      this.setStatus('error', { detail: 'Gateway socket error.' })
    })

    ws.on('close', () => {
      if (this.socket === ws) {
        this.socket = null
      }
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
    this.stopHeartbeat()
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
    this.resumeRequested = true
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.sendResume()
      return
    }
    this.connect()
  }

  sendQuery(content: string, conversationContext: any[] = [], requestId?: string) {
    const effectiveRequestId = String(requestId || '').trim() || `req_${crypto.randomUUID()}`
    this.sendJson({
      type: 'query',
      request_id: effectiveRequestId,
      session_id: this.currentSessionId,
      content,
      conversation_context: Array.isArray(conversationContext) ? conversationContext : [],
    })
    return effectiveRequestId
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
    this.sendJson({
      type: 'resume',
      request_id: requestId,
      session_id: this.currentSessionId,
      known_task_ids: Array.from(this.knownTaskIds),
    })
  }

  private sendPing() {
    this.sendJson({
      type: 'ping',
      ts_unix_ms: Date.now(),
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
    if (eventType === 'resume.ok') {
      this.resumeRequested = false
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

  private startHeartbeat() {
    this.stopHeartbeat()
    this.heartbeatTimer = setInterval(() => {
      if (this.socket?.readyState === WebSocket.OPEN) {
        try {
          this.sendPing()
        } catch {
          return
        }
      }
    }, 25000)
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

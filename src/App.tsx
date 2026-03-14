import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'
import LiquidGlass from './LiquidGlass'
import DynamicIsland from './DynamicIsland'
import CosmicLoginModal from './CosmicLoginModal'
import LiquidGlassLoader from './LiquidGlassLoader'
import MeetingMode from './MeetingMode'
import cosmicGlassyThunderLogo from './assets/cosmic-glassy-thunder-logo.png'
import './spotlight.css'

export type SearchPosition = 'bottom' | 'middle'
export type QueryMode = 'chat' | 'task' | 'meeting'
export type GatewayModelSelection = 'cosmic' | 'haiku' | 'opus' | 'perplexity'
type LauncherTileId = 'chat' | 'meeting' | 'task' | 'spaces'

interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  thinking?: string
  sources?: Array<{ url: string; title?: string; domain?: string } | string>
  stopped?: boolean
  channel?: string | null
}

interface PendingTaskInput {
  inputRequestId: string
  taskId: string
  sessionId?: string | null
  agent?: string | null
  channel?: string | null
  question: string
  options: string[]
  status?: string
  timestamp?: string | null
}

interface GatewayStatus {
  state: 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'error'
  connected: boolean
  detail?: string
  sessionId?: string | null
}

interface SurfaceLaunchState {
  target: 'chat' | 'meeting'
  token: number
  composerOffsetX: number
  composerOffsetY: number
  responseOffsetX: number
  responseOffsetY: number
  meetingOffsetX: number
  meetingOffsetY: number
}

type HoverTooltipTone = 'launcher' | 'model' | 'control'

interface HoverTooltipState {
  label: string
  x: number
  y: number
  tone: HoverTooltipTone
}

// Helper to strip "PROMPT:" from legacy database entries
const cleanText = (text: string) => {
  if (!text) return ""
  return text.replace(/^PROMPT:/, '')
}

const historyToMessages = (history: any[] = []): Message[] => {
  return history
    .filter((item) => item && (item.role === 'user' || item.role === 'assistant'))
    .map((item, index) => ({
      id: String(item.message_id || `${item.role}-${index}-${crypto.randomUUID()}`),
      role: item.role,
      content: String(item.content || ''),
      thinking: typeof item?.metadata?.thinking_text === 'string' ? item.metadata.thinking_text : undefined,
      sources: Array.isArray(item?.metadata?.sources) ? item.metadata.sources : undefined,
      stopped: Boolean(item?.metadata?.interrupted),
      channel: typeof item?.channel === 'string' ? item.channel : null,
    }))
}

/** Extract a human-readable channel label from the raw channel string. */
const channelLabel = (ch: string | null | undefined): string | null => {
  if (!ch) return null
  const lower = ch.toLowerCase()
  if (lower.startsWith('whatsapp:')) return 'WhatsApp'
  if (lower.startsWith('telegram:')) return 'Telegram'
  return null
}

/** Check if a message originated from a non-desktop channel. */
const isExternalChannel = (msg: Message): boolean => {
  return channelLabel(msg.channel) !== null
}

const buildConversationContext = (messages: Message[]) => {
  return messages.slice(-10).map((message) => ({
    role: message.role,
    content: message.content,
  }))
}

const normalizeGatewayModelSelection = (value: unknown): GatewayModelSelection => {
  const normalized = String(value || '').trim().toLowerCase()
  if (normalized === 'haiku' || normalized === 'opus' || normalized === 'perplexity') {
    return normalized
  }
  return 'cosmic'
}

const MODEL_OPTIONS: Array<{ id: GatewayModelSelection; label: string; shortLabel: string }> = [
  { id: 'cosmic', label: 'Cosmic', shortLabel: 'COSMIC' },
  { id: 'haiku', label: 'Haiku', shortLabel: 'HAIKU' },
  { id: 'opus', label: 'Opus', shortLabel: 'OPUS' },
  { id: 'perplexity', label: 'Perplexity', shortLabel: 'PPLX' },
]

const LAUNCHPAD_TILES: Array<{ id: LauncherTileId; label: string; locked: boolean }> = [
  { id: 'chat', label: 'Chat', locked: false },
  { id: 'meeting', label: 'Meeting', locked: false },
  { id: 'task', label: 'Task', locked: false },
  { id: 'spaces', label: 'Spaces', locked: true },
]

const normalizePendingTaskInput = (value: any): PendingTaskInput | null => {
  const inputRequestId = String(value?.input_request_id || '').trim()
  const taskId = String(value?.task_id || '').trim()
  const question = String(value?.question || '').trim()
  if (!inputRequestId || !taskId || !question) {
    return null
  }
  return {
    inputRequestId,
    taskId,
    sessionId: typeof value?.session_id === 'string' ? value.session_id : null,
    agent: typeof value?.agent === 'string' ? value.agent : null,
    channel: typeof value?.channel === 'string' ? value.channel : null,
    question,
    options: Array.isArray(value?.options) ? value.options.map((item: any) => String(item || '').trim()).filter(Boolean) : [],
    status: typeof value?.status === 'string' ? value.status : undefined,
    timestamp: typeof value?.timestamp === 'string' ? value.timestamp : null,
  }
}

const sortPendingTaskInputs = (items: PendingTaskInput[]) => {
  return [...items].sort((left, right) => {
    const leftTs = Date.parse(left.timestamp || '')
    const rightTs = Date.parse(right.timestamp || '')
    if (Number.isFinite(leftTs) && Number.isFinite(rightTs) && leftTs !== rightTs) {
      return leftTs - rightTs
    }
    return left.inputRequestId.localeCompare(right.inputRequestId)
  })
}

const mergePendingTaskInputs = (existing: PendingTaskInput[], incoming: PendingTaskInput[]) => {
  const byId = new Map<string, PendingTaskInput>()
  for (const item of existing) {
    byId.set(item.inputRequestId, item)
  }
  for (const item of incoming) {
    byId.set(item.inputRequestId, item)
  }
  return sortPendingTaskInputs(Array.from(byId.values()))
}

export default function App() {
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const modelDialRef = useRef<HTMLDivElement>(null)
  const composerSurfaceRef = useRef<HTMLDivElement>(null)
  const chatResponseSurfaceRef = useRef<HTMLDivElement>(null)
  const taskInterruptStackRef = useRef<HTMLDivElement>(null)
  const responseEndRef = useRef<HTMLDivElement>(null)
  const responseContainerRef = useRef<HTMLDivElement>(null)
  const modelDialSettleTimeoutRef = useRef<number | null>(null)
  const modelDialPulseTimeoutRef = useRef<number | null>(null)
  const modelDialWheelLockUntilRef = useRef(0)
  const surfaceLaunchResetTimeoutRef = useRef<number | null>(null)
  const meetingSurfaceRef = useRef<HTMLDivElement>(null)
  const activeAssistantMessageByRequestRef = useRef<Map<string, string>>(new Map())
  const activeAssistantMessageByTaskRef = useRef<Map<string, string>>(new Map())
  const streamedResponseRequestIdsRef = useRef<Set<string>>(new Set())
  const streamedResponseTaskIdsRef = useRef<Set<string>>(new Set())
  const activeStreamingRequestIdRef = useRef<string | null>(null)
  const activeStreamingTaskIdRef = useRef<string | null>(null)
  const messagesRef = useRef<Message[]>([])
  const activeSessionIdRef = useRef<string | null>(null)
  const shouldAutoScrollRef = useRef(true)
  const selectedModelRef = useRef<GatewayModelSelection>('cosmic')

  const [query, setQuery] = useState('')
  const [searchState, setSearchState] = useState<'hidden' | 'visible' | 'hiding'>('hidden')
  const [isIslandHovered, setIsIslandHovered] = useState(false)
  const [searchPosition, setSearchPosition] = useState<SearchPosition>('bottom')
  const [staybackTime, setStaybackTime] = useState(0)
  const [islandOpacity, setIslandOpacity] = useState(0.85) // Default opacity

  const [mode, setMode] = useState<QueryMode>('chat')
  const modeRef = useRef<QueryMode>('chat')
  const [isInputFocused, setIsInputFocused] = useState(false)
  const [showLauncherTray, setShowLauncherTray] = useState(false)
  const [selectedModel, setSelectedModel] = useState<GatewayModelSelection>('cosmic')
  const [modelPulseModel, setModelPulseModel] = useState<GatewayModelSelection | null>(null)
  const [hoverTooltip, setHoverTooltip] = useState<HoverTooltipState | null>(null)
  const [surfaceLaunch, setSurfaceLaunch] = useState<SurfaceLaunchState | null>(null)
  const [viewportSize, setViewportSize] = useState(() => ({
    width: typeof window !== 'undefined' ? window.innerWidth : 1440,
    height: typeof window !== 'undefined' ? window.innerHeight : 900,
  }))

  // --- CHAT STATE ---
  const [messages, setMessages] = useState<Message[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [showScrollButton, setShowScrollButton] = useState(false)
  const [expandedCrossChannelIds, setExpandedCrossChannelIds] = useState<Set<string>>(new Set())

  // --- AUTH STATE ---
  const [authState, setAuthState] = useState<'loading' | 'unauthenticated' | 'authenticated'>('loading')
  const [authData, setAuthData] = useState<any>(null)
  const [gatewayStatus, setGatewayStatus] = useState<GatewayStatus>({ state: 'idle', connected: false })

  // --- HISTORY / DB STATE ---
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)
  const [pendingTaskInputs, setPendingTaskInputs] = useState<PendingTaskInput[]>([])
  const [taskInputDrafts, setTaskInputDrafts] = useState<Record<string, string>>({})
  const [submittingTaskInputs, setSubmittingTaskInputs] = useState<Record<string, boolean>>({})
  const [taskInputErrors, setTaskInputErrors] = useState<Record<string, string>>({})
  const [dismissedTaskInterruptIds, setDismissedTaskInterruptIds] = useState<string[]>([])
  const [selectedTaskInputId, setSelectedTaskInputId] = useState<string | null>(null)
  const [taskInterruptIndex, setTaskInterruptIndex] = useState(0)
  const pendingTaskCount = pendingTaskInputs.length
  const orderedPendingTaskInputs = useMemo(() => [...pendingTaskInputs].reverse(), [pendingTaskInputs])
  const visibleTaskInterrupts = useMemo(
    () => orderedPendingTaskInputs.filter((item) => !dismissedTaskInterruptIds.includes(item.inputRequestId)),
    [dismissedTaskInterruptIds, orderedPendingTaskInputs],
  )
  const selectedTaskInput = useMemo(() => {
    if (orderedPendingTaskInputs.length === 0) {
      return null
    }
    if (selectedTaskInputId) {
      const matched = orderedPendingTaskInputs.find((item) => item.inputRequestId === selectedTaskInputId)
      if (matched) {
        return matched
      }
    }
    return orderedPendingTaskInputs[0]
  }, [orderedPendingTaskInputs, selectedTaskInputId])

  // Track key status for SetupModal
  const [keyStatus, setKeyStatus] = useState({
    haiku: false,
    perplexity: false,
    deepgram: false,
    groq: false,
    anthropic: false,
  })

  const resetInFlightAssistantMaps = () => {
    activeAssistantMessageByRequestRef.current.clear()
    activeAssistantMessageByTaskRef.current.clear()
    streamedResponseRequestIdsRef.current.clear()
    streamedResponseTaskIdsRef.current.clear()
    activeStreamingRequestIdRef.current = null
    activeStreamingTaskIdRef.current = null
  }

  const createAssistantMessageId = () => `assistant_${crypto.randomUUID()}`

  const createAssistantMessage = (overrides: Partial<Message> = {}): Message => ({
    id: overrides.id || createAssistantMessageId(),
    role: 'assistant',
    content: '',
    thinking: '',
    ...overrides,
  })

  const createUserMessage = (content: string): Message => ({
    id: `user_${crypto.randomUUID()}`,
    role: 'user',
    content,
  })

  const triggerModelDialPulse = (model: GatewayModelSelection) => {
    if (modelDialPulseTimeoutRef.current !== null) {
      window.clearTimeout(modelDialPulseTimeoutRef.current)
    }
    setModelPulseModel(model)
    modelDialPulseTimeoutRef.current = window.setTimeout(() => {
      setModelPulseModel((current) => (current === model ? null : current))
      modelDialPulseTimeoutRef.current = null
    }, 260)
  }

  const commitSelectedModel = (model: GatewayModelSelection, persist = true, withPulse = false) => {
    if (selectedModelRef.current === model) {
      if (withPulse) {
        triggerModelDialPulse(model)
      }
      return
    }
    selectedModelRef.current = model
    setSelectedModel(model)
    if (withPulse) {
      triggerModelDialPulse(model)
    }
    if (persist) {
      window.cosmic?.saveSetting('gatewayModelSelection', model)
    }
  }

  const scrollModelDialTo = (model: GatewayModelSelection, behavior: ScrollBehavior = 'smooth') => {
    const viewport = modelDialRef.current
    if (!viewport) {
      return
    }
    const target = viewport.querySelector<HTMLButtonElement>(`[data-model="${model}"]`)
    if (!target) {
      return
    }
    const targetLeft = target.offsetLeft - (viewport.clientWidth - target.offsetWidth) / 2
    viewport.scrollTo({
      left: Math.max(0, targetLeft),
      behavior,
    })
  }

  const getCenteredModelFromDial = (): GatewayModelSelection | null => {
    const viewport = modelDialRef.current
    if (!viewport) {
      return null
    }
    const buttons = Array.from(viewport.querySelectorAll<HTMLButtonElement>('[data-model]'))
    if (buttons.length === 0) {
      return null
    }

    const viewportCenter = viewport.scrollLeft + viewport.clientWidth / 2
    let closestModel: GatewayModelSelection | null = null
    let smallestDistance = Number.POSITIVE_INFINITY

    for (const button of buttons) {
      const model = button.dataset.model as GatewayModelSelection | undefined
      if (!model) {
        continue
      }
      const buttonCenter = button.offsetLeft + button.offsetWidth / 2
      const distance = Math.abs(buttonCenter - viewportCenter)
      if (distance < smallestDistance) {
        smallestDistance = distance
        closestModel = model
      }
    }

    return closestModel
  }

  const stepModelDial = (direction: -1 | 1) => {
    const currentModel = getCenteredModelFromDial() || selectedModelRef.current
    const currentIndex = MODEL_OPTIONS.findIndex((item) => item.id === currentModel)
    const nextIndex = Math.min(Math.max(currentIndex + direction, 0), MODEL_OPTIONS.length - 1)
    const nextModel = MODEL_OPTIONS[nextIndex]?.id
    if (!nextModel) {
      return
    }
    commitSelectedModel(nextModel, true, true)
    scrollModelDialTo(nextModel, 'smooth')
  }

  const showHoverTooltipForElement = (
    label: string,
    element: HTMLElement,
    tone: HoverTooltipTone,
  ) => {
    const rect = element.getBoundingClientRect()
    setHoverTooltip({
      label,
      x: rect.left + rect.width / 2,
      y: rect.top - 10,
      tone,
    })
  }

  const hideHoverTooltip = () => {
    setHoverTooltip(null)
  }

  const measureLaunchOffset = (element: HTMLElement | null, originX: number, originY: number) => {
    if (!element) {
      return { x: 0, y: 0 }
    }
    const rect = element.getBoundingClientRect()
    return {
      x: originX - (rect.left + rect.width / 2),
      y: originY - (rect.top + rect.height / 2),
    }
  }

  const clearSurfaceLaunch = () => {
    if (surfaceLaunchResetTimeoutRef.current !== null) {
      window.clearTimeout(surfaceLaunchResetTimeoutRef.current)
      surfaceLaunchResetTimeoutRef.current = null
    }
    setSurfaceLaunch(null)
  }

  const startSurfaceLaunch = (target: 'chat' | 'meeting', originX: number, originY: number) => {
    clearSurfaceLaunch()
    const composerAnchor = composerSurfaceRef.current
    const responseAnchor = chatResponseSurfaceRef.current || composerAnchor
    const meetingAnchor = meetingSurfaceRef.current || composerAnchor
    const composerOffset = measureLaunchOffset(composerAnchor, originX, originY)
    const responseOffset = measureLaunchOffset(responseAnchor, originX, originY)
    const meetingOffset = measureLaunchOffset(meetingAnchor, originX, originY)
    setSurfaceLaunch({
      target,
      token: Date.now(),
      composerOffsetX: composerOffset.x,
      composerOffsetY: composerOffset.y,
      responseOffsetX: responseOffset.x,
      responseOffsetY: responseOffset.y,
      meetingOffsetX: meetingOffset.x,
      meetingOffsetY: meetingOffset.y,
    })
    surfaceLaunchResetTimeoutRef.current = window.setTimeout(() => {
      setSurfaceLaunch(null)
      surfaceLaunchResetTimeoutRef.current = null
    }, 420)
  }

  const bindAssistantMessageToEvent = (event: any, messageId: string) => {
    const requestId = typeof event?.request_id === 'string' ? event.request_id.trim() : ''
    const taskId = typeof event?.task_id === 'string' ? event.task_id.trim() : ''
    if (requestId) {
      activeAssistantMessageByRequestRef.current.set(requestId, messageId)
    }
    if (taskId) {
      activeAssistantMessageByTaskRef.current.set(taskId, messageId)
    }
  }

  const findAssistantMessageIdForEvent = (event: any) => {
    const requestId = typeof event?.request_id === 'string' ? event.request_id.trim() : ''
    const taskId = typeof event?.task_id === 'string' ? event.task_id.trim() : ''
    if (taskId) {
      const taskBoundId = activeAssistantMessageByTaskRef.current.get(taskId)
      if (taskBoundId) {
        return taskBoundId
      }
    }
    if (requestId) {
      const requestBoundId = activeAssistantMessageByRequestRef.current.get(requestId)
      if (requestBoundId) {
        return requestBoundId
      }
    }
    return null
  }

  const forgetAssistantMessageBindings = (event: any) => {
    const requestId = typeof event?.request_id === 'string' ? event.request_id.trim() : ''
    const taskId = typeof event?.task_id === 'string' ? event.task_id.trim() : ''
    if (requestId) {
      activeAssistantMessageByRequestRef.current.delete(requestId)
      streamedResponseRequestIdsRef.current.delete(requestId)
    }
    if (taskId) {
      activeAssistantMessageByTaskRef.current.delete(taskId)
      streamedResponseTaskIdsRef.current.delete(taskId)
    }
  }

  const markResponseStreamSeen = (event: any) => {
    const requestId = typeof event?.request_id === 'string' ? event.request_id.trim() : ''
    const taskId = typeof event?.task_id === 'string' ? event.task_id.trim() : ''
    if (requestId) {
      streamedResponseRequestIdsRef.current.add(requestId)
    }
    if (taskId) {
      streamedResponseTaskIdsRef.current.add(taskId)
    }
  }

  const clearActiveStreamingRefs = () => {
    activeStreamingRequestIdRef.current = null
    activeStreamingTaskIdRef.current = null
  }

  const removePendingTaskInput = (inputRequestId: string) => {
    setPendingTaskInputs((prev) => prev.filter((item) => item.inputRequestId !== inputRequestId))
    setTaskInputDrafts((prev) => {
      if (!(inputRequestId in prev)) {
        return prev
      }
      const next = { ...prev }
      delete next[inputRequestId]
      return next
    })
    setSubmittingTaskInputs((prev) => {
      if (!(inputRequestId in prev)) {
        return prev
      }
      const next = { ...prev }
      delete next[inputRequestId]
      return next
    })
    setTaskInputErrors((prev) => {
      if (!(inputRequestId in prev)) {
        return prev
      }
      const next = { ...prev }
      delete next[inputRequestId]
      return next
    })
    setDismissedTaskInterruptIds((prev) => prev.filter((item) => item !== inputRequestId))
  }

  const removePendingTaskInputsForTask = (taskId: string) => {
    const normalizedTaskId = String(taskId || '').trim()
    if (!normalizedTaskId) {
      return
    }
    const relatedInputRequestIds = pendingTaskInputs
      .filter((item) => item.taskId === normalizedTaskId)
      .map((item) => item.inputRequestId)
    setPendingTaskInputs((prev) => prev.filter((item) => item.taskId !== normalizedTaskId))
    setTaskInputDrafts((prev) => {
      if (relatedInputRequestIds.length === 0) {
        return prev
      }
      const next = { ...prev }
      let changed = false
      for (const inputRequestId of relatedInputRequestIds) {
        if (inputRequestId in next) {
          delete next[inputRequestId]
          changed = true
        }
      }
      return changed ? next : prev
    })
    setSubmittingTaskInputs((prev) => {
      if (relatedInputRequestIds.length === 0) {
        return prev
      }
      const next = { ...prev }
      let changed = false
      for (const inputRequestId of relatedInputRequestIds) {
        if (inputRequestId in next) {
          delete next[inputRequestId]
          changed = true
        }
      }
      return changed ? next : prev
    })
    setTaskInputErrors((prev) => {
      if (relatedInputRequestIds.length === 0) {
        return prev
      }
      const next = { ...prev }
      let changed = false
      for (const inputRequestId of relatedInputRequestIds) {
        if (inputRequestId in next) {
          delete next[inputRequestId]
          changed = true
        }
      }
      return changed ? next : prev
    })
    setDismissedTaskInterruptIds((prev) => prev.filter((item) => !relatedInputRequestIds.includes(item)))
  }

  const dismissTaskInterrupt = (inputRequestId: string) => {
    setDismissedTaskInterruptIds((prev) => (
      prev.includes(inputRequestId) ? prev : [...prev, inputRequestId]
    ))
  }

  const resetDesktopVmSessionState = (detail = 'Signed out from your VM.') => {
    hideHoverTooltip()
    clearSurfaceLaunch()
    resetInFlightAssistantMaps()
    clearActiveStreamingRefs()
    setMessages([])
    setActiveSessionId(null)
    setPendingTaskInputs([])
    setTaskInputDrafts({})
    setSubmittingTaskInputs({})
    setTaskInputErrors({})
    setDismissedTaskInterruptIds([])
    setSelectedTaskInputId(null)
    setIsStreaming(false)
    setGatewayStatus({ state: 'idle', connected: false, detail, sessionId: null })
    setShowLauncherTray(false)
    setMode('chat')
    setQuery('')
    setIsInputFocused(false)
    shouldAutoScrollRef.current = true
    if (inputRef.current) {
      inputRef.current.style.height = '24px'
      inputRef.current.blur()
    }
  }

  const hasStreamedResponse = (event: any) => {
    const requestId = typeof event?.request_id === 'string' ? event.request_id.trim() : ''
    const taskId = typeof event?.task_id === 'string' ? event.task_id.trim() : ''
    if (taskId && streamedResponseTaskIdsRef.current.has(taskId)) {
      return true
    }
    if (requestId && streamedResponseRequestIdsRef.current.has(requestId)) {
      return true
    }
    return false
  }

  const ensureAssistantMessageForEvent = (messages: Message[], event: any) => {
    const boundId = findAssistantMessageIdForEvent(event)
    if (boundId) {
      bindAssistantMessageToEvent(event, boundId)
      return {
        messages,
        messageId: boundId,
      }
    }

    const last = messages[messages.length - 1]
    if (last?.role === 'assistant') {
      bindAssistantMessageToEvent(event, last.id)
      return {
        messages,
        messageId: last.id,
      }
    }

    const messageId = createAssistantMessageId()
    bindAssistantMessageToEvent(event, messageId)
    return {
      messages: [...messages, createAssistantMessage({ id: messageId })],
      messageId,
    }
  }

  const refreshSessionFromGateway = async (sessionId?: string | null) => {
    const targetSessionId = typeof sessionId === 'string' && sessionId.trim()
      ? sessionId.trim()
      : activeSessionIdRef.current
    if (!targetSessionId || !window.cosmic?.getGatewaySessionHistory) {
      return
    }

    try {
      const payload = await window.cosmic.getGatewaySessionHistory(targetSessionId)
      resetInFlightAssistantMaps()
      setMessages(historyToMessages(payload?.messages))
      setActiveSessionId(targetSessionId)
    } catch {
      return
    }
  }

  useEffect(() => {
    modeRef.current = mode
  }, [mode])

  useEffect(() => {
    messagesRef.current = messages
  }, [messages])

  useEffect(() => {
    activeSessionIdRef.current = activeSessionId
  }, [activeSessionId])

  useEffect(() => {
    selectedModelRef.current = selectedModel
  }, [selectedModel])

  useEffect(() => {
    return () => {
      if (modelDialPulseTimeoutRef.current !== null) {
        window.clearTimeout(modelDialPulseTimeoutRef.current)
      }
    }
  }, [])

  useEffect(() => {
    if (orderedPendingTaskInputs.length === 0) {
      if (selectedTaskInputId !== null) {
        setSelectedTaskInputId(null)
      }
      return
    }
    if (!selectedTaskInputId || !orderedPendingTaskInputs.some((item) => item.inputRequestId === selectedTaskInputId)) {
      setSelectedTaskInputId(orderedPendingTaskInputs[0].inputRequestId)
    }
  }, [orderedPendingTaskInputs, selectedTaskInputId])

  useEffect(() => {
    if (dismissedTaskInterruptIds.length === 0) {
      return
    }
    const validIds = new Set(pendingTaskInputs.map((item) => item.inputRequestId))
    setDismissedTaskInterruptIds((prev) => prev.filter((id) => validIds.has(id)))
  }, [dismissedTaskInterruptIds.length, pendingTaskInputs])

  useEffect(() => {
    if (visibleTaskInterrupts.length === 0) {
      if (taskInterruptIndex !== 0) {
        setTaskInterruptIndex(0)
      }
      return
    }
    if (taskInterruptIndex > visibleTaskInterrupts.length - 1) {
      setTaskInterruptIndex(visibleTaskInterrupts.length - 1)
    }
  }, [taskInterruptIndex, visibleTaskInterrupts.length])

  useEffect(() => {
    const container = taskInterruptStackRef.current
    if (!container || visibleTaskInterrupts.length === 0) {
      return
    }
    const targetLeft = container.clientWidth * taskInterruptIndex
    if (Math.abs(container.scrollLeft - targetLeft) > 2) {
      container.scrollTo({ left: targetLeft, behavior: 'smooth' })
    }
  }, [taskInterruptIndex, visibleTaskInterrupts.length])

  const handleTaskInterruptScroll = () => {
    const container = taskInterruptStackRef.current
    if (!container) {
      return
    }
    const cardWidth = container.clientWidth
    if (cardWidth <= 0) {
      return
    }
    const nextIndex = Math.max(
      0,
      Math.min(visibleTaskInterrupts.length - 1, Math.round(container.scrollLeft / cardWidth)),
    )
    if (nextIndex !== taskInterruptIndex) {
      setTaskInterruptIndex(nextIndex)
    }
  }

  const showChatComposer = () => {
    hideHoverTooltip()
    setMode('chat')
    setShowLauncherTray(false)
    setSearchState('visible')
    setIsInputFocused(true)
    setTimeout(() => {
      if (!inputRef.current) return
      inputRef.current.style.height = '24px'
      inputRef.current.focus()
    }, 10)
  }

  const showTaskSurface = (options: { focusComposer?: boolean; forceOpus?: boolean; focusInputRequestId?: string | null } = {}) => {
    const { focusComposer = false, forceOpus = true, focusInputRequestId = null } = options
    hideHoverTooltip()
    setDismissedTaskInterruptIds([])
    if (focusInputRequestId) {
      setSelectedTaskInputId(focusInputRequestId)
    }
    if (forceOpus) {
      commitSelectedModel('opus', true, true)
    }
    setSearchState('visible')
    setMode('task')
    setShowLauncherTray(false)
    setIsInputFocused(focusComposer)
    if (!focusComposer && inputRef.current) {
      inputRef.current.blur()
    }
    if (focusComposer) {
      setTimeout(() => {
        if (!inputRef.current) return
        inputRef.current.style.height = '24px'
        inputRef.current.focus()
      }, 10)
    }
  }

  const showMeetingSurface = () => {
    hideHoverTooltip()
    setSearchState('visible')
    setMode('meeting')
    setShowLauncherTray(false)
    setIsInputFocused(false)
  }

  // --- INIT & MOUSE EVENTS ---
  useEffect(() => {
    const unsubKeys = window.cosmic?.onKeyStatus((status) => {
      setKeyStatus({
        haiku: !!status.haiku,
        perplexity: !!status.perplexity,
        deepgram: !!status.deepgram,
        groq: !!status.groq,
        anthropic: !!status.anthropic,
      })
    })
    window.cosmic?.getLocalKeyStatus()

    // Load Settings + Auth Check
    window.cosmic?.getSettings()
    const unsubSettings = window.cosmic?.onSettingsUpdate((settings) => {
      console.log("App: Loaded settings", settings)
      if (settings['searchPosition']) setSearchPosition(settings['searchPosition'])
      if (settings['staybackTime']) setStaybackTime(parseInt(settings['staybackTime']))
      if (settings['islandOpacity']) setIslandOpacity(parseFloat(settings['islandOpacity']))
      if (settings['gatewayModelSelection']) {
        const nextModel = normalizeGatewayModelSelection(settings['gatewayModelSelection'])
        selectedModelRef.current = nextModel
        setSelectedModel(nextModel)
        requestAnimationFrame(() => {
          scrollModelDialTo(nextModel, 'auto')
        })
      }

      // Check auth from settings
      if (settings['cosmicAuth']) {
        try {
          const auth = typeof settings['cosmicAuth'] === 'string'
            ? JSON.parse(settings['cosmicAuth'])
            : settings['cosmicAuth']
          if (auth?.userId) {
            setAuthState('authenticated')
            setAuthData(auth)
            return
          }
        } catch { /* invalid JSON, treat as unauthenticated */ }
      }
      if (authState === 'loading') {
        setAuthState('unauthenticated')
      }
    })

    if (window.cosmic?.getGatewayState) {
      window.cosmic.getGatewayState()
        .then((state) => {
          if (!state) return
          if (state.status) {
            setGatewayStatus(state.status)
          }
          if (typeof state.sessionId === 'string') {
            setActiveSessionId(state.sessionId)
          }
          if (Array.isArray(state.historyTail) && state.historyTail.length > 0) {
            setMessages(historyToMessages(state.historyTail))
          }
        })
        .catch(() => { })
    }

    let lastIgnore: boolean | null = null
    let lastIsland: boolean | null = null

    const handleMouseMove = (e: MouseEvent) => {
      const el = document.elementFromPoint(e.clientX, e.clientY)
      if (!el) return
      const island = !!el.closest('.island')
      const settings = !!el.closest('.settings-overlay')
      const overlay = searchState !== 'hidden' && !!el.closest('.overlay')

      const isInteractive = island || settings || overlay

      if (lastIsland !== isInteractive) {
        lastIsland = isInteractive
        setIsIslandHovered(isInteractive)
      }
      const shouldIgnore = !(isInteractive || overlay)
      if (lastIgnore === shouldIgnore) return
      lastIgnore = shouldIgnore
      if (shouldIgnore) {
        ; (window as any).ipcRenderer.send('set-ignore-mouse-events', true, { forward: true })
      } else {
        ; (window as any).ipcRenderer.send('set-ignore-mouse-events', false)
      }
    }
    window.addEventListener('mousemove', handleMouseMove)
    return () => {
      unsubKeys?.()
      unsubSettings?.()
      window.removeEventListener('mousemove', handleMouseMove)
    }
  }, [searchState])

  // --- VISIBILITY HANDLERS ---
  const performHide = () => {
    hideHoverTooltip()
    setSearchState('hiding')
    setIsInputFocused(false)
    clearSurfaceLaunch()
    setTimeout(() => {
      setSearchState('hidden')
      setShowLauncherTray(false)
      if (modeRef.current === 'meeting') setMode('chat')
    }, 250)
  }

  useEffect(() => {
    const off1 = window.cosmic?.onShown(showChatComposer)
    const off2 = window.cosmic?.onHiding(performHide)
    const off3 = window.cosmic?.onMeetingInvoke(showMeetingSurface)
    const off4 = window.cosmic?.onMeetingToggle(() => {
      if (modeRef.current === 'meeting') {
        window.cosmic?.hide()
        return
      }
      showMeetingSurface()
    })

    return () => { off1?.(); off2?.(); off3?.(); off4?.() }
  }, [])

  useEffect(() => {
    const animationFrame = requestAnimationFrame(() => {
      scrollModelDialTo(selectedModelRef.current, 'auto')
    })

    return () => {
      cancelAnimationFrame(animationFrame)
      clearSurfaceLaunch()
      if (modelDialSettleTimeoutRef.current !== null) {
        window.clearTimeout(modelDialSettleTimeoutRef.current)
        modelDialSettleTimeoutRef.current = null
      }
    }
  }, [])

  // --- DATA LISTENERS ---
  useEffect(() => {
    const offEvent = window.cosmic?.onGatewayEvent((event) => {
      const eventType = String(event?.type || '')
      if (!eventType) return

      if (eventType === 'resume.ok') {
        setActiveSessionId(typeof event.session_id === 'string' ? event.session_id : null)
        resetInFlightAssistantMaps()
        setMessages(historyToMessages(event.history_tail))
        setPendingTaskInputs(
          mergePendingTaskInputs(
            [],
            Array.isArray(event.pending_inputs)
              ? event.pending_inputs.map(normalizePendingTaskInput).filter(Boolean) as PendingTaskInput[]
              : [],
          ),
        )
        setTaskInputDrafts({})
        setSubmittingTaskInputs({})
        setTaskInputErrors({})
        setDismissedTaskInterruptIds([])
        setSelectedTaskInputId(null)
        setIsStreaming(false)
        return
      }

      if (eventType === 'route_result') {
        if (typeof event.request_id === 'string' && event.request_id.trim()) {
          activeStreamingRequestIdRef.current = event.request_id.trim()
        }
        setActiveSessionId((prev) => typeof event.session_id === 'string' ? event.session_id : prev)
        setMessages((prev) => {
          const existingId = findAssistantMessageIdForEvent(event)
          if (existingId) {
            return prev
          }
          const messageId = createAssistantMessageId()
          bindAssistantMessageToEvent(event, messageId)
          return [...prev, createAssistantMessage({ id: messageId })]
        })
        return
      }

      if (eventType === 'task.created') {
        if (typeof event.task_id === 'string' && event.task_id.trim()) {
          activeStreamingTaskIdRef.current = event.task_id.trim()
        }
        setMessages((prev) => {
          const { messages: nextMessages } = ensureAssistantMessageForEvent(prev, event)
          return nextMessages
        })
        return
      }

      if (eventType === 'task.input_required') {
        const pendingInput = normalizePendingTaskInput(event)
        if (!pendingInput) {
          return
        }
        setPendingTaskInputs((prev) => mergePendingTaskInputs(prev, [pendingInput]))
        setTaskInputErrors((prev) => {
          if (!(pendingInput.inputRequestId in prev)) {
            return prev
          }
          const next = { ...prev }
          delete next[pendingInput.inputRequestId]
          return next
        })
        setDismissedTaskInterruptIds((prev) => prev.filter((id) => id !== pendingInput.inputRequestId))
        if (modeRef.current === 'task') {
          setSelectedTaskInputId(pendingInput.inputRequestId)
        }
        return
      }

      if (eventType === 'task.input_reply.accepted') {
        const inputRequestId = String(event?.input_request_id || '').trim()
        if (!inputRequestId) {
          return
        }
        removePendingTaskInput(inputRequestId)
        return
      }

      if (eventType === 'response.chunk') {
        markResponseStreamSeen(event)
        setMessages((prev) => {
          if (!event.content) return prev
          const { messages: nextMessages, messageId } = ensureAssistantMessageForEvent(prev, event)
          return nextMessages.map((message) => {
            if (message.id !== messageId) {
              return message
            }
            return {
              ...message,
              content: `${message.content}${String(event.content)}`,
              stopped: false,
            }
          })
        })
        return
      }

      if (eventType === 'response.thinking.chunk') {
        markResponseStreamSeen(event)
        setMessages((prev) => {
          if (!event.content) return prev
          const { messages: nextMessages, messageId } = ensureAssistantMessageForEvent(prev, event)
          return nextMessages.map((message) => {
            if (message.id !== messageId) {
              return message
            }
            return {
              ...message,
              thinking: `${message.thinking || ''}${String(event.content)}`,
              stopped: false,
            }
          })
        })
        return
      }

      if (eventType === 'response.complete') {
        markResponseStreamSeen(event)
        setActiveSessionId((prev) => typeof event.session_id === 'string' ? event.session_id : prev)
        setMessages((prev) => {
          const sources = Array.isArray(event.sources) ? event.sources : undefined
          const { messages: nextMessages, messageId } = ensureAssistantMessageForEvent(prev, event)
          const updatedMessages = nextMessages.map((message) => {
            if (message.id !== messageId) {
              return message
            }
            return {
              ...message,
              content: String(event.content || message.content || ''),
              sources,
              stopped: false,
            }
          })
          if (!event.task_id) {
            forgetAssistantMessageBindings(event)
          }
          return updatedMessages
        })
        setIsStreaming(false)
        clearActiveStreamingRefs()
        return
      }

      if (eventType === 'task.failed') {
        setIsStreaming(false)
        clearActiveStreamingRefs()
        if (event.task_id) {
          removePendingTaskInputsForTask(String(event.task_id))
        }
        const message = String(event?.error?.message || event?.message || 'Opus task failed.')
        setMessages((prev) => {
          const { messages: nextMessages, messageId } = ensureAssistantMessageForEvent(prev, event)
          return nextMessages.map((item) => {
            if (item.id !== messageId) {
              return item
            }
            return { ...item, content: message }
          })
        })
        forgetAssistantMessageBindings(event)
        return
      }

      if (eventType === 'task.completed' || eventType === 'task.cancelled') {
        setIsStreaming(false)
        clearActiveStreamingRefs()
        if (event.task_id) {
          removePendingTaskInputsForTask(String(event.task_id))
        }
        const messageId = findAssistantMessageIdForEvent(event)
        const boundMessage = messageId
          ? messagesRef.current.find((item) => item.id === messageId)
          : null
        const shouldRefreshFromHistory =
          eventType === 'task.completed' &&
          !hasStreamedResponse(event) &&
          (!boundMessage || !String(boundMessage.content || '').trim())
        forgetAssistantMessageBindings(event)
        if (eventType === 'task.cancelled' && messageId && boundMessage && !String(boundMessage.content || '').trim() && !String(boundMessage.thinking || '').trim()) {
          setMessages((prev) => prev.filter((item) => item.id !== messageId))
          return
        }
        if (eventType === 'task.cancelled' && messageId) {
          setMessages((prev) => prev.map((item) => {
            if (item.id !== messageId) {
              return item
            }
            if (!String(item.content || '').trim() && !String(item.thinking || '').trim()) {
              return item
            }
            return {
              ...item,
              stopped: true,
            }
          }))
          return
        }
        if (shouldRefreshFromHistory) {
          void refreshSessionFromGateway(typeof event.session_id === 'string' ? event.session_id : null)
        }
        return
      }

      if (eventType === 'error') {
        setIsStreaming(false)
        clearActiveStreamingRefs()
        if (event.message) {
          setMessages((prev) => [...prev, {
            ...createAssistantMessage(),
            content: String(event.message),
          }])
        }
      }
    })

    const offStatus = window.cosmic?.onGatewayStatus((status) => {
      if (!status) return
      setGatewayStatus(status)
      if (typeof status?.sessionId === 'string') {
        setActiveSessionId(status.sessionId)
      }
      if (status?.state === 'error' || status?.state === 'idle') {
        setIsStreaming(false)
        clearActiveStreamingRefs()
      }
    })

    return () => { offEvent?.(); offStatus?.() }
  }, [])

  useEffect(() => {
    const shouldFocusComposer =
      mode === 'chat' || (mode === 'task' && pendingTaskCount === 0)
    if (!isStreaming && searchState === 'visible' && !showLauncherTray && mode !== 'meeting' && shouldFocusComposer) {
      setTimeout(() => inputRef.current?.focus(), 10)
    }
  }, [isStreaming, mode, pendingTaskCount, searchState, showLauncherTray])

  useEffect(() => {
    if (authState !== 'authenticated') {
      resetDesktopVmSessionState('Desktop is signed out from your VM.')
      return
    }

    if (window.cosmic?.requestGatewayResume) {
      window.cosmic.requestGatewayResume().catch(() => { })
    }
  }, [authState])

  // --- ACTIONS ---
  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setQuery(e.target.value)
    const target = e.target
    target.style.height = 'auto'
    const newHeight = Math.min(target.scrollHeight, 120)
    target.style.height = `${newHeight}px`
  }

  const handleSubmit = () => {
    if (mode === 'meeting') return
    setIsInputFocused(false)
    if (inputRef.current) inputRef.current.blur()

    if (!query.trim() || isStreaming) return

    const textToSend = query
    setQuery('')
    if (inputRef.current) inputRef.current.style.height = '24px'
    shouldAutoScrollRef.current = true

    setMessages(prev => [...prev, createUserMessage(textToSend)])
    if (authState !== 'authenticated') {
      setMessages(prev => [...prev, {
        ...createAssistantMessage(),
        content: "Please sign in to connect this desktop app to your VM."
      }])
      return
    }

    if (!gatewayStatus.connected) {
      setMessages(prev => [...prev, {
        ...createAssistantMessage(),
        content: gatewayStatus.detail || "The desktop app is not connected to your VM yet."
      }])
      return
    }

    const requestId = `req_${crypto.randomUUID()}`
    const assistantMessageId = createAssistantMessageId()
    activeStreamingRequestIdRef.current = requestId
    activeStreamingTaskIdRef.current = null
    activeAssistantMessageByRequestRef.current.set(requestId, assistantMessageId)

    // Reserve the assistant slot before the first streaming event arrives.
    setMessages(prev => [
      ...prev,
      createAssistantMessage({ id: assistantMessageId }),
    ])

    setIsStreaming(true)
    if (!window.cosmic?.sendGatewayQuery) {
      setIsStreaming(false)
      activeAssistantMessageByRequestRef.current.delete(requestId)
      setMessages(prev => [...prev, {
        ...createAssistantMessage(),
        content: "Gateway chat support is unavailable in this desktop build."
      }])
      return
    }

    const effectiveRouteOverride =
      mode === 'task'
        ? 'opus'
        : selectedModel === 'cosmic'
          ? undefined
          : selectedModel

    window.cosmic.sendGatewayQuery({
      requestId,
      content: textToSend,
      conversationContext: buildConversationContext([...messages, createUserMessage(textToSend)]),
      routeOverride: effectiveRouteOverride,
    }).then((result) => {
      const confirmedRequestId = typeof result?.requestId === 'string' ? result.requestId.trim() : ''
      if (confirmedRequestId && confirmedRequestId !== requestId) {
        activeAssistantMessageByRequestRef.current.delete(requestId)
        activeAssistantMessageByRequestRef.current.set(confirmedRequestId, assistantMessageId)
        activeStreamingRequestIdRef.current = confirmedRequestId
      }
    }).catch((error: any) => {
      setIsStreaming(false)
      clearActiveStreamingRefs()
      activeAssistantMessageByRequestRef.current.delete(requestId)
      setMessages(prev => prev.map((message) => {
        if (message.id !== assistantMessageId) {
          return message
        }
        return {
          ...message,
          content: error?.message || "Unable to send the message to your VM.",
        }
      }))
    })
  }

  const handleTaskInputDraftChange = (inputRequestId: string, value: string) => {
    setTaskInputDrafts((prev) => ({
      ...prev,
      [inputRequestId]: value,
    }))
    setTaskInputErrors((prev) => {
      if (!(inputRequestId in prev)) {
        return prev
      }
      const next = { ...prev }
      delete next[inputRequestId]
      return next
    })
  }

  const submitTaskInputReply = async (taskInput: PendingTaskInput, explicitContent?: string) => {
    const content = String(explicitContent ?? taskInputDrafts[taskInput.inputRequestId] ?? '').trim()
    if (!content) {
      setTaskInputErrors((prev) => ({
        ...prev,
        [taskInput.inputRequestId]: 'Reply required',
      }))
      return
    }
    if (!window.cosmic?.submitGatewayTaskInputReply) {
      setTaskInputErrors((prev) => ({
        ...prev,
        [taskInput.inputRequestId]: 'Task replies are unavailable in this desktop build.',
      }))
      return
    }
    setSubmittingTaskInputs((prev) => ({
      ...prev,
      [taskInput.inputRequestId]: true,
    }))
    setTaskInputErrors((prev) => {
      if (!(taskInput.inputRequestId in prev)) {
        return prev
      }
      const next = { ...prev }
      delete next[taskInput.inputRequestId]
      return next
    })
    try {
      await window.cosmic.submitGatewayTaskInputReply({
        inputRequestId: taskInput.inputRequestId,
        taskId: taskInput.taskId,
        content,
      })
      setTaskInputDrafts((prev) => ({
        ...prev,
        [taskInput.inputRequestId]: content,
      }))
    } catch (error: any) {
      setSubmittingTaskInputs((prev) => {
        const next = { ...prev }
        delete next[taskInput.inputRequestId]
        return next
      })
      setTaskInputErrors((prev) => ({
        ...prev,
        [taskInput.inputRequestId]: error?.message || 'Unable to send task reply.',
      }))
    }
  }

  const handleStopStreaming = () => {
    const requestId = activeStreamingRequestIdRef.current
    const taskId = activeStreamingTaskIdRef.current
    if (!requestId && !taskId) {
      return
    }
    const cancelPromise = window.cosmic?.cancelGatewayResponse?.({
      requestId: requestId || undefined,
      taskId: taskId || undefined,
    })
    cancelPromise?.catch(() => { })
  }

  const handleShowLauncherTray = () => {
    hideHoverTooltip()
    setShowLauncherTray(true)
    setIsInputFocused(false)
    if (inputRef.current) {
      inputRef.current.blur()
    }
  }

  const handleLauncherTileClick = (tile: LauncherTileId, event: React.MouseEvent<HTMLButtonElement>) => {
    hideHoverTooltip()
    if (tile === 'spaces') {
      return
    }
    const rect = event.currentTarget.getBoundingClientRect()
    const originX = rect.left + rect.width / 2
    const originY = rect.top + rect.height / 2
    startSurfaceLaunch(tile === 'meeting' ? 'meeting' : 'chat', originX, originY)
    if (tile === 'meeting') {
      showMeetingSurface()
      return
    }
    if (tile === 'task') {
      showTaskSurface({ focusComposer: pendingTaskInputs.length === 0, forceOpus: true })
      return
    }
    showChatComposer()
  }

  const handleModelDialScroll = () => {
    hideHoverTooltip()
    if (modelDialSettleTimeoutRef.current !== null) {
      window.clearTimeout(modelDialSettleTimeoutRef.current)
    }
    modelDialSettleTimeoutRef.current = window.setTimeout(() => {
      const nextModel = getCenteredModelFromDial()
      if (!nextModel) {
        return
      }
      commitSelectedModel(nextModel, true, true)
      scrollModelDialTo(nextModel, 'smooth')
    }, 140)
  }

  const handleModelDialWheel = (event: React.WheelEvent<HTMLDivElement>) => {
    const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY
    if (Math.abs(delta) < 4) {
      return
    }
    event.preventDefault()
    hideHoverTooltip()
    const now = Date.now()
    if (now < modelDialWheelLockUntilRef.current) {
      return
    }
    modelDialWheelLockUntilRef.current = now + 180
    stepModelDial(delta > 0 ? 1 : -1)
  }

  const handleModelDialFocus = (model: GatewayModelSelection) => {
    commitSelectedModel(model, true, true)
    scrollModelDialTo(model, 'smooth')
  }

  const handleVmLogout = async () => {
    hideHoverTooltip()
    const requestId = activeStreamingRequestIdRef.current
    const taskId = activeStreamingTaskIdRef.current
    if (requestId || taskId) {
      try {
        await window.cosmic?.cancelGatewayResponse?.({
          requestId: requestId || undefined,
          taskId: taskId || undefined,
        })
      } catch {
        // Best-effort cancellation before transport teardown.
      }
    }

    try {
      await window.cosmic?.logout?.()
    } catch {
      // Local UI still needs to reset even if IPC logout fails.
    }

    setAuthState('unauthenticated')
    setAuthData(null)
    resetDesktopVmSessionState('Signed out from your VM.')
  }

  const handleCopy = async (text: string, id: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopiedId(id)
      setTimeout(() => setCopiedId(null), 2000)
    } catch (err) {
      console.error('Failed to copy:', err)
    }
  }

  const scrollToBottom = () => {
    responseEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }

  const handleScroll = () => {
    if (!responseContainerRef.current) return
    const { scrollTop, scrollHeight, clientHeight } = responseContainerRef.current
    const distanceFromBottom = scrollHeight - scrollTop - clientHeight
    const isNearBottom = distanceFromBottom < 50
    shouldAutoScrollRef.current = isNearBottom
    setShowScrollButton(!isNearBottom && messages.length > 1)
  }

  useEffect(() => {
    if (shouldAutoScrollRef.current || isStreaming) {
      responseEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages, isStreaming])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (searchState === 'visible') {
          window.cosmic?.hide()
        }
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [searchState])

  useEffect(() => {
    const handleResize = () => {
      setViewportSize({
        width: window.innerWidth,
        height: window.innerHeight,
      })
    }
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  // Render Classes
  const shouldShowTaskInterrupt =
    visibleTaskInterrupts.length > 0 &&
    mode !== 'task' &&
    mode !== 'meeting' &&
    !showLauncherTray
  const shouldShowPrimarySurface = mode !== 'meeting' && !showLauncherTray
  const shouldShowResponseSurface = shouldShowPrimarySurface && (mode === 'task' || messages.length > 0)
  const taskRailLayout = useMemo(() => {
    if (!shouldShowTaskInterrupt) {
      return null
    }

    const edgePadding = viewportSize.width >= 1440 ? 32 : 24
    const railGap = 20
    const preferredRailWidth = viewportSize.width >= 1380 ? 360 : 328
    const minimumRailWidth = viewportSize.width < 920 ? 228 : 252
    const minimumMainWidth = shouldShowResponseSurface ? 420 : 360

    const railWidth = Math.min(
      preferredRailWidth,
      Math.max(
        minimumRailWidth,
        viewportSize.width - (minimumMainWidth + edgePadding * 2 + railGap),
      ),
    )
    const reserve = railWidth + railGap + edgePadding
    const compact = railWidth < 286
    const top = viewportSize.height < 820 ? 176 : 200

    return {
      edgePadding,
      railWidth,
      reserve,
      compact,
      top,
    }
  }, [shouldShowTaskInterrupt, shouldShowResponseSurface, viewportSize.height, viewportSize.width])
  const effectivePosition = (messages.length > 0 || mode === 'task') ? 'bottom' : searchPosition
  const overlayClass = [
    searchState === 'hidden' ? '' : 'visible',
    effectivePosition === 'middle' ? 'position-middle' : '',
    shouldShowResponseSurface ? 'has-response' : '',
    (isInputFocused || shouldShowResponseSurface || isStreaming || mode === 'meeting') ? 'focused' : ''
  ].join(' ')
  const composerLaunchClass = surfaceLaunch?.target === 'chat' ? 'launcher-expand' : ''
  const composerLaunchStyle = surfaceLaunch?.target === 'chat'
    ? ({
      ['--launch-offset-x' as string]: `${surfaceLaunch.composerOffsetX}px`,
      ['--launch-offset-y' as string]: `${surfaceLaunch.composerOffsetY}px`,
    } as React.CSSProperties)
    : undefined
  const responseLaunchClass = surfaceLaunch?.target === 'chat' ? 'launcher-expand' : ''
  const responseLaunchStyle = surfaceLaunch?.target === 'chat'
    ? ({
      ['--launch-offset-x' as string]: `${surfaceLaunch.responseOffsetX}px`,
      ['--launch-offset-y' as string]: `${surfaceLaunch.responseOffsetY}px`,
    } as React.CSSProperties)
    : undefined
  const meetingLaunchClass = surfaceLaunch?.target === 'meeting' ? 'launcher-expand' : ''
  const meetingLaunchStyle = surfaceLaunch?.target === 'meeting'
    ? ({
      ['--launch-offset-x' as string]: `${surfaceLaunch.meetingOffsetX}px`,
      ['--launch-offset-y' as string]: `${surfaceLaunch.meetingOffsetY}px`,
    } as React.CSSProperties)
    : undefined
  const overlayStyle = {
    pointerEvents: searchState === 'visible' ? 'auto' : 'none',
    ['--task-rail-reserve' as string]: taskRailLayout ? `${taskRailLayout.reserve}px` : '0px',
  } as React.CSSProperties
  const taskInterruptStyle = taskRailLayout
    ? ({
      ['--task-rail-width' as string]: `${taskRailLayout.railWidth}px`,
      ['--task-rail-edge' as string]: `${taskRailLayout.edgePadding}px`,
      ['--task-rail-top' as string]: `${taskRailLayout.top}px`,
    } as React.CSSProperties)
    : undefined
  const hasMultiplePendingTaskInputs = orderedPendingTaskInputs.length > 1

  return (
    <>
      <DynamicIsland
        searchActive={searchState === 'visible'}
        hovered={isIslandHovered}
        debug={false}
        searchPosition={searchPosition}
        onPositionChange={(pos) => {
          setSearchPosition(pos)
          window.cosmic?.saveSetting('searchPosition', pos)
        }}
        staybackTime={staybackTime}
        onStaybackChange={(time) => {
          setStaybackTime(time)
          window.cosmic?.saveSetting('staybackTime', time)
        }}
        islandOpacity={islandOpacity}
        onOpacityChange={(val) => {
          setIslandOpacity(val)
          window.cosmic?.saveSetting('islandOpacity', val)
        }}
        keyStatus={keyStatus}
        authData={authData}
        onLogout={handleVmLogout}
      />

      {authState === 'unauthenticated' && (
        <CosmicLoginModal
          onAuthenticated={(data) => {
            setAuthState('authenticated')
            setAuthData(data)
          }}
        />
      )}

      <div
        className={`overlay ${overlayClass}`}
        onDoubleClick={(e) => {
          if (e.target === e.currentTarget) window.cosmic?.hide()
        }}
        style={overlayStyle}
      >
        <MeetingMode
          active={mode === 'meeting'}
          keyStatus={keyStatus}
          onBackToChat={showChatComposer}
          containerRef={meetingSurfaceRef}
          containerClassName={meetingLaunchClass}
          containerStyle={meetingLaunchStyle}
        />

        {shouldShowTaskInterrupt && visibleTaskInterrupts.length > 0 && (
          <div className={`task-interrupt-shell ${taskRailLayout?.compact ? 'compact' : ''}`} style={taskInterruptStyle}>
            <div
              ref={taskInterruptStackRef}
              className="task-interrupt-stack"
              role="list"
              aria-label={`${visibleTaskInterrupts.length} task inputs waiting`}
              onScroll={handleTaskInterruptScroll}
            >
              {visibleTaskInterrupts.map((taskInput, index) => (
                <LiquidGlass
                  key={taskInput.inputRequestId}
                  disableTilt={true}
                  cornerRadius={30}
                  className="task-interrupt-glass"
                  style={{ width: '100%' }}
                >
                  <div className="task-interrupt-card">
                    <div className="task-interrupt-head">
                      <div className="task-interrupt-title-cluster">
                        <div className="task-interrupt-logo-shell" aria-hidden="true">
                          <img
                            src={cosmicGlassyThunderLogo}
                            alt=""
                            className="task-interrupt-logo"
                            draggable={false}
                          />
                        </div>
                        <div className="task-interrupt-copy">
                          <div className="task-interrupt-kicker">Task needs your input</div>
                          <div className="task-interrupt-meta">
                            {visibleTaskInterrupts.length > 1
                              ? `${index + 1} of ${visibleTaskInterrupts.length} waiting`
                              : taskInput.options.length > 0
                                ? `${taskInput.options.length} quick choices in Task Inbox`
                                : 'Open Task Inbox to continue'}
                          </div>
                        </div>
                      </div>
                      <div className="task-interrupt-chip-row">
                        {visibleTaskInterrupts.length > 1 && (
                          <div className="task-interrupt-chip count">{visibleTaskInterrupts.length} waiting</div>
                        )}
                        <div className="task-interrupt-chip">Opus task</div>
                      </div>
                    </div>
                    <div className="task-interrupt-preview">{taskInput.question}</div>
                    <div className="task-interrupt-actions">
                      <button
                        type="button"
                        className="task-interrupt-btn secondary"
                        onClick={() => dismissTaskInterrupt(taskInput.inputRequestId)}
                      >
                        Later
                      </button>
                      <button
                        type="button"
                        className="task-interrupt-btn primary"
                        onClick={() => {
                          setDismissedTaskInterruptIds([])
                          showTaskSurface({
                            focusComposer: false,
                            forceOpus: true,
                            focusInputRequestId: taskInput.inputRequestId,
                          })
                        }}
                      >
                        Reply
                      </button>
                    </div>
                  </div>
                </LiquidGlass>
              ))}
            </div>
            {visibleTaskInterrupts.length > 1 && (
              <div
                className="task-interrupt-dots"
                aria-label={`Task card ${taskInterruptIndex + 1} of ${visibleTaskInterrupts.length}`}
              >
                {visibleTaskInterrupts.map((taskInput, index) => (
                  <button
                    key={taskInput.inputRequestId}
                    type="button"
                    className={`task-interrupt-dot ${taskInterruptIndex === index ? 'active' : ''}`}
                    aria-label={`Show task ${index + 1}`}
                    aria-current={taskInterruptIndex === index}
                    onClick={() => {
                      const container = taskInterruptStackRef.current
                      if (!container) {
                        return
                      }
                      container.scrollTo({ left: container.clientWidth * index, behavior: 'smooth' })
                      setTaskInterruptIndex(index)
                    }}
                  />
                ))}
              </div>
            )}
          </div>
        )}

        {/* PRIMARY CONTENT AREA */}
        {shouldShowResponseSurface && (
          <div
            ref={chatResponseSurfaceRef}
            className={`response-container ${searchState === 'visible' ? 'visible' : ''} ${responseLaunchClass}`}
            style={responseLaunchStyle}
          >
            <LiquidGlass disableTilt={true} cornerRadius={32} style={{ width: '100%', height: '100%' }}>
              <div className="response-wrapper">
                <div className="response-content" style={{ paddingTop: 24 }} ref={responseContainerRef} onScroll={handleScroll}>
                  {mode === 'task' && (
                    <div className="task-hub">
                      <div className="task-hub-header">
                        <div>
                          <div className="task-hub-kicker">Task Inbox</div>
                          <h2 className="task-hub-title">
                            {pendingTaskCount > 0 ? `${pendingTaskCount} request${pendingTaskCount === 1 ? '' : 's'} waiting` : 'No task input needed right now'}
                          </h2>
                        </div>
                        <button type="button" className="task-hub-chat-btn" onClick={showChatComposer}>
                          Return to chat
                        </button>
                      </div>

                      {pendingTaskCount > 0 && selectedTaskInput ? (
                        <div className={`task-hub-body ${hasMultiplePendingTaskInputs ? '' : 'single'}`}>
                          {hasMultiplePendingTaskInputs && (
                            <div className="task-list-pane">
                              <div className="task-list">
                                {orderedPendingTaskInputs.map((taskInput) => (
                                  <button
                                    key={taskInput.inputRequestId}
                                    type="button"
                                    className={`task-list-item ${selectedTaskInput.inputRequestId === taskInput.inputRequestId ? 'active' : ''}`}
                                    onClick={() => setSelectedTaskInputId(taskInput.inputRequestId)}
                                    >
                                      <div className="task-list-item-label-row">
                                        <span className="task-list-item-label">Task</span>
                                        <span className="task-list-item-status">
                                          {taskInput.options.length > 0
                                            ? `${taskInput.options.length} choice${taskInput.options.length === 1 ? '' : 's'}`
                                            : 'Reply needed'}
                                        </span>
                                      </div>
                                      <div className="task-list-item-question">{taskInput.question}</div>
                                    </button>
                                  ))}
                                </div>
                              </div>
                          )}

                          <div className="task-detail-pane">
                            {(() => {
                              const taskInput = selectedTaskInput
                              const isSubmitting = Boolean(submittingTaskInputs[taskInput.inputRequestId])
                              const draftValue = taskInputDrafts[taskInput.inputRequestId] || ''
                              const errorText = taskInputErrors[taskInput.inputRequestId]

                              return (
                                <div className="task-card task-detail-card">
                                  <div className="task-card-topline">
                                    <span className="task-card-badge">Awaiting input</span>
                                    <span className="task-card-meta">{taskInput.taskId}</span>
                                  </div>
                                  <div className="task-card-question task-detail-question">{taskInput.question}</div>
                                  <div className="task-detail-copy">
                                    Choose a quick option or send a custom reply to resume this task without disturbing your current conversation.
                                  </div>
                                  {taskInput.options.length > 0 && (
                                    <div className="task-card-options">
                                      {taskInput.options.map((option) => (
                                        <button
                                          key={option}
                                          type="button"
                                          className="task-option-chip"
                                          disabled={isSubmitting}
                                          onClick={() => void submitTaskInputReply(taskInput, option)}
                                        >
                                          {option}
                                        </button>
                                      ))}
                                    </div>
                                  )}
                                  <textarea
                                    className="task-reply-input"
                                    value={draftValue}
                                    placeholder="Add a reply for this task..."
                                    disabled={isSubmitting}
                                    onChange={(event) => handleTaskInputDraftChange(taskInput.inputRequestId, event.target.value)}
                                  />
                                  <div className="task-card-footer">
                                    <div className="task-card-status">
                                      {errorText ? (
                                        <span className="task-card-error">{errorText}</span>
                                      ) : isSubmitting ? (
                                        <span className="task-card-waiting">Submitting...</span>
                                      ) : (
                                        <span className="task-card-hint">Reply keeps the task moving without interrupting your current chat.</span>
                                      )}
                                    </div>
                                    <button
                                      type="button"
                                      className="task-submit-btn"
                                      disabled={isSubmitting}
                                      onClick={() => void submitTaskInputReply(taskInput)}
                                    >
                                      Send reply
                                    </button>
                                  </div>
                                </div>
                              )
                            })()}
                          </div>
                        </div>
                      ) : (
                        <div className="task-empty-state">
                          <div className="task-empty-icon">◌</div>
                          <div className="task-empty-title">Tasks will collect here when they need you.</div>
                          <div className="task-empty-copy">
                            Long-running Opus work can pause for clarification without interrupting your active chat screen.
                          </div>
                        </div>
                      )}
                    </div>
                  )}

                  {/* MESSAGES */}
                  {mode !== 'task' && messages.map((msg, idx) => {
                    // Cross-channel messages: show as collapsible row with channel badge
                    const extLabel = channelLabel(msg.channel)
                    if (extLabel && msg.role === 'user') {
                      const isExpanded = expandedCrossChannelIds.has(msg.id)
                      // Find the assistant response paired with this user message (next message)
                      const pairedResponse = messages[idx + 1]?.role === 'assistant' && isExternalChannel(messages[idx + 1])
                        ? messages[idx + 1] : null
                      return (
                        <div key={msg.id} className="cross-channel-group" style={{ marginBottom: 16 }}>
                          <button
                            className="cross-channel-bar"
                            onClick={() => setExpandedCrossChannelIds((prev) => {
                              const next = new Set(prev)
                              if (next.has(msg.id)) next.delete(msg.id)
                              else next.add(msg.id)
                              return next
                            })}
                            style={{
                              width: '100%', display: 'flex', alignItems: 'center', gap: 10,
                              padding: '10px 14px', borderRadius: 12,
                              background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.08)',
                              color: 'rgba(255,255,255,0.6)', cursor: 'pointer', fontSize: 13,
                              transition: 'background 0.15s',
                            }}
                          >
                            <span style={{
                              padding: '2px 8px', borderRadius: 6, fontSize: 11, fontWeight: 600,
                              background: extLabel === 'WhatsApp' ? 'rgba(37,211,102,0.15)' : 'rgba(0,136,204,0.15)',
                              color: extLabel === 'WhatsApp' ? '#25d366' : '#0088cc',
                            }}>
                              {extLabel}
                            </span>
                            <span style={{ flex: 1, textAlign: 'left', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {cleanText(msg.content).slice(0, 80)}{msg.content.length > 80 ? '...' : ''}
                            </span>
                            <span style={{ fontSize: 11, opacity: 0.5 }}>
                              {isExpanded ? 'collapse' : 'expand'}
                            </span>
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"
                              style={{ transform: isExpanded ? 'rotate(180deg)' : 'rotate(0deg)', transition: 'transform 0.2s' }}>
                              <path d="M7 10l5 5 5-5z" />
                            </svg>
                          </button>
                          {isExpanded && (
                            <div style={{ padding: '12px 14px 4px', borderLeft: '2px solid rgba(255,255,255,0.06)', marginLeft: 16 }}>
                              <div className="message-row user" style={{ marginBottom: 12, display: 'flex', flexDirection: 'column', alignItems: 'flex-end' }}>
                                <div className="query-pill" style={{ maxWidth: '70%', alignSelf: 'flex-end', position: 'relative' }}>
                                  <span style={{ display: 'inline-block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '100%' }}>
                                    {cleanText(msg.content)}
                                  </span>
                                </div>
                              </div>
                              {pairedResponse && (
                                <div className="message-row assistant" style={{ marginBottom: 8, display: 'flex', flexDirection: 'column', alignItems: 'flex-start' }}>
                                  <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]}
                                    components={{
                                      table: ({ node, ...props }) => <div className="table-wrapper"><table {...props} /></div>,
                                      code: ({ node, inline, className, children, ...props }: any) => {
                                        if (inline) return <code className="inline-code" {...props}>{children}</code>
                                        return <div className="code-block"><code {...props}>{children}</code></div>
                                      },
                                      a: ({ node, ...props }) => <a target="_blank" rel="noopener noreferrer" {...props} />
                                    }}>
                                    {pairedResponse.content}
                                  </ReactMarkdown>
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      )
                    }
                    // Skip assistant messages that belong to a cross-channel pair (rendered above with their user message)
                    if (extLabel && msg.role === 'assistant') {
                      const prevMsg = idx > 0 ? messages[idx - 1] : null
                      if (prevMsg && prevMsg.role === 'user' && isExternalChannel(prevMsg)) {
                        return null // Already rendered as part of the collapsed group
                      }
                    }

                    return (
                    <div key={msg.id} className={`message-row ${msg.role}`} style={{ marginBottom: 24, display: 'flex', flexDirection: 'column', alignItems: msg.role === 'user' ? 'flex-end' : 'flex-start' }}>

                      {msg.role === 'user' ? (
                        <div className="query-pill" style={{ maxWidth: '70%', alignSelf: 'flex-end', position: 'relative' }}>
                          <span style={{
                            display: 'inline-block',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                            maxWidth: '100%'
                          }}>
                            {cleanText(msg.content)}
                          </span>
                          <button
                            className="copy-btn"
                            onClick={() => handleCopy(msg.content, `user-${idx}`)}
                          >
                            {copiedId === `user-${idx}` ? (
                              <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                                <path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z" />
                              </svg>
                            ) : (
                              <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                                <path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z" />
                              </svg>
                            )}
                          </button>
                        </div>
                      ) : (
                        <>
                          {msg.thinking && (
                            <div className="thinking-block">
                              <div className="thinking-label">Thinking</div>
                              <div className="thinking-text">{msg.thinking}</div>
                            </div>
                          )}
                          <ReactMarkdown
                            remarkPlugins={[remarkGfm, remarkMath]}
                            rehypePlugins={[rehypeKatex]}
                            components={{
                              table: ({ node, ...props }) => <div className="table-wrapper"><table {...props} /></div>,
                              code: ({ node, inline, className, children, ...props }: any) => {
                                if (inline) return <code className="inline-code" {...props}>{children}</code>
                                return <div className="code-block"><code {...props}>{children}</code></div>
                              },
                              a: ({ node, ...props }) => <a target="_blank" rel="noopener noreferrer" {...props} />
                            }}
                          >
                            {msg.content}
                          </ReactMarkdown>

                          {/* Copy Button for AI Response (Bottom) */}
                          <button
                            className="copy-btn-ai"
                            onClick={() => handleCopy(msg.content, `ai-${idx}`)}
                            style={{ marginTop: 12, alignSelf: 'flex-start' }}
                          >
                            {copiedId === `ai-${idx}` ? (
                              <>
                                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style={{ marginRight: 6 }}>
                                  <path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z" />
                                </svg>
                                Copied
                              </>
                            ) : (
                              <>
                                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style={{ marginRight: 6 }}>
                                  <path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z" />
                                </svg>
                                Copy
                              </>
                            )}
                          </button>

                          {/* Sources for Assistant Messages (Bottom) */}
                          {msg.role === 'assistant' && msg.sources && msg.sources.length > 0 && (
                            <div className="sources-section" style={{ marginTop: 16, marginBottom: 4, width: '100%' }}>
                              <div className="sources-header">SOURCES</div>
                              <div className="sources-grid">
                                {msg.sources.map((src: any, sIdx: number) => {
                                  // Handle both old string format and new object format
                                  const url = typeof src === 'string' ? src : src.url;
                                  const title = typeof src === 'object' ? src.title : null;

                                  let domain = "Unknown";
                                  try {
                                    domain = new URL(url).hostname.replace('www.', '');
                                  } catch (e) { }

                                  return (
                                    <a
                                      key={sIdx}
                                      href={url}
                                      onClick={(e) => {
                                        e.preventDefault()
                                        window.cosmic?.openExternal(url)
                                      }}
                                      className="source-card"
                                    >
                                      <div className="source-header-row" style={{ display: 'flex', alignItems: 'center', marginBottom: 6 }}>
                                        <img
                                          src={`https://www.google.com/s2/favicons?domain=${domain}&sz=64`}
                                          alt=""
                                          style={{ width: 16, height: 16, marginRight: 8, borderRadius: 2 }}
                                        />
                                        <div className="source-title" style={{ fontSize: '11px', fontWeight: 600, opacity: 0.9 }}>
                                          {title || domain}
                                        </div>
                                      </div>
                                      <div className="source-footer">
                                        <span className="source-idx">{sIdx + 1}</span>
                                        <span style={{ fontSize: '10px', opacity: 0.7, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: '80%' }}>{url}</span>
                                      </div>
                                    </a>
                                  )
                                })}
                              </div>
                            </div>
                          )}
                          {msg.role === 'assistant' && msg.stopped && (String(msg.content || '').trim() || String(msg.thinking || '').trim()) && (
                            <div className="response-stopped-label">Stopped</div>
                          )}
                        </>
                      )}
                    </div>
                    )
                  })}

                  {mode !== 'task' && isStreaming && (
                    <div className="streaming-indicator">
                      <div className="dot"></div><div className="dot"></div><div className="dot"></div>
                    </div>
                  )}
                  <div ref={responseEndRef} />
                </div>
              </div>
            </LiquidGlass>
          </div>
        )}

        {/* SCROLL TO BOTTOM BUTTON */}
        {shouldShowPrimarySurface && mode !== 'task' && showScrollButton && (
          <button className="scroll-to-bottom" onClick={scrollToBottom}>
            <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
              <path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6 1.41-1.41z" />
            </svg>
          </button>
        )}

        {/* INPUT BAR / LAUNCHER */}
        {mode !== 'meeting' && <div
          ref={composerSurfaceRef}
          className={`cosmic ${searchState === 'visible' ? 'visible' : searchState === 'hiding' ? 'hiding' : ''} ${showLauncherTray ? 'launchpad-open' : ''} ${composerLaunchClass}`}
          style={composerLaunchStyle}
        >
          <LiquidGlass cornerRadius={24} style={{ width: '100%', height: '100%' }}>
            <div className="glass-content">
              {showLauncherTray ? (
                <div className="launchpad-row" role="toolbar" aria-label="COSMIC modes">
                  {LAUNCHPAD_TILES.map((tile) => (
                    <button
                      key={tile.id}
                      className={`launchpad-tile ${tile.id} ${tile.locked ? 'locked' : ''}`}
                      onClick={(event) => handleLauncherTileClick(tile.id, event)}
                      onMouseEnter={(event) => showHoverTooltipForElement(tile.locked ? `${tile.label} locked` : tile.label, event.currentTarget, 'launcher')}
                      onMouseLeave={hideHoverTooltip}
                      onFocus={(event) => showHoverTooltipForElement(tile.locked ? `${tile.label} locked` : tile.label, event.currentTarget, 'launcher')}
                      onBlur={hideHoverTooltip}
                      type="button"
                      disabled={tile.locked}
                      aria-label={tile.locked ? `${tile.label} locked` : tile.label}
                    >
                      {tile.id === 'task' && pendingTaskCount > 0 && (
                        <span className="launchpad-badge">{pendingTaskCount}</span>
                      )}
                      <div className="launchpad-icon-shell">
                        <LaunchpadIcon tile={tile.id} />
                      </div>
                      <span className="launchpad-label">{tile.label}</span>
                    </button>
                  ))}
                </div>
              ) : (
                mode === 'task' ? (
                  <div className="task-toolbar">
                    <button
                      className="back-btn"
                      onClick={handleShowLauncherTray}
                      onMouseEnter={(event) => showHoverTooltipForElement('Modes', event.currentTarget, 'control')}
                      onMouseLeave={hideHoverTooltip}
                      onFocus={(event) => showHoverTooltipForElement('Modes', event.currentTarget, 'control')}
                      onBlur={hideHoverTooltip}
                      aria-label="Modes"
                      style={{ marginRight: 8 }}
                      type="button"
                    >
                      <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M14.71 6.71a1 1 0 0 1 0 1.41L10.83 12l3.88 3.88a1 1 0 0 1-1.41 1.41l-4.59-4.59a1 1 0 0 1 0-1.41l4.59-4.59a1 1 0 0 1 1.41 0z" />
                      </svg>
                    </button>
                    <div className="task-toolbar-copy">
                      <div className="task-toolbar-title">Task Inbox</div>
                      <div className="task-toolbar-subtitle">
                        {pendingTaskCount > 0 ? `${pendingTaskCount} waiting for your input` : 'No task is waiting right now'}
                      </div>
                    </div>
                    <div className="task-mode-pill" aria-label="Task mode uses Opus">
                      <span className="task-mode-pill-kicker">Task</span>
                      <span className="task-mode-pill-model">OPUS</span>
                    </div>
                    <button
                      className="task-toolbar-chat-btn"
                      onClick={showChatComposer}
                      type="button"
                    >
                      Open chat
                    </button>
                  </div>
                ) : (
                  <div className="input-row">
                    <button
                      className="back-btn"
                      onClick={handleShowLauncherTray}
                      onMouseEnter={(event) => showHoverTooltipForElement('Modes', event.currentTarget, 'control')}
                      onMouseLeave={hideHoverTooltip}
                      onFocus={(event) => showHoverTooltipForElement('Modes', event.currentTarget, 'control')}
                      onBlur={hideHoverTooltip}
                      aria-label="Modes"
                      style={{ marginRight: 8 }}
                      type="button"
                    >
                      {pendingTaskCount > 0 && <span className="back-btn-badge">{pendingTaskCount}</span>}
                      <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M14.71 6.71a1 1 0 0 1 0 1.41L10.83 12l3.88 3.88a1 1 0 0 1-1.41 1.41l-4.59-4.59a1 1 0 0 1 0-1.41l4.59-4.59a1 1 0 0 1 1.41 0z" />
                      </svg>
                    </button>

                    <textarea
                      ref={inputRef}
                      className="input"
                      rows={1}
                      value={query}
                      onChange={handleInput}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && !e.shiftKey) {
                          e.preventDefault()
                          handleSubmit()
                        }
                      }}
                      onFocus={() => setIsInputFocused(true)}
                      onBlur={() => setIsInputFocused(false)}
                      placeholder={gatewayStatus.connected ? "Ask Cosmic..." : "Connecting to your VM..."}
                      spellCheck={false}
                      autoComplete="off"
                      disabled={isStreaming || authState !== 'authenticated'}
                    />

                    {query && (
                      <button
                        className="clear-btn"
                        onClick={() => {
                          setQuery('')
                          if (inputRef.current) {
                            inputRef.current.style.height = '24px'
                            inputRef.current.focus()
                          }
                        }}
                        type="button"
                      >
                        ✕
                      </button>
                    )}

                    <div className="model-dial" role="tablist" aria-label="Model selection">
                      <div className="model-dial-window">
                        <div
                          className="model-dial-viewport"
                          ref={modelDialRef}
                          onScroll={handleModelDialScroll}
                          onWheel={handleModelDialWheel}
                        >
                          {MODEL_OPTIONS.map((item) => (
                            <button
                              key={item.id}
                              data-model={item.id}
                              className={`model-dial-btn ${selectedModel === item.id ? 'active' : ''} ${modelPulseModel === item.id ? 'pulse' : ''}`}
                              onClick={() => handleModelDialFocus(item.id)}
                              onMouseEnter={(event) => showHoverTooltipForElement(item.label, event.currentTarget, 'model')}
                              onMouseLeave={hideHoverTooltip}
                              onFocus={(event) => {
                                handleModelDialFocus(item.id)
                                showHoverTooltipForElement(item.label, event.currentTarget, 'model')
                              }}
                              onBlur={hideHoverTooltip}
                              type="button"
                              aria-label={item.label}
                              aria-selected={selectedModel === item.id}
                            >
                              <span className="model-dial-knob">
                                <ModelDialIcon model={item.id} />
                              </span>
                            </button>
                          ))}
                        </div>
                      </div>
                    </div>

                    {isStreaming ? (
                      <button
                        className="stream-stop-btn"
                        onClick={handleStopStreaming}
                        onMouseEnter={(event) => showHoverTooltipForElement('Stop response', event.currentTarget, 'control')}
                        onMouseLeave={hideHoverTooltip}
                        onFocus={(event) => showHoverTooltipForElement('Stop response', event.currentTarget, 'control')}
                        onBlur={hideHoverTooltip}
                        type="button"
                        aria-label="Stop response"
                      >
                        <LiquidGlassLoader />
                      </button>
                    ) : (
                      <button
                        className={`send-btn ${query.trim() ? 'active' : ''}`}
                        onClick={handleSubmit}
                        disabled={!query.trim()}
                        type="button"
                      >
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
                          <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z" />
                        </svg>
                      </button>
                    )}
                  </div>
                )
              )}
            </div>
          </LiquidGlass>
        </div>}
      </div>
      {hoverTooltip && createPortal(
        <div
          className="cosmic-hover-tooltip"
          style={{
            left: `${hoverTooltip.x}px`,
            top: `${hoverTooltip.y}px`,
          }}
          role="tooltip"
        >
          {hoverTooltip.label}
        </div>,
        document.body,
      )}
    </>
  )
}

function LaunchpadIcon({ tile }: { tile: LauncherTileId }) {
  if (tile === 'chat') {
    return (
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <path d="M6.25 6.75h11.5A1.75 1.75 0 0 1 19.5 8.5v6a1.75 1.75 0 0 1-1.75 1.75H11l-3.9 3.08A.65.65 0 0 1 6 18.82v-2.57A1.75 1.75 0 0 1 4.5 14.5v-6A1.75 1.75 0 0 1 6.25 6.75Z" />
        <path d="M9 11.5h6" />
        <path d="M9 14h3.5" />
      </svg>
    )
  }
  if (tile === 'meeting') {
    return (
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <rect x="4.75" y="7.25" width="11.5" height="9.5" rx="2.1" />
        <path d="M16.5 10.25 20 8.5v7l-3.5-1.75" />
        <circle cx="9.75" cy="12" r="2" />
      </svg>
    )
  }
  if (tile === 'task') {
    return (
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <rect x="6" y="4.75" width="12" height="14.5" rx="2.3" />
        <path d="M9 4.75h6" />
        <path d="m9.4 11.8 1.7 1.7 3.5-3.5" />
        <path d="M9 16.4h6" />
      </svg>
    )
  }
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <rect x="5.25" y="5.25" width="5.5" height="5.5" rx="1.4" />
      <rect x="13.25" y="5.25" width="5.5" height="5.5" rx="1.4" />
      <rect x="5.25" y="13.25" width="5.5" height="5.5" rx="1.4" />
      <path d="M16 13.75v3.5" />
      <path d="M14.25 15.5H17.75" />
    </svg>
  )
}

function ModelDialIcon({ model }: { model: GatewayModelSelection }) {
  const label = model === 'perplexity' ? 'P' : model === 'cosmic' ? 'C' : model === 'haiku' ? 'H' : 'O'
  return <span className="model-dial-glyph">{label}</span>
}

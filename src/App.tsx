import { useEffect, useRef, useState } from 'react'
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
import './spotlight.css'

export type SearchPosition = 'bottom' | 'middle'
export type QueryMode = 'chat' | 'meeting'
export type GatewayModelSelection = 'cosmic' | 'haiku' | 'opus' | 'perplexity'

interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  thinking?: string
  sources?: Array<{ url: string; title?: string; domain?: string } | string>
  stopped?: boolean
}

interface GatewayStatus {
  state: 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'error'
  connected: boolean
  detail?: string
  sessionId?: string | null
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
    }))
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

export default function App() {
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const responseEndRef = useRef<HTMLDivElement>(null)
  const responseContainerRef = useRef<HTMLDivElement>(null)
  const activeAssistantMessageByRequestRef = useRef<Map<string, string>>(new Map())
  const activeAssistantMessageByTaskRef = useRef<Map<string, string>>(new Map())
  const streamedResponseRequestIdsRef = useRef<Set<string>>(new Set())
  const streamedResponseTaskIdsRef = useRef<Set<string>>(new Set())
  const activeStreamingRequestIdRef = useRef<string | null>(null)
  const activeStreamingTaskIdRef = useRef<string | null>(null)
  const messagesRef = useRef<Message[]>([])
  const activeSessionIdRef = useRef<string | null>(null)
  const shouldAutoScrollRef = useRef(true)

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

  // --- CHAT STATE ---
  const [messages, setMessages] = useState<Message[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [showScrollButton, setShowScrollButton] = useState(false)

  // --- AUTH STATE ---
  const [authState, setAuthState] = useState<'loading' | 'unauthenticated' | 'authenticated'>('loading')
  const [authData, setAuthData] = useState<any>(null)
  const [gatewayStatus, setGatewayStatus] = useState<GatewayStatus>({ state: 'idle', connected: false })

  // --- HISTORY / DB STATE ---
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)

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

  const showChatComposer = () => {
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

  const showMeetingSurface = () => {
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
        setSelectedModel(normalizeGatewayModelSelection(settings['gatewayModelSelection']))
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
    setSearchState('hiding')
    setIsInputFocused(false)
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

  // --- DATA LISTENERS ---
  useEffect(() => {
    const offEvent = window.cosmic?.onGatewayEvent((event) => {
      const eventType = String(event?.type || '')
      if (!eventType) return

      if (eventType === 'resume.ok') {
        setActiveSessionId(typeof event.session_id === 'string' ? event.session_id : null)
        resetInFlightAssistantMaps()
        setMessages(historyToMessages(event.history_tail))
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
    if (!isStreaming && searchState === 'visible' && !showLauncherTray && mode !== 'meeting') {
      setTimeout(() => inputRef.current?.focus(), 10)
    }
  }, [isStreaming, mode, searchState, showLauncherTray])

  useEffect(() => {
    if (authState !== 'authenticated') {
      resetInFlightAssistantMaps()
      setMessages([])
      setActiveSessionId(null)
      setGatewayStatus({ state: 'idle', connected: false })
      setShowLauncherTray(false)
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

    window.cosmic.sendGatewayQuery({
      requestId,
      content: textToSend,
      conversationContext: buildConversationContext([...messages, createUserMessage(textToSend)]),
      routeOverride: selectedModel === 'cosmic' ? undefined : selectedModel,
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
    setShowLauncherTray(true)
    setIsInputFocused(false)
    if (inputRef.current) {
      inputRef.current.blur()
    }
  }

  const handleLauncherTileClick = (tile: 'chat' | 'meeting' | 'task' | 'spaces') => {
    if (tile === 'spaces') {
      return
    }
    if (tile === 'meeting') {
      showMeetingSurface()
      return
    }
    showChatComposer()
  }

  const handleModelSelection = (model: GatewayModelSelection) => {
    setSelectedModel(model)
    window.cosmic?.saveSetting('gatewayModelSelection', model)
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

  // Render Classes
  const isChatSurfaceVisible = mode !== 'meeting' && !showLauncherTray
  const effectivePosition = messages.length > 0 ? 'bottom' : searchPosition
  const overlayClass = [
    searchState === 'hidden' ? '' : 'visible',
    effectivePosition === 'middle' ? 'position-middle' : '',
    isChatSurfaceVisible && messages.length > 0 ? 'has-response' : '',
    (isInputFocused || (isChatSurfaceVisible && messages.length > 0) || isStreaming || mode === 'meeting') ? 'focused' : ''
  ].join(' ')

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
        onLogout={() => {
          window.cosmic?.logout()
          setAuthState('unauthenticated')
          setAuthData(null)
        }}
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
        style={{ pointerEvents: searchState === 'visible' ? 'auto' : 'none' }}
      >
        <MeetingMode
          active={mode === 'meeting'}
          keyStatus={keyStatus}
          onBackToChat={showChatComposer}
        />

        {/* MESSAGES AREA */}
        {isChatSurfaceVisible && messages.length > 0 && (
          <div className={`response-container ${searchState === 'visible' ? 'visible' : ''}`}>
            <LiquidGlass disableTilt={true} cornerRadius={32} style={{ width: '100%', height: '100%' }}>
              <div className="response-wrapper">
                <div className="response-content" style={{ paddingTop: 24 }} ref={responseContainerRef} onScroll={handleScroll}>

                  {/* SOURCES GRID */}


                  {/* MESSAGES */}
                  {messages.map((msg, idx) => (
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
                  ))}

                  {isStreaming && (
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
        {isChatSurfaceVisible && showScrollButton && (
          <button className="scroll-to-bottom" onClick={scrollToBottom}>
            <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
              <path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6 1.41-1.41z" />
            </svg>
          </button>
        )}

        {/* INPUT BAR / LAUNCHER */}
        {mode !== 'meeting' && <div className={`cosmic ${searchState === 'visible' ? 'visible' : searchState === 'hiding' ? 'hiding' : ''} ${showLauncherTray ? 'launchpad-open' : ''}`}>
          <LiquidGlass cornerRadius={24} style={{ width: '100%', height: '100%' }}>
            <div className="glass-content">
              {showLauncherTray ? (
                <div className="launchpad-row" role="toolbar" aria-label="COSMIC modes">
                  {[
                    { id: 'chat', label: 'Chat', locked: false },
                    { id: 'meeting', label: 'Meeting', locked: false },
                    { id: 'task', label: 'Task', locked: false },
                    { id: 'spaces', label: 'Spaces', locked: true },
                  ].map((tile) => (
                    <button
                      key={tile.id}
                      className={`launchpad-tile ${tile.locked ? 'locked' : ''}`}
                      onClick={() => handleLauncherTileClick(tile.id as 'chat' | 'meeting' | 'task' | 'spaces')}
                      type="button"
                      disabled={tile.locked}
                      title={tile.locked ? `${tile.label} (Locked)` : tile.label}
                    >
                      <div className="launchpad-icon-shell">
                        <LaunchpadIcon tile={tile.id as 'chat' | 'meeting' | 'task' | 'spaces'} />
                      </div>
                      <span className="launchpad-label">{tile.label}</span>
                    </button>
                  ))}
                </div>
              ) : (
                <div className="input-row">
                  <button
                    className="back-btn"
                    onClick={handleShowLauncherTray}
                    title="Modes"
                    style={{ marginRight: 8 }}
                    type="button"
                  >
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
                    {([
                      { id: 'cosmic', label: 'Cosmic' },
                      { id: 'haiku', label: 'Haiku' },
                      { id: 'opus', label: 'Opus' },
                      { id: 'perplexity', label: 'PPLX' },
                    ] as const).map((item) => (
                      <button
                        key={item.id}
                        className={`model-dial-btn ${selectedModel === item.id ? 'active' : ''}`}
                        onClick={() => handleModelSelection(item.id)}
                        type="button"
                        title={item.label}
                      >
                        <span className="model-dial-knob">
                          <ModelDialIcon model={item.id} />
                        </span>
                        <span className="model-dial-label">{item.label}</span>
                      </button>
                    ))}
                  </div>

                  {isStreaming ? (
                    <button
                      className="stream-stop-btn"
                      onClick={handleStopStreaming}
                      type="button"
                      title="Stop response"
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
              )}
            </div>
          </LiquidGlass>
        </div>}
      </div>
    </>
  )
}

function LaunchpadIcon({ tile }: { tile: 'chat' | 'meeting' | 'task' | 'spaces' }) {
  if (tile === 'chat') {
    return (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
        <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v7A2.5 2.5 0 0 1 17.5 15H9.8l-4.22 3.52A1 1 0 0 1 4 17.75V15.1A2.5 2.5 0 0 1 2 12.5v-7A2.5 2.5 0 0 1 4.5 3H5a1 1 0 1 1 0 2h-.5A.5.5 0 0 0 4 5.5Z" />
      </svg>
    )
  }
  if (tile === 'meeting') {
    return (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
        <path d="M15 8a3 3 0 1 1-6 0a3 3 0 0 1 6 0Zm-8.5 9.5c0-2.5 2.94-4.5 6.5-4.5s6.5 2 6.5 4.5V19H6.5zM19 8.75l3-1.5v9.5l-3-1.5z" />
      </svg>
    )
  }
  if (tile === 'task') {
    return (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
        <path d="M8 4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2h2.5A2.5 2.5 0 0 1 21 6.5v13A2.5 2.5 0 0 1 18.5 22h-13A2.5 2.5 0 0 1 3 19.5v-13A2.5 2.5 0 0 1 5.5 4Zm2 0v1h4V4Zm6 7H8v2h8Zm-3 4H8v2h5Z" />
      </svg>
    )
  }
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
      <path d="M7 10V8a5 5 0 1 1 10 0v2h1a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2Zm2 0h6V8a3 3 0 1 0-6 0Z" />
    </svg>
  )
}

function ModelDialIcon({ model }: { model: GatewayModelSelection }) {
  const label = model === 'perplexity' ? 'P' : model === 'cosmic' ? 'C' : model === 'haiku' ? 'H' : 'O'
  return <span className="model-dial-glyph">{label}</span>
}

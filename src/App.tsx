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

interface Message {
  role: 'user' | 'assistant'
  content: string
  thinking?: string
  sources?: Array<{ url: string; title?: string; domain?: string } | string>
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
    .map((item) => ({
      role: item.role,
      content: String(item.content || ''),
      sources: Array.isArray(item?.metadata?.sources) ? item.metadata.sources : undefined,
    }))
}

const buildConversationContext = (messages: Message[]) => {
  return messages.slice(-10).map((message) => ({
    role: message.role,
    content: message.content,
  }))
}

export default function App() {
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const responseEndRef = useRef<HTMLDivElement>(null)
  const responseContainerRef = useRef<HTMLDivElement>(null)

  const [query, setQuery] = useState('')
  const [searchState, setSearchState] = useState<'hidden' | 'visible' | 'hiding'>('hidden')
  const [isIslandHovered, setIsIslandHovered] = useState(false)
  const [searchPosition, setSearchPosition] = useState<SearchPosition>('bottom')
  const [staybackTime, setStaybackTime] = useState(0)
  const [islandOpacity, setIslandOpacity] = useState(0.85) // Default opacity

  const [mode, setMode] = useState<QueryMode>('chat')
  const modeRef = useRef<QueryMode>('chat')
  const [showModeDropdown, setShowModeDropdown] = useState(false)
  const [isInputFocused, setIsInputFocused] = useState(false)

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
  const [showHistory, setShowHistory] = useState(false)
  const [sessions, setSessions] = useState<any[]>([])
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)

  // Track key status for SetupModal
  const [keyStatus, setKeyStatus] = useState({
    gemini: false,
    perplexity: false,
    deepgram: false,
    groq: false,
    anthropic: false,
  })

  useEffect(() => {
    modeRef.current = mode
  }, [mode])

  // --- INIT & MOUSE EVENTS ---
  useEffect(() => {
    const unsubKeys = window.cosmic?.onKeyStatus((status) => {
      setKeyStatus({
        gemini: !!status.gemini,
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
      const modeDropdown = !!el.closest('.mode-dropdown')

      const isInteractive = island || settings || overlay || modeDropdown || showHistory

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
  }, [searchState, showHistory])

  // --- VISIBILITY HANDLERS ---
  const performHide = () => {
    setSearchState('hiding')
    setIsInputFocused(false)
    setTimeout(() => {
      setSearchState('hidden')
      setShowModeDropdown(false)
      setShowHistory(false)
      if (modeRef.current === 'meeting') setMode('chat')
    }, 250)
  }

  useEffect(() => {
    const handleShown = () => {
      setSearchState('visible')
      setIsInputFocused(true)
      if (inputRef.current) {
        inputRef.current.style.height = '24px'
        inputRef.current.focus()
      }
    }

    const handleMeetingInvoke = () => {
      setSearchState('visible')
      setMode('meeting')
      setShowModeDropdown(false)
      setShowHistory(false)
      setIsInputFocused(false)
    }

    const off1 = window.cosmic?.onShown(handleShown)
    const off2 = window.cosmic?.onHiding(performHide)
    const off3 = window.cosmic?.onMeetingInvoke(handleMeetingInvoke)
    const off4 = window.cosmic?.onMeetingToggle(() => {
      if (modeRef.current === 'meeting') {
        window.cosmic?.hide()
        return
      }
      handleMeetingInvoke()
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
        setMessages(historyToMessages(event.history_tail))
        setIsStreaming(false)
        return
      }

      if (eventType === 'response.chunk') {
        setMessages((prev) => {
          const last = prev[prev.length - 1]
          if (!event.content) return prev
          if (!last || last.role === 'user') {
            return [...prev, { role: 'assistant', content: String(event.content) }]
          }
          return [...prev.slice(0, -1), { ...last, content: last.content + String(event.content) }]
        })
        return
      }

      if (eventType === 'response.thinking.chunk') {
        setMessages((prev) => {
          const last = prev[prev.length - 1]
          if (!event.content) return prev
          if (!last || last.role === 'user') {
            return [...prev, { role: 'assistant', content: '', thinking: String(event.content) }]
          }
          return [
            ...prev.slice(0, -1),
            {
              ...last,
              thinking: `${last.thinking || ''}${String(event.content)}`,
            },
          ]
        })
        return
      }

      if (eventType === 'response.complete') {
        setActiveSessionId((prev) => typeof event.session_id === 'string' ? event.session_id : prev)
        setMessages((prev) => {
          const last = prev[prev.length - 1]
          const sources = Array.isArray(event.sources) ? event.sources : undefined
          if (last && last.role === 'assistant') {
            return [...prev.slice(0, -1), {
              ...last,
              content: String(event.content || last.content || ''),
              sources,
            }]
          }
          return [...prev, { role: 'assistant', content: String(event.content || ''), sources }]
        })
        setIsStreaming(false)
        return
      }

      if (eventType === 'task.failed') {
        setIsStreaming(false)
        const message = String(event?.error?.message || event?.message || 'Opus task failed.')
        setMessages((prev) => {
          const last = prev[prev.length - 1]
          if (last && last.role === 'assistant' && !last.content) {
            return [...prev.slice(0, -1), { ...last, content: message }]
          }
          return [...prev, { role: 'assistant', content: message }]
        })
        return
      }

      if (eventType === 'error') {
        setIsStreaming(false)
        if (event.message) {
          setMessages((prev) => [...prev, { role: 'assistant', content: String(event.message) }])
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
      }
    })

    return () => { offEvent?.(); offStatus?.() }
  }, [])

  useEffect(() => {
    if (!isStreaming && searchState === 'visible') {
      setTimeout(() => inputRef.current?.focus(), 10)
    }
  }, [isStreaming, searchState])

  useEffect(() => {
    if (authState !== 'authenticated') {
      setMessages([])
      setSessions([])
      setActiveSessionId(null)
      setGatewayStatus({ state: 'idle', connected: false })
      return
    }

    if (window.cosmic?.requestGatewayResume) {
      window.cosmic.requestGatewayResume().catch(() => { })
    }
    if (window.cosmic?.listGatewaySessions) {
      window.cosmic.listGatewaySessions()
        .then((payload) => {
          setSessions(Array.isArray(payload?.sessions) ? payload.sessions : [])
        })
        .catch(() => { })
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

    // 1. Add User Message (Optimistic)
    setMessages(prev => [...prev, { role: 'user', content: textToSend }])
    if (authState !== 'authenticated') {
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: "Please sign in to connect this desktop app to your VM."
      }])
      return
    }

    if (!gatewayStatus.connected) {
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: gatewayStatus.detail || "The desktop app is not connected to your VM yet."
      }])
      return
    }

    setIsStreaming(true)
    if (!window.cosmic?.sendGatewayQuery) {
      setIsStreaming(false)
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: "Gateway chat support is unavailable in this desktop build."
      }])
      return
    }

    window.cosmic.sendGatewayQuery({
      content: textToSend,
      conversationContext: buildConversationContext([...messages, { role: 'user', content: textToSend }]),
    }).catch((error: any) => {
      setIsStreaming(false)
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: error?.message || "Unable to send the message to your VM."
      }])
    })
  }

  const handleHistoryToggle = () => {
    if (!showHistory) {
      if (window.cosmic?.listGatewaySessions) {
        window.cosmic.listGatewaySessions()
          .then((payload) => {
            setSessions(Array.isArray(payload?.sessions) ? payload.sessions : [])
          })
          .catch(() => {
            setSessions([])
          })
      } else {
        setSessions([])
      }
      setShowHistory(true)
    } else {
      setShowHistory(false)
      setTimeout(() => inputRef.current?.focus(), 50)
    }
  }

  const handleSelectSession = async (id: string) => {
    setActiveSessionId(id)
    try {
      const payload = window.cosmic?.getGatewaySessionHistory
        ? await window.cosmic.getGatewaySessionHistory(id)
        : null
      setMessages(historyToMessages(payload?.messages))
    } catch {
      setMessages([])
    }
    setShowHistory(false)
    setSearchState('visible')
    setTimeout(() => inputRef.current?.focus(), 50)
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
    console.log('🔍 Scroll Debug:', { scrollTop, scrollHeight, clientHeight, distanceFromBottom })
    const isNearBottom = distanceFromBottom < 50
    setShowScrollButton(!isNearBottom && messages.length > 1)
  }

  useEffect(() => {
    responseEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages.length])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (showModeDropdown) setShowModeDropdown(false)
        else if (showHistory) setShowHistory(false)
        else if (searchState === 'visible') {
          window.cosmic?.hide()
        }
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [searchState, showModeDropdown, showHistory])

  // Render Classes
  const effectivePosition = messages.length > 0 ? 'bottom' : searchPosition
  const overlayClass = [
    searchState === 'hidden' ? '' : 'visible',
    effectivePosition === 'middle' ? 'position-middle' : '',
    messages.length > 0 ? 'has-response' : '',
    (isInputFocused || messages.length > 0 || isStreaming || mode === 'meeting') ? 'focused' : ''
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
          onBackToChat={() => setMode('chat')}
        />

        {/* MESSAGES AREA */}
        {mode !== 'meeting' && messages.length > 0 && (
          <div className={`response-container ${searchState === 'visible' ? 'visible' : ''}`}>
            <LiquidGlass disableTilt={true} cornerRadius={32} style={{ width: '100%', height: '100%' }}>
              <div className="response-wrapper">
                <div className="response-content" style={{ paddingTop: 24 }} ref={responseContainerRef} onScroll={handleScroll}>

                  {/* SOURCES GRID */}


                  {/* MESSAGES */}
                  {messages.map((msg, idx) => (
                    <div key={idx} className={`message-row ${msg.role}`} style={{ marginBottom: 24, display: 'flex', flexDirection: 'column', alignItems: msg.role === 'user' ? 'flex-end' : 'flex-start' }}>

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
        {mode !== 'meeting' && showScrollButton && (
          <button className="scroll-to-bottom" onClick={scrollToBottom}>
            <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
              <path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6 1.41-1.41z" />
            </svg>
          </button>
        )}

        {/* INPUT BAR / HISTORY CONTAINER */}
        {mode !== 'meeting' && <div className={`cosmic ${searchState === 'visible' ? 'visible' : searchState === 'hiding' ? 'hiding' : ''} ${showHistory ? 'history-open' : ''}`}>
          <LiquidGlass cornerRadius={24} style={{ width: '100%', height: '100%' }}>
            <div className="glass-content">

              {showHistory ? (
                /* --- EXPANDED HISTORY VIEW --- */
                <div className="history-container">
                  <div className="history-header">
                    <span className="history-title">Chat History</span>
                    <button className="clear-btn" onClick={() => setShowHistory(false)}>✕</button>
                  </div>
                  <div className="history-list">
                    {sessions.length === 0 ? (
                      <div style={{ padding: 20, color: 'rgba(255,255,255,0.4)', textAlign: 'center' }}>No history found</div>
                    ) : sessions.map(session => (
                      <div key={session.id} className={`history-item-row ${activeSessionId === session.id ? 'active' : ''}`} onClick={() => handleSelectSession(session.id)}>
                        <div className="history-info">
                          <div className="history-name">{cleanText(session.title || "Untitled Chat")}</div>
                          <div className="history-time">{new Date(session.updated_at || session.created_at).toLocaleString()}</div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ) : (
                /* --- STANDARD INPUT VIEW --- */
                <div className="input-row">
                  <button
                    className={`history-btn ${showHistory ? 'active' : ''}`}
                    onClick={handleHistoryToggle}
                    title="History"
                    style={{ marginRight: 8 }}
                  >
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                      <path d="M13 3a9 9 0 0 0-9 9H1l3.89 3.89.07.14L9 12H6c0-3.87 3.13-7 7-7s7 3.13 7 7-3.13 7-7 7c-1.93 0-3.68-.79-4.94-2.06l-1.42 1.42A8.954 8.954 0 0 0 13 21a9 9 0 0 0 0-18zm-1 5v5l4.28 2.54.72-1.21-3.5-2.08V8H12z" />
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

                  <div className="mode-selector">
                    <button
                      className="mode-btn"
                      onClick={() => setShowModeDropdown(!showModeDropdown)}
                      type="button"
                    >
                      <ModeIcon mode={mode} />
                      <span className="mode-label">
                        {mode === 'chat' ? 'Cosmic' : 'Meeting'}
                      </span>
                      <svg className="chevron" width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M7 10l5 5 5-5z" />
                      </svg>
                    </button>

                    {showModeDropdown && (
                      <div className="mode-dropdown">
                      <div className="mode-options">
                        <button
                            className={`mode-option ${mode === 'chat' ? 'active' : ''}`}
                            onClick={() => { setMode('chat'); setShowModeDropdown(false); }}
                            type="button"
                          >
                            <ModeIcon mode="chat" />
                            <div className="mode-text">
                              <span>Cosmic Chat</span>
                              <span className="mode-desc">Gateway-routed</span>
                            </div>
                          </button>
                          <button
                            className={`mode-option ${modeRef.current === 'meeting' ? 'active' : ''}`}
                            onClick={() => { setMode('meeting'); setShowModeDropdown(false); }}
                            type="button"
                          >
                            <ModeIcon mode="meeting" />
                            <div className="mode-text">
                              <span>Meeting</span>
                              <span className="mode-desc">Live copilot</span>
                            </div>
                          </button>
                        </div>
                      </div>
                    )}
                  </div>

                  {isStreaming ? (
                    <LiquidGlassLoader />
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

              {/* Footer (Hide in History Mode) */}

            </div>
          </LiquidGlass>
        </div>}
      </div>
    </>
  )
}

function ModeIcon({ mode }: { mode: QueryMode }) {
  if (mode === 'chat') {
    return (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
        <path d="M9 2L7.17 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2h-3.17L15 2H9zm3 15c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5z" />
      </svg>
    )
  }
  if (mode === 'meeting') {
    return (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
        <path d="M17 10.5V7c0-2.76-2.24-5-5-5S7 4.24 7 7v3.5C5.21 11.41 4 13.11 4 15c0 2.76 3.58 5 8 5s8-2.24 8-5c0-1.89-1.21-3.59-3-4.5zM9 7c0-1.65 1.35-3 3-3s3 1.35 3 3v2.74c-.94-.47-1.99-.74-3-.74s-2.06.27-3 .74V7zm3 11c-3.31 0-6-1.34-6-3 0-1.66 2.69-3 6-3s6 1.34 6 3c0 1.66-2.69 3-6 3z" />
      </svg>
    )
  }
  return null
}

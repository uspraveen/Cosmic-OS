import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'
import LiquidGlass from './LiquidGlass'
import DynamicIsland from './DynamicIsland'
import SetupModal from './SetupModal'
import LiquidGlassLoader from './LiquidGlassLoader'
import './spotlight.css'

export type SearchPosition = 'bottom' | 'middle'
export type QueryMode = 'perplexity' | 'llm'



interface Message {
  role: 'user' | 'assistant'
  content: string
  sources?: string[]
}

// Helper to strip "PROMPT:" from legacy database entries
const cleanText = (text: string) => {
  if (!text) return ""
  return text.replace(/^PROMPT:/, '')
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

  const [mode, setMode] = useState<QueryMode>('llm')
  const [showModeDropdown, setShowModeDropdown] = useState(false)
  const [isInputFocused, setIsInputFocused] = useState(false)

  // --- CHAT STATE ---
  const [messages, setMessages] = useState<Message[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [showScrollButton, setShowScrollButton] = useState(false)

  // --- HISTORY / DB STATE ---
  const [isFirstRun, setIsFirstRun] = useState(false)
  const [showHistory, setShowHistory] = useState(false)
  const [sessions, setSessions] = useState<any[]>([])
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)

  // Track key status for SetupModal
  const [keyStatus, setKeyStatus] = useState({ gemini: false, perplexity: false })

  // --- INIT & MOUSE EVENTS ---
  useEffect(() => {
    // Check Keys on Load
    const unsubKeys = window.cosmic?.onKeyStatus((status) => {
      setKeyStatus({ gemini: status.gemini, perplexity: status.perplexity })
      if (!status.hasKeys) {
        setIsFirstRun(true)
        setSearchState('visible')
      }
    })
    window.cosmic?.sendToGemini("CHECK_KEYS")

    // Load Settings
    window.cosmic?.getSettings()
    const unsubSettings = window.cosmic?.onSettingsUpdate((settings) => {
      console.log("App: Loaded settings", settings)
      if (settings['searchPosition']) setSearchPosition(settings['searchPosition'])
      if (settings['staybackTime']) setStaybackTime(parseInt(settings['staybackTime']))
      if (settings['islandOpacity']) setIslandOpacity(parseFloat(settings['islandOpacity']))
    })

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

    const off1 = window.cosmic?.onShown(handleShown)
    const off2 = window.cosmic?.onHiding(performHide)

    return () => { off1?.(); off2?.() }
  }, [])

  // --- DATA LISTENERS ---
  useEffect(() => {
    const u1 = window.cosmic?.onSessionList((list) => setSessions(list))
    const u2 = window.cosmic?.onHistoryLoad((data) => {
      const uiMsgs = data.map((m: any) => ({ role: m.role, content: m.content }))
      setMessages(uiMsgs)
    })

    const u3 = window.cosmic?.onGeminiChunk((data) => {
      if (mode !== 'llm') return
      setMessages(prev => {
        const last = prev[prev.length - 1]
        if (data.chunk && (!last || last.role === 'user')) {
          return [...prev, { role: 'assistant', content: data.chunk }]
        }
        if (last && last.role === 'assistant') {
          return [...prev.slice(0, -1), { ...last, content: last.content + data.chunk }]
        }
        return prev
      })
      if (data.done) setIsStreaming(false)
    })

    const u4 = window.cosmic?.onPerplexityChunk((data) => {
      if (mode !== 'perplexity') return
      setMessages(prev => {
        const last = prev[prev.length - 1]
        if (data.chunk && (!last || last.role === 'user')) {
          return [...prev, { role: 'assistant', content: data.chunk }]
        }
        if (last && last.role === 'assistant') {
          return [...prev.slice(0, -1), { ...last, content: last.content + data.chunk }]
        }
        return prev
      })
      if (data.done) setIsStreaming(false)
    })

    const u5 = window.cosmic?.onPerplexitySources((data) => {
      if (mode !== 'perplexity') return
      console.log("App: Received sources", data)
      setMessages(prev => {
        const last = prev[prev.length - 1]
        // If the last message is already an assistant message (streaming or empty), attach sources to it
        if (last && last.role === 'assistant') {
          return [...prev.slice(0, -1), { ...last, sources: data }]
        }
        // Otherwise, start a new assistant message with these sources
        return [...prev, { role: 'assistant', content: '', sources: data }]
      })
    })

    const u6 = window.cosmic?.onSessionSet((id) => {
      console.log("Session Synced:", id)
      if (activeSessionId !== id) {
        setActiveSessionId(id)
        // Broadcast to the OTHER model (or all)
        // If a bridge set the session, we ensure others load it too
        window.cosmic?.sendToGemini(`LOAD_SESSION:${id}`)
        window.cosmic?.sendToPerplexity(`LOAD_SESSION:${id}`)
      }
    })

    const u7 = window.cosmic?.onHistoryLoad((data) => {
      console.log("🔍 Session loaded with messages:", data)
      console.log("🔍 Number of messages:", data?.length)
      console.log("🔍 First message:", data?.[0])
      setMessages(data)
    })

    return () => { u1?.(); u2?.(); u3?.(); u4?.(); u5?.(); u6?.(); u7?.() }
  }, [mode])

  useEffect(() => {
    if (!isStreaming && searchState === 'visible') {
      setTimeout(() => inputRef.current?.focus(), 10)
    }
  }, [isStreaming, searchState])

  // --- ACTIONS ---
  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setQuery(e.target.value)
    const target = e.target
    target.style.height = 'auto'
    const newHeight = Math.min(target.scrollHeight, 120)
    target.style.height = `${newHeight}px`
  }

  const handleSubmit = () => {
    setIsInputFocused(false)
    if (inputRef.current) inputRef.current.blur()

    if (!query.trim() || isStreaming) return

    const textToSend = query
    setQuery('')
    if (inputRef.current) inputRef.current.style.height = '24px'

    // 1. Add User Message (Optimistic)
    setMessages(prev => [...prev, { role: 'user', content: textToSend }])

    // --- KEY VALIDATION ---
    if (mode === 'perplexity' && !keyStatus.perplexity) {
      setTimeout(() => {
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: "**Perplexity API Key Missing**\n\nPlease open Settings (click the island) and configure your API key to use this search mode."
        }])
      }, 100)
      return
    }

    if (mode === 'llm' && !keyStatus.gemini) {
      setTimeout(() => {
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: "**Gemini API Key Missing**\n\nPlease open Settings (click the island) and configure your API key to use the chat."
        }])
      }, 100)
      return
    }

    setIsStreaming(true)

    // FIX: Send raw text. Main process adds the PROMPT: protocol prefix.
    if (mode === 'llm') {
      window.cosmic?.sendToGemini(textToSend)
    } else {
      window.cosmic?.sendToPerplexity(textToSend)
    }
  }

  const handleHistoryToggle = () => {
    if (!showHistory) {
      window.cosmic?.sendToGemini("LIST_SESSIONS")
      setShowHistory(true)
    } else {
      setShowHistory(false)
      setTimeout(() => inputRef.current?.focus(), 50)
    }
  }

  const handleSelectSession = (id: string) => {
    setActiveSessionId(id)
    window.cosmic?.sendToGemini(`LOAD_SESSION:${id}`)
    window.cosmic?.sendToPerplexity(`LOAD_SESSION:${id}`)
    setShowHistory(false)
    setSearchState('visible')
    setTimeout(() => inputRef.current?.focus(), 50)
  }

  const handleDeleteSession = (e: React.MouseEvent, id: string) => {
    e.stopPropagation()
    window.cosmic?.sendToGemini(`DELETE_SESSION:${id}`)
    window.cosmic?.sendToPerplexity(`DELETE_SESSION:${id}`)
  }

  const handleNewChat = () => {
    setActiveSessionId(null)
    setMessages([])
    setQuery('')

    window.cosmic?.sendToGemini("NEW_CHAT")
    window.cosmic?.sendToPerplexity("NEW_CHAT")
    setShowHistory(false)
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
    (isInputFocused || messages.length > 0 || isStreaming) ? 'focused' : ''
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
      />

      {isFirstRun && (
        <SetupModal
          onComplete={() => {
            setIsFirstRun(false);
            performHide();
            window.cosmic?.hide();
          }}
        />
      )}

      <div
        className={`overlay ${overlayClass}`}
        onMouseDown={(e) => {
          if (e.target === e.currentTarget) window.cosmic?.hide()
        }}
        style={{ pointerEvents: searchState === 'visible' ? 'auto' : 'none' }}
      >
        {/* MESSAGES AREA */}
        {messages.length > 0 && (
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
        {showScrollButton && (
          <button className="scroll-to-bottom" onClick={scrollToBottom}>
            <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
              <path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6 1.41-1.41z" />
            </svg>
          </button>
        )}

        {/* INPUT BAR / HISTORY CONTAINER */}
        <div className={`cosmic ${searchState === 'visible' ? 'visible' : searchState === 'hiding' ? 'hiding' : ''} ${showHistory ? 'history-open' : ''}`}>
          <LiquidGlass cornerRadius={24} style={{ width: '100%', height: '100%' }}>
            <div className="glass-content">

              {showHistory ? (
                /* --- EXPANDED HISTORY VIEW --- */
                <div className="history-container">
                  <div className="history-header">
                    <span className="history-title">Chat History</span>
                    <div style={{ display: 'flex', gap: 8 }}>
                      <button className="mode-btn" onClick={handleNewChat} style={{ padding: '6px 12px' }}>
                        + New Chat
                      </button>
                      <button className="clear-btn" onClick={() => setShowHistory(false)}>✕</button>
                    </div>
                  </div>
                  <div className="history-list">
                    {sessions.length === 0 ? (
                      <div style={{ padding: 20, color: 'rgba(255,255,255,0.4)', textAlign: 'center' }}>No history found</div>
                    ) : sessions.map(session => (
                      <div key={session.id} className="history-item-row" onClick={() => handleSelectSession(session.id)}>
                        <div className="history-info">
                          <div className="history-name">{cleanText(session.title || "Untitled Chat")}</div>
                          <div className="history-time">{new Date(session.created_at * 1000).toLocaleString()}</div>
                        </div>
                        <button className="delete-btn" onClick={(e) => handleDeleteSession(e, session.id)} title="Delete">
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z" /></svg>
                        </button>
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
                    placeholder={mode === 'llm' ? "Ask Gemini..." : "Search Perplexity..."}
                    spellCheck={false}
                    autoComplete="off"
                    disabled={isStreaming}
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
                      <span className="mode-label">{mode === 'llm' ? 'Gemini' : 'Perplexity'}</span>
                      <svg className="chevron" width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M7 10l5 5 5-5z" />
                      </svg>
                    </button>

                    {showModeDropdown && (
                      <div className="mode-dropdown">
                        <div className="mode-options">
                          <button
                            className={`mode-option ${mode === 'llm' ? 'active' : ''}`}
                            onClick={() => { setMode('llm'); setShowModeDropdown(false); }}
                            type="button"
                          >
                            <ModeIcon mode="llm" />
                            <div className="mode-text">
                              <span>LLM Mode</span>
                              <span className="mode-desc">Gemini AI</span>
                            </div>
                          </button>
                          <button
                            className={`mode-option ${mode === 'perplexity' ? 'active' : ''}`}
                            onClick={() => { setMode('perplexity'); setShowModeDropdown(false); }}
                            type="button"
                          >
                            <ModeIcon mode="perplexity" />
                            <div className="mode-text">
                              <span>Perplexity</span>
                              <span className="mode-desc">Sonar Search</span>
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
        </div>
      </div>
    </>
  )
}

function ModeIcon({ mode }: { mode: QueryMode }) {
  if (mode === 'llm') {
    return (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
        <path d="M9 2L7.17 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2h-3.17L15 2H9zm3 15c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5z" />
      </svg>
    )
  }
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 17h-2v-2h2v2zm2.07-7.75l-.9.92C13.45 12.9 13 13.5 13 15h-2v-.5c0-1.1.45-2.1 1.17-2.83l1.24-1.26c.37-.36.59-.86.59-1.41 0-1.1-.9-2-2-2s-2 .9-2 2H8c0-2.21 1.79-4 4-4s4 1.79 4 4c0 .88-.36 1.68-.93 2.25z" />
    </svg>
  )
}
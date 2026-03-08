import { useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import LiquidGlass from './LiquidGlass'
import './meeting-mode.css'

type MeetingStatus = 'ready' | 'running' | 'paused' | 'listening' | 'stopped' | 'error' | string
type MeetingPage = 'selection' | 'setup' | 'active' | 'ended'
type MeetingType = 'online' | 'physical'

interface KeyStatus {
  haiku: boolean
  perplexity: boolean
  deepgram?: boolean
  groq?: boolean
  anthropic?: boolean
}

interface MeetingModeProps {
  active: boolean
  keyStatus: KeyStatus
  onBackToChat: () => void
  containerRef?: React.RefObject<HTMLDivElement | null>
  containerClassName?: string
  containerStyle?: React.CSSProperties
}

interface TranscriptSegment {
  segment_id?: number
  speaker: string
  text: string
  raw_text?: string
  is_final: boolean
  meeting_time: number
  timestamp: number
  correction?: boolean
}

interface MeetingUpdate {
  summary?: string
  cues?: string[]
  nudge?: string
  action_items?: string[]
  meeting_time?: number
  timestamp?: number
}

interface CueCard {
  id: string
  timestamp: number
  cues: string[]
}

interface QAItem {
  question: string
  answer: string
  references: AnswerReference[]
  timestamp: number
  streaming?: boolean
}

interface NudgeItem {
  id: string
  text: string
  timestamp: number
}

interface AnswerReference {
  title: string
  url: string
}

interface MeetingSettings {
  name_on_call: string
  mic_sensitivity: number
  update_interval_sec: number
}

const UPDATE_INTERVAL_MIN = 1
const UPDATE_INTERVAL_MAX = 5

interface ListeningModeOption {
  id: 'focused' | 'balanced' | 'sensitive'
  label: string
  description: string
  value: number
  default?: boolean
}

const DEFAULT_MEETING_SETTINGS: MeetingSettings = {
  name_on_call: 'User',
  mic_sensitivity: 55,
  update_interval_sec: 1,
}

const LISTENING_MODE_OPTIONS: ListeningModeOption[] = [
  { id: 'focused', label: 'Focused', description: 'Ignore more room noise', value: 35 },
  { id: 'balanced', label: 'Balanced', description: 'Default for most calls', value: 55, default: true },
  { id: 'sensitive', label: 'Sensitive', description: 'Pick up softer voices', value: 75 },
]

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value))
}

function normalizeMeetingSettings(raw: unknown): MeetingSettings {
  const data = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const sensitivityValue = Number(data.mic_sensitivity)
  const intervalValue = Number(data.update_interval_sec)
  return {
    name_on_call: String(data.name_on_call || DEFAULT_MEETING_SETTINGS.name_on_call).trim() || DEFAULT_MEETING_SETTINGS.name_on_call,
    mic_sensitivity: Number.isFinite(sensitivityValue)
      ? clamp(Math.round(sensitivityValue), 0, 100)
      : DEFAULT_MEETING_SETTINGS.mic_sensitivity,
    update_interval_sec: Number.isFinite(intervalValue)
      ? clamp(intervalValue, UPDATE_INTERVAL_MIN, UPDATE_INTERVAL_MAX)
      : DEFAULT_MEETING_SETTINGS.update_interval_sec,
  }
}

function getListeningModeOption(value: number): ListeningModeOption {
  return LISTENING_MODE_OPTIONS.reduce((closest, option) => {
    const currentDelta = Math.abs(option.value - value)
    const bestDelta = Math.abs(closest.value - value)
    return currentDelta < bestDelta ? option : closest
  }, LISTENING_MODE_OPTIONS[1])
}

function formatDuration(totalSeconds: number): string {
  const sec = Math.max(0, Math.floor(totalSeconds))
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = sec % 60
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

function formatClock(ts: number): string {
  const epochMs = ts > 1e11 ? ts : ts * 1000
  return new Date(epochMs).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

function formatCompactClock(ts: number): string {
  const epochMs = ts > 1e11 ? ts : ts * 1000
  return new Date(epochMs).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  })
}

function getHostname(url: string): string {
  try {
    const host = new URL(url).hostname.replace(/^www\./i, '')
    return host || 'source'
  } catch {
    return 'source'
  }
}

function dedupeStrings(values: string[]): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const value of values) {
    const text = value.trim()
    const norm = text.toLowerCase()
    if (!text || seen.has(norm)) continue
    seen.add(norm)
    out.push(text)
  }
  return out
}

function normalizeAnswerReferences(raw: unknown, answer: string): AnswerReference[] {
  const seen = new Set<string>()
  const references: AnswerReference[] = []

  const add = (title: string, url: string) => {
    const cleanUrl = url.trim()
    if (!/^https?:\/\//i.test(cleanUrl)) return
    const key = cleanUrl.toLowerCase()
    if (seen.has(key)) return
    seen.add(key)
    references.push({
      title: (title || cleanUrl).trim(),
      url: cleanUrl,
    })
  }

  if (Array.isArray(raw)) {
    raw.forEach((entry) => {
      if (entry && typeof entry === 'object') {
        const rec = entry as Record<string, unknown>
        add(String(rec.title ?? ''), String(rec.url ?? ''))
      } else if (typeof entry === 'string') {
        add(entry, entry)
      }
    })
  }

  const markdownLinkRegex = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g
  let match: RegExpExecArray | null
  while ((match = markdownLinkRegex.exec(answer)) !== null) {
    add(match[1], match[2])
  }

  return references
}

export default function MeetingMode({
  active,
  keyStatus,
  onBackToChat,
  containerRef,
  containerClassName,
  containerStyle,
}: MeetingModeProps) {
  const [page, setPage] = useState<MeetingPage>('selection')
  const [meetingType, setMeetingType] = useState<MeetingType>('online')

  const [title, setTitle] = useState('Product Sync')
  const [goal, setGoal] = useState('')
  const [instructions, setInstructions] = useState('')
  const [meetingSettings, setMeetingSettings] = useState<MeetingSettings>(DEFAULT_MEETING_SETTINGS)
  const [settingsDraft, setSettingsDraft] = useState<MeetingSettings>(DEFAULT_MEETING_SETTINGS)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsSaveState, setSettingsSaveState] = useState<'idle' | 'saving' | 'saved'>('idle')

  const [status, setStatus] = useState<MeetingStatus>('ready')
  const [, setMeetingId] = useState<string | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const [meetingError, setMeetingError] = useState<string | null>(null)

  const [transcripts, setTranscripts] = useState<TranscriptSegment[]>([])
  const [summaryHistory, setSummaryHistory] = useState<string[]>([])
  const [cueCards, setCueCards] = useState<CueCard[]>([])
  const [nudges, setNudges] = useState<NudgeItem[]>([])
  const [pinnedNudge, setPinnedNudge] = useState<NudgeItem | null>(null)
  const [actionItems, setActionItems] = useState<string[]>([])
  const [qaHistory, setQaHistory] = useState<QAItem[]>([])
  const [askText, setAskText] = useState('')
  const [askingText, setAskingText] = useState('')
  const [isAsking, setIsAsking] = useState(false)
  const [rightPanelView, setRightPanelView] = useState<'nudges' | 'answers'>('nudges')
  const [nudgesUnread, setNudgesUnread] = useState(0)
  const [answersUnread, setAnswersUnread] = useState(0)

  const [webSearchEnabled, setWebSearchEnabled] = useState(true)
  const [showNudges, setShowNudges] = useState(true)
  const [ccEnabled, setCcEnabled] = useState(true)
  const [endedTab, setEndedTab] = useState<'summary' | 'actions' | 'transcript'>('summary')
  const liveNameRef = useRef<HTMLSpanElement | null>(null)
  const nameInputRef = useRef<HTMLInputElement | null>(null)

  const keyReady = Boolean(keyStatus.deepgram && keyStatus.anthropic)
  const running = status === 'running' || status === 'listening'
  const paused = status === 'paused'
  const canAsk = running || paused || page === 'ended'

  const finalTranscripts = useMemo(
    () => transcripts.filter((s) => s.is_final),
    [transcripts],
  )
  const normalizedSettingsDraft = useMemo(
    () => normalizeMeetingSettings(settingsDraft),
    [settingsDraft],
  )
  const settingsChanged =
    normalizedSettingsDraft.name_on_call !== meetingSettings.name_on_call ||
    normalizedSettingsDraft.mic_sensitivity !== meetingSettings.mic_sensitivity ||
    normalizedSettingsDraft.update_interval_sec !== meetingSettings.update_interval_sec
  const visibleNudges = pinnedNudge
    ? nudges.filter((item) => item.id !== pinnedNudge.id)
    : nudges

  const resetLiveData = () => {
    setTranscripts([])
    setSummaryHistory([])
    setCueCards([])
    setNudges([])
    setPinnedNudge(null)
    setNudgesUnread(0)
    setActionItems([])
    setQaHistory([])
    setAskText('')
    setAskingText('')
    setIsAsking(false)
    setElapsed(0)
    setMeetingId(null)
    setRightPanelView('nudges')
    setAnswersUnread(0)
  }

  useEffect(() => {
    const offMeetingSettings = window.cosmic?.onMeetingSettings((data) => {
      setMeetingSettings(normalizeMeetingSettings(data))
    })

    window.cosmic?.getMeetingSettings()
    return () => {
      offMeetingSettings?.()
    }
  }, [])

  useEffect(() => {
    const offStatus = window.cosmic?.onMeetingStatus((data) => {
      const next = String(data?.status || 'ready')
      setStatus(next)
      if (data?.meeting_id) setMeetingId(String(data.meeting_id))
      if (next === 'error') {
        setMeetingError(String(data?.error || 'Meeting error'))
        return
      }
      setMeetingError(null)
      if (next === 'running' || next === 'listening') setPage('active')
      else if (next === 'stopped') setPage('ended')
    })

    const offTranscript = window.cosmic?.onMeetingTranscript((data: TranscriptSegment) => {
      setTranscripts((prev) => {
        const segmentId = typeof data?.segment_id === 'number' ? data.segment_id : null
        if (segmentId !== null) {
          const existingIdx = prev.findIndex((item) => item.segment_id === segmentId)
          if (existingIdx >= 0) {
            const next = [...prev]
            next[existingIdx] = { ...next[existingIdx], ...data }
            return next
          }
        }
        return [...prev.slice(-1200), data]
      })
      if (typeof data?.meeting_time === 'number') {
        setElapsed(Math.max(0, Math.floor(data.meeting_time)))
      }
    })

    const offUpdate = window.cosmic?.onMeetingUpdate((data: MeetingUpdate) => {
      const ts = typeof data?.timestamp === 'number' ? data.timestamp : Date.now()

      if (typeof data?.meeting_time === 'number') {
        setElapsed(Math.max(0, Math.floor(data.meeting_time)))
      }

      const summary = typeof data?.summary === 'string' ? data.summary.trim() : ''
      if (summary) {
        setSummaryHistory((prev) => dedupeStrings([...prev, summary]).slice(-24))
      }

      if (Array.isArray(data?.cues) && data.cues.length > 0) {
        const clean = dedupeStrings(data.cues).slice(0, 3)
        if (clean.length > 0) {
          setCueCards((prev) => [
            { id: `${ts}-${clean.join('|')}`, timestamp: ts, cues: clean },
            ...prev.slice(0, 24),
          ])
        }
      }

      const nudge = typeof data?.nudge === 'string' ? data.nudge.trim() : ''
      if (nudge && showNudges) {
        const nextNudge: NudgeItem = {
          id: `${ts}-${nudge.slice(0, 48)}`,
          text: nudge,
          timestamp: ts,
        }
        setNudges((prev) => [{ ...nextNudge }, ...prev].slice(0, 50))
        if (pinnedNudge || rightPanelView !== 'nudges') {
          setNudgesUnread((prev) => prev + 1)
        }
      }

      const actions = Array.isArray(data?.action_items) ? data.action_items.map(String) : []
      if (actions.length > 0) {
        setActionItems((prev) => dedupeStrings([...prev, ...actions]).slice(-60))
      }
    })

    const offChunk = window.cosmic?.onMeetingAnswerChunk((data) => {
      const q = String(data?.question || '')
      const chunk = String(data?.chunk ?? '')
      if (!chunk) return
      setQaHistory((prev) =>
        prev.map((item) =>
          item.streaming && item.question === q ? { ...item, answer: item.answer + chunk } : item
        )
      )
    })

    const offAnswer = window.cosmic?.onMeetingAnswer((data) => {
      setIsAsking(false)
      setAskingText('')
      const q = String(data?.question || '')
      const a = String(data?.answer || '')
      const refs = normalizeAnswerReferences(data?.references, a)
      if (!a.trim() && refs.length === 0) return
      setQaHistory((prev) => {
        const idx = prev.findIndex((item) => item.streaming && (q ? item.question === q : true))
        if (idx >= 0) {
          const next = [...prev]
          next[idx] = { ...next[idx], question: q || next[idx].question, answer: a, references: refs, streaming: false }
          return next
        }
        return [{ question: q, answer: a, references: refs, timestamp: Date.now() }, ...prev.slice(0, 119)]
      })
      if (rightPanelView !== 'answers') setAnswersUnread((prev) => prev + 1)
    })

    const offFinal = window.cosmic?.onMeetingFinal((data) => {
      if (data?.summary) setSummaryHistory((prev) => dedupeStrings([...prev, String(data.summary)]))
      if (Array.isArray(data?.action_items)) setActionItems(dedupeStrings(data.action_items.map(String)))
      setPage('ended')
    })

    window.cosmic?.checkMeetingKeys()
    return () => {
      offStatus?.()
      offTranscript?.()
      offUpdate?.()
      offChunk?.()
      offAnswer?.()
      offFinal?.()
    }
  }, [showNudges, rightPanelView, pinnedNudge])

  useEffect(() => {
    if (!settingsOpen) {
      setSettingsSaveState('idle')
      return
    }

    if (settingsChanged) {
      const timer = window.setTimeout(() => {
        setSettingsSaveState('saving')
        window.cosmic?.saveMeetingSettings(normalizedSettingsDraft)
      }, 240)
      return () => window.clearTimeout(timer)
    }

    if (settingsSaveState === 'saving') {
      setSettingsSaveState('saved')
      const timer = window.setTimeout(() => setSettingsSaveState('idle'), 1100)
      return () => window.clearTimeout(timer)
    }
  }, [
    settingsOpen,
    settingsChanged,
    normalizedSettingsDraft,
    settingsSaveState,
  ])

  useEffect(() => {
    if (rightPanelView === 'answers' && answersUnread > 0) {
      setAnswersUnread(0)
    }
  }, [rightPanelView, answersUnread])

  useEffect(() => {
    if (rightPanelView === 'nudges' && !pinnedNudge && nudgesUnread > 0) {
      setNudgesUnread(0)
    }
  }, [rightPanelView, pinnedNudge, nudgesUnread])

  useEffect(() => {
    if (!(running || paused)) return
    window.cosmic?.setMeetingWebSearch(webSearchEnabled)
  }, [webSearchEnabled, running, paused])

  useEffect(() => {
    if (!running) return
    const id = window.setInterval(() => setElapsed((p) => p + 1), 1000)
    return () => window.clearInterval(id)
  }, [running])

  const handleSelectType = (type: MeetingType) => {
    setMeetingType(type)
    setPage('setup')
  }

  const handleStartMeeting = () => {
    if (!keyReady) {
      setMeetingError('Deepgram and Anthropic keys are required. Configure them in Settings.')
      return
    }
    setMeetingError(null)
    resetLiveData()
    setPage('active')
    window.cosmic?.startMeeting({
      title: title.trim() || 'Meeting',
      goal: goal.trim(),
      user_name: meetingSettings.name_on_call.trim() || 'User',
      custom_instructions: instructions.trim(),
      meeting_type: meetingType,
      web_search_enabled: webSearchEnabled,
      mic_sensitivity: meetingSettings.mic_sensitivity,
      update_interval_sec: meetingSettings.update_interval_sec,
    })
  }

  const handleStopMeeting = () => window.cosmic?.stopMeeting()
  const handlePauseResume = () => (paused ? window.cosmic?.resumeMeeting() : window.cosmic?.pauseMeeting())

  const askQuestion = (q: string) => {
    const text = q.trim()
    if (!text || !canAsk || isAsking) return
    setAskingText(text)
    setIsAsking(true)
    setQaHistory((prev) => [
      { question: text, answer: '', references: [], timestamp: Date.now(), streaming: true },
      ...prev.slice(0, 119),
    ])
    if (rightPanelView !== 'answers') setAnswersUnread((prev) => prev + 1)
    window.cosmic?.askMeeting({ question: text, web_search_enabled: webSearchEnabled })
  }

  const handleAsk = () => {
    if (!askText.trim()) return
    askQuestion(askText)
    setAskText('')
  }

  const handleCueClick = (cue: string) => {
    setAskText(cue)
    askQuestion(cue)
  }

  const handlePinNudge = (item: NudgeItem) => {
    setPinnedNudge(item)
    setNudgesUnread(0)
  }

  const handleUnpinNudge = () => {
    if (pinnedNudge) {
      setNudges((prev) => [pinnedNudge, ...prev.filter((item) => item.id !== pinnedNudge.id)].slice(0, 50))
    }
    setPinnedNudge(null)
    if (rightPanelView === 'nudges') {
      setNudgesUnread(0)
    }
  }

  const handleNewMeeting = () => {
    setStatus('ready')
    resetLiveData()
    setPage('selection')
  }

  const openSettings = () => {
    setSettingsDraft(meetingSettings)
    setSettingsOpen(true)
  }

  const triggerNameTypingFeedback = () => {
    if (liveNameRef.current?.animate) {
      liveNameRef.current.animate(
        [
          { opacity: 0.45, filter: 'blur(1.5px)', transform: 'translateY(2px)' },
          { opacity: 1, filter: 'blur(0px)', transform: 'translateY(0px)' },
        ],
        {
          duration: 180,
          easing: 'cubic-bezier(0.22, 0.7, 0.2, 1)',
        },
      )
    }

    if (nameInputRef.current?.animate) {
      nameInputRef.current.animate(
        [
          { transform: 'translateY(0px)', boxShadow: '0 0 0 rgba(255,255,255,0)' },
          { transform: 'translateY(-0.5px)', boxShadow: '0 8px 18px rgba(0, 0, 0, 0.14)' },
          { transform: 'translateY(0px)', boxShadow: '0 0 0 rgba(255,255,255,0)' },
        ],
        {
          duration: 220,
          easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)',
        },
      )
    }
  }

  // ---------------------------------------------------------------------------
  // Selection
  // ---------------------------------------------------------------------------
  const renderSelectionPage = () => (
    <div className="m-page m-selection">
      <div className="m-selection-layout">
        <div className="m-selection-head">
          <div className="m-selection-eyebrow-row">
            <span className="m-eyebrow-pill">Meeting mode</span>
            <span className="m-eyebrow-pill soft">Live AI</span>
          </div>
          <h1 className="m-hero">Meeting Assistant</h1>
          <p className="m-hero-sub">Choose how this meeting starts. The assistant will capture context, cues, and summaries in real time.</p>
        </div>

        <div className="m-selection-tiles">
          <button className="m-select-tile online" onClick={() => handleSelectType('online')}>
            <span className="m-select-tile-tag">Recommended</span>
            <div className="m-select-tile-copy">
              <strong>Online meeting</strong>
              <p>Zoom, Teams, Meet, browser calls</p>
            </div>
            <div className="m-select-tile-points">
              <span>Live transcript</span>
              <span>Nudges and answers</span>
            </div>
            <span className="m-select-tile-meta">Start now</span>
          </button>

          <button className="m-select-tile disabled" disabled>
            <span className="m-select-tile-tag muted">Coming soon</span>
            <div className="m-select-tile-copy">
              <strong>In-person meeting</strong>
              <p>Room capture and nearby speakers</p>
            </div>
            <div className="m-select-tile-points">
              <span>Mic array capture</span>
              <span>Speaker separation</span>
            </div>
            <span className="m-select-tile-meta">Unavailable</span>
          </button>
        </div>

        <div className="m-selection-footer">
          <button className="m-ghost-btn m-ghost-subtle" onClick={() => setPage('setup')}>
            Set up details first
          </button>
          <p className="m-selection-footnote">
            Audio stays on-device by default. Web search only sends anonymized snippets when you enable it.
          </p>
          {!keyReady && (
            <p className="m-warn m-warn-inline">Deepgram + Groq API keys required. Add them in Settings.</p>
          )}
        </div>
      </div>
    </div>
  )

  // ---------------------------------------------------------------------------
  // Setup
  // ---------------------------------------------------------------------------
  const renderSetupPage = () => (
    <div className="m-page m-setup">
      <div className="m-setup-header">
        <div>
          <div className="m-selection-eyebrow-row">
            <span className="m-eyebrow-pill">Meeting details</span>
            <span className="m-eyebrow-pill soft">
              {meetingType === 'online' ? 'Online' : 'In-person (soon)'}
            </span>
          </div>
          <h2 className="m-page-title">Outline the call in a few seconds</h2>
          <p className="m-setup-sub">
            Give the assistant just enough context to take high-quality notes and nudge you at the right time.
          </p>
        </div>
      </div>

      {meetingError && <p className="m-error">{meetingError}</p>}

      <div className="m-setup-grid">
        <div className="m-setup-card">
          <div className="m-setup-card-head">
            <span>Meeting context</span>
            <div className="m-setup-card-head-right">
              <span className={`m-meta-pill compact ${keyReady ? 'ready' : ''}`}>
                {keyReady ? 'Deepgram + Groq ready' : 'Keys missing'}
              </span>
              <span className="m-meta-pill compact subtle">Live transcript · Cues · Actions</span>
            </div>
          </div>
          <div className="m-setup-form-wrap">
            <div className="m-setup-form-grid">
              <label className="m-field">
                <span>Meeting name</span>
                <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Weekly product sync" />
              </label>
              <div className="m-settings-preview-card">
                <div className="m-settings-preview-head">
                  <div className="m-settings-preview-title">
                    <span>Meeting settings</span>
                    <strong>Defaults for every call</strong>
                  </div>
                  <button type="button" className="m-ghost-btn m-settings-preview-btn" onClick={openSettings}>
                    Edit
                  </button>
                </div>
                <div className="m-settings-preview-list">
                  <div className="m-settings-preview-row">
                    <span>Name on call</span>
                    <strong>{meetingSettings.name_on_call}</strong>
                  </div>
                  <div className="m-settings-preview-row">
                    <span>Listening mode</span>
                    <strong>{getListeningModeOption(meetingSettings.mic_sensitivity).label}</strong>
                  </div>
                  <div className="m-settings-preview-row">
                    <span>Response speed</span>
                    <strong>{meetingSettings.update_interval_sec}s</strong>
                  </div>
                </div>
              </div>
              <label className="m-field m-field-span">
                <span>Goal for this call</span>
                <input
                  value={goal}
                  onChange={(e) => setGoal(e.target.value)}
                  placeholder="Decide launch scope, owners, and timelines"
                />
              </label>
              <label className="m-field m-field-span">
                <span>Custom instructions</span>
                <textarea
                  rows={4}
                  value={instructions}
                  onChange={(e) => setInstructions(e.target.value)}
                  placeholder="Highlight blockers, open risks, and follow-ups. Prefer concise bullets over prose."
                />
              </label>
            </div>
          </div>
        </div>
      </div>

      <div className="m-row m-row-spread m-setup-footer">
        <button className="m-ghost-btn" onClick={() => setPage('selection')}>Back</button>
        <div className="m-setup-footer-right">
          <span className="m-setup-hint">You can tweak these details while the meeting is running.</span>
          <button className="m-primary-btn" onClick={handleStartMeeting}>Start meeting</button>
        </div>
      </div>
    </div>
  )

  // ---------------------------------------------------------------------------
  // Active meeting
  // ---------------------------------------------------------------------------
  const renderActivePage = () => (
    <div className="m-page m-active">
      {/* Top bar */}
      <div className="m-topbar">
        <div className="m-topbar-left">
          {running && <span className="m-rec-dot" />}
          <span className="m-timer">{formatDuration(elapsed)}</span>
          <span className="m-badge-status">{paused ? 'Paused' : running ? 'Live' : status}</span>
        </div>
        <div className="m-topbar-right">
          <button
            className={`m-chip toggle ${showNudges ? 'on' : 'off'}`}
            onClick={() => setShowNudges((p) => !p)}
            aria-pressed={showNudges}
          >
            Nudges
          </button>
          <button
            className={`m-chip toggle ${webSearchEnabled ? 'on' : 'off'}`}
            onClick={() => setWebSearchEnabled((p) => !p)}
            aria-pressed={webSearchEnabled}
          >
            Web
          </button>
          <button
            className={`m-chip toggle ${ccEnabled ? 'on' : 'off'}`}
            onClick={() => setCcEnabled((p) => !p)}
            aria-pressed={ccEnabled}
          >
            CC
          </button>
          {(running || paused) && (
            <button className="m-chip ghost" onClick={handlePauseResume}>{paused ? 'Resume' : 'Pause'}</button>
          )}
          <button className="m-chip danger" onClick={handleStopMeeting} disabled={!running && !paused}>Stop</button>
        </div>
      </div>

      {meetingError && <p className="m-error">{meetingError}</p>}

      {/* Content area */}
      <div className="m-content-split">
        {/* Left column: suggested cues + transcript stacked */ }
        <div className="m-col m-col-left">
          <div className="m-subcol m-subcol-suggested">
            <div className="m-col-head">
              <span>Suggested</span>
            </div>
            <div className="m-scroll m-suggest-scroll">
              {cueCards.map((card) => (
                <div className="m-cue-card" key={card.id}>
                  <span className="m-cue-ts">{formatClock(card.timestamp)}</span>
                  {card.cues.map((cue) => (
                    <button key={`${card.id}-${cue}`} className="m-cue-btn" onClick={() => handleCueClick(cue)} title={cue}>
                      {cue.length > 60 ? cue.slice(0, 57) + '…' : cue}
                    </button>
                  ))}
                </div>
              ))}
            </div>
            <div className="m-suggest-footer">
              <button
                className="m-cue-btn muted m-cue-btn-fixed"
                onClick={() => handleCueClick('What should I say now?')}
              >
                What should I say now?
              </button>
            </div>
          </div>

          {ccEnabled && (
            <div className="m-subcol m-subcol-transcript">
              <div className="m-col-head">
                <span>Transcript</span>
              </div>
              <div className="m-scroll">
                {finalTranscripts.length === 0 ? (
                  <div className="m-empty">
                    {running ? (
                      <span className="m-listening"><span className="m-bar" /><span className="m-bar" /><span className="m-bar" /><span>Listening</span></span>
                    ) : 'Waiting to start…'}
                  </div>
                ) : (
                  [...finalTranscripts].reverse().map((seg, i) => (
                    <div key={`${seg.timestamp}-${i}`} className="m-line">
                      <span className="m-who">{seg.speaker}</span>
                      <span>{seg.text}</span>
                    </div>
                  ))
                )}
              </div>
            </div>
          )}

        </div>

        {/* Right column: nudges / answers */}
        <div className="m-col m-col-right">
          <div className="m-col-head m-col-head-tabs">
            <div className="m-col-head-title">
              <span>{rightPanelView === 'nudges' ? 'Nudges' : 'Answers'}</span>
              <span className="m-col-head-sub">
                {rightPanelView === 'nudges'
                  ? (showNudges ? 'Live coaching cues' : 'Nudges paused')
                  : 'Latest first - markdown + references'}
              </span>
            </div>
            <div className="m-col-toggle">
              <button
                className={`m-chip tiny ${rightPanelView === 'nudges' ? 'on' : ''}`}
                onClick={() => {
                  setRightPanelView('nudges')
                  if (!pinnedNudge) setNudgesUnread(0)
                }}
              >
                Nudges
                {nudgesUnread > 0 && (
                  <span className="m-chip-badge">{nudgesUnread > 99 ? '99+' : nudgesUnread}</span>
                )}
              </button>
              <button
                className={`m-chip tiny ${rightPanelView === 'answers' ? 'on' : ''}`}
                onClick={() => {
                  setRightPanelView('answers')
                  setAnswersUnread(0)
                }}
              >
                Answers
                {answersUnread > 0 && (
                  <span className="m-chip-badge">{answersUnread > 99 ? '99+' : answersUnread}</span>
                )}
              </button>
            </div>
          </div>

          {rightPanelView === 'nudges' ? (
            <div className="m-scroll m-panel-scroll">
              {!showNudges ? (
                <div className="m-empty">Nudges are paused. Enable the Nudges chip in the top bar.</div>
              ) : nudges.length === 0 && !pinnedNudge ? (
                <div className="m-empty">Nudges will appear here as the meeting progresses.</div>
              ) : (
                <div className="m-qa-section">
                  {pinnedNudge && (
                    <div className="m-qa-item m-nudge-pinned">
                      <div className="m-qa-meta">
                        <span className="m-cue-ts">Pinned · {formatClock(pinnedNudge.timestamp)}</span>
                        <button className="m-nudge-pin-btn active" onClick={handleUnpinNudge}>
                          Unpin
                        </button>
                      </div>
                      <div className="m-qa-a">{pinnedNudge.text}</div>
                    </div>
                  )}
                  {visibleNudges.map((item, i) => (
                    <div key={item.id || `${item.timestamp}-${i}`} className="m-qa-item">
                      <div className="m-qa-meta">
                        <span className="m-cue-ts">{formatClock(item.timestamp)}</span>
                        <button
                          className="m-nudge-pin-btn"
                          onClick={() => handlePinNudge(item)}
                          disabled={pinnedNudge?.id === item.id}
                        >
                          Pin
                        </button>
                      </div>
                      <div className="m-qa-a">{item.text}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <div className="m-scroll m-panel-scroll">
              {qaHistory.length === 0 ? (
                <div className="m-empty">Answers will appear here when you ask a question or click a suggestion.</div>
              ) : (
                <div className="m-qa-section">
                  {qaHistory.map((item, i) => {
                    const hasMarkdownReferences = /(^|\n)#{1,6}\s+references\b/i.test(item.answer)
                    return (
                      <div key={`${item.timestamp}-${i}`} className="m-qa-item">
                        <div className="m-qa-meta">
                          <span className="m-qa-time">{formatCompactClock(item.timestamp)}</span>
                        </div>
                        {item.question && <div className="m-qa-q">{item.question}</div>}
                        <div className="m-qa-a m-answer-markdown">
                          <ReactMarkdown
                            remarkPlugins={[remarkGfm, remarkMath]}
                            rehypePlugins={[rehypeKatex]}
                            components={{
                              a: ({ href, ...props }) => (
                                <a
                                  {...props}
                                  href={href}
                                  onClick={(e) => {
                                    if (!href) return
                                    e.preventDefault()
                                    window.cosmic?.openExternal(href)
                                  }}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                />
                              ),
                            }}
                          >
                            {item.answer}
                          </ReactMarkdown>
                          {item.streaming && <span className="m-answer-cursor" aria-hidden>▌</span>}
                        </div>
                        {item.references.length > 0 && !hasMarkdownReferences && (
                          <div className="m-answer-refs">
                            <div className="m-answer-refs-title">References</div>
                            <div className="m-answer-refs-list">
                              {item.references.map((ref, refIndex) => (
                                <a
                                  key={`${ref.url}-${refIndex}`}
                                  href={ref.url}
                                  className="m-answer-ref"
                                  onClick={(e) => {
                                    e.preventDefault()
                                    window.cosmic?.openExternal(ref.url)
                                  }}
                                >
                                  <span className="m-answer-ref-title">{ref.title}</span>
                                  <span className="m-answer-ref-meta">
                                    <span className="m-answer-ref-domain">{getHostname(ref.url)}</span>
                                    <span className="m-answer-ref-url">{ref.url}</span>
                                  </span>
                                </a>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Ask input pinned to the bottom of the full meeting canvas */}
      {canAsk && (
        <div className="m-ask-row">
          <div className="m-ask-input-wrap">
            <span className="m-ask-attachment-tab">Meeting Context atttached</span>
            <span className="m-ask-pill">Ask</span>
            {isAsking && askingText && (
              <span className="m-ask-preview" aria-hidden>
                <span className="m-ask-preview-text">{askingText}</span>
              </span>
            )}
            <input
              value={askText}
              onChange={(e) => setAskText(e.target.value)}
              placeholder="Ask about this meeting..."
              onKeyDown={(e) => e.key === 'Enter' && handleAsk()}
              disabled={!canAsk || isAsking}
            />
            <button
              className="m-ask-submit"
              onClick={handleAsk}
              disabled={!canAsk || !askText.trim() || isAsking}
            >
              {isAsking ? (
                <span className="m-ask-loader-grid" aria-label="Answering">
                  <span />
                  <span />
                  <span />
                  <span />
                  <span />
                  <span />
                  <span />
                  <span />
                </span>
              ) : '→'}
            </button>
          </div>
        </div>
      )}
    </div>
  )

  // ---------------------------------------------------------------------------
  // Settings overlay
  // ---------------------------------------------------------------------------
  const selectedListeningMode = getListeningModeOption(settingsDraft.mic_sensitivity)
  const selectedListeningModeIndex = Math.max(
    0,
    LISTENING_MODE_OPTIONS.findIndex((option) => option.id === selectedListeningMode.id),
  )
  const settingsDisplayName = normalizedSettingsDraft.name_on_call.trim() || 'User'
  const settingsInitial = settingsDisplayName.charAt(0).toUpperCase() || 'U'
  const settingsStatusText =
    settingsSaveState === 'saving'
      ? 'Saving'
      : settingsSaveState === 'saved'
        ? 'Saved'
        : 'Local'

  const renderSettingsOverlay = () => (
    <div className="m-settings-overlay" onClick={() => setSettingsOpen(false)}>
      <div className="m-settings-sheet" onClick={(e) => e.stopPropagation()}>
        <div className="m-settings-sheet-head">
          <span className="m-settings-kicker">Meeting settings</span>
          <button className="m-back-btn" onClick={() => setSettingsOpen(false)} aria-label="Close meeting settings">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>
          </button>
        </div>

        <div className="m-settings-sheet-body">
          <div className="m-settings-hero">
            <div className="m-settings-avatar" aria-hidden="true">{settingsInitial}</div>
            <div className="m-settings-hero-copy">
              <h3 className="m-settings-title">
                You are <span ref={liveNameRef} className="m-settings-live-name">{settingsDisplayName}</span> on this call.
              </h3>
              <p className="m-settings-hero-sub">
                The assistant is listening in <strong>{selectedListeningMode.label}</strong> mode.
              </p>
            </div>
            <span className={`m-settings-status ${settingsSaveState}`}>
              <span key={settingsStatusText} className="m-settings-status-text">{settingsStatusText}</span>
            </span>
          </div>

          <div className="m-settings-panel">
            <div className="m-settings-section">
              <div className="m-settings-section-head">
                <span className="m-settings-section-label">Identity</span>
                <p className="m-settings-section-copy">How the assistant refers to you in prompts and nudges.</p>
              </div>
              <div className="m-settings-row">
                <div className="m-settings-row-copy">
                  <span className="m-settings-row-label">Name on call</span>
                  <p className="m-settings-row-help">Used across summaries, answers, and nudges.</p>
                </div>
                <input
                  ref={nameInputRef}
                  className="m-settings-inline-input"
                  value={settingsDraft.name_on_call}
                  onChange={(e) => {
                    setSettingsDraft((prev) => ({ ...prev, name_on_call: e.target.value }))
                    triggerNameTypingFeedback()
                  }}
                  placeholder="Praveen"
                />
              </div>
            </div>

            <div className="m-settings-section">
              <div className="m-settings-section-head m-settings-section-head-spread">
                <div>
                  <span className="m-settings-section-label">Audio</span>
                  <p className="m-settings-section-copy">Choose the listening bias. The capture layer still adapts to room noise live.</p>
                </div>
                <span className="m-meta-pill subtle">Adaptive</span>
              </div>
              <div className="m-settings-row m-settings-row-listening">
                <div
                  className="m-settings-segmented"
                  style={{ ['--m-settings-segment-index' as any]: selectedListeningModeIndex }}
                >
                  <span className="m-settings-segment-glider" aria-hidden="true" />
                  {LISTENING_MODE_OPTIONS.map((option) => (
                    <button
                      key={option.id}
                      type="button"
                      className={`m-settings-segment ${selectedListeningMode.id === option.id ? 'active' : ''}`}
                      onClick={() => setSettingsDraft((prev) => ({ ...prev, mic_sensitivity: option.value }))}
                    >
                      {option.label}
                      {option.default && <span className="m-settings-segment-default">Default</span>}
                    </button>
                  ))}
                </div>
                <p className="m-settings-mode-note">{selectedListeningMode.description}</p>
              </div>
            </div>

            <div className="m-settings-section">
              <div className="m-settings-section-head">
                <span className="m-settings-section-label">Response speed</span>
                <p className="m-settings-section-copy">How often nudges and suggestions refresh. Faster = more responsive, slower = fewer API calls.</p>
              </div>
              <div className="m-settings-row m-settings-row-listening">
                <div className="m-settings-response-speed">
                  <input
                    type="range"
                    min={UPDATE_INTERVAL_MIN}
                    max={UPDATE_INTERVAL_MAX}
                    step={1}
                    value={settingsDraft.update_interval_sec}
                    onChange={(e) => setSettingsDraft((prev) => ({ ...prev, update_interval_sec: clamp(Number(e.target.value), UPDATE_INTERVAL_MIN, UPDATE_INTERVAL_MAX) }))}
                    className="m-settings-range"
                  />
                  <div className="m-settings-range-labels">
                    <span>1s</span>
                    <span>2s</span>
                    <span>3s</span>
                    <span>4s</span>
                    <span>5s</span>
                  </div>
                </div>
                <p className="m-settings-mode-note">
                  {settingsDraft.update_interval_sec}s between updates
                  {settingsDraft.update_interval_sec === 1 ? ' — fastest' : settingsDraft.update_interval_sec === 5 ? ' — calmest' : ''}
                </p>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  )

  // ---------------------------------------------------------------------------
  // Ended
  // ---------------------------------------------------------------------------
  const renderEndedPage = () => (
    <div className="m-page m-ended">
      <div className="m-ended-hero">
        <span className="m-ended-badge">Completed</span>
        <h2>{title}</h2>
        <p>{formatDuration(elapsed)} &middot; {finalTranscripts.length} lines</p>
      </div>

      <div className="m-kpis">
        <div className="m-kpi"><strong>{formatDuration(elapsed)}</strong><span>Duration</span></div>
        <div className="m-kpi"><strong>{actionItems.length}</strong><span>Actions</span></div>
        <div className="m-kpi"><strong>{finalTranscripts.length}</strong><span>Lines</span></div>
      </div>

      <div className="m-tabs">
        {(['summary', 'actions', 'transcript'] as const).map((t) => (
          <button key={t} className={`m-tab ${endedTab === t ? 'active' : ''}`} onClick={() => setEndedTab(t)}>
            {t[0].toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>

      <div className="m-scroll m-tab-body">
        {endedTab === 'summary' && (
          summaryHistory.length === 0
            ? <div className="m-empty">No summary generated.</div>
            : <ul className="m-bullet-list">{dedupeStrings(summaryHistory).map((s, i) => <li key={i}>{s}</li>)}</ul>
        )}
        {endedTab === 'actions' && (
          actionItems.length === 0
            ? <div className="m-empty">No action items identified.</div>
            : <ul className="m-check-list">{actionItems.map((item, i) => <li key={i}><span className="m-check-box" />{item}</li>)}</ul>
        )}
        {endedTab === 'transcript' && (
          finalTranscripts.length === 0
            ? <div className="m-empty">No transcript recorded.</div>
            : finalTranscripts.map((seg, i) => (
                <div key={`${seg.timestamp}-${i}`} className="m-line compact">
                  <span className="m-who">{seg.speaker}</span>
                  <span>{seg.text}</span>
                </div>
              ))
        )}
      </div>

      <div className="m-row m-row-spread">
        <button className="m-ghost-btn" onClick={handleNewMeeting}>New meeting</button>
        <button className="m-primary-btn" onClick={() => { setAnswersUnread(0); setRightPanelView('answers'); setPage('active') }}>Ask about this meeting</button>
      </div>
    </div>
  )

  // ---------------------------------------------------------------------------
  // Shell — mirrors response-container + LiquidGlass exactly
  // ---------------------------------------------------------------------------
  return (
    <div
      ref={containerRef}
      className={`response-container ${active ? 'visible' : ''} ${containerClassName || ''}`}
      style={{ display: active ? 'flex' : 'none', ...containerStyle }}
      aria-hidden={!active}
    >
      <LiquidGlass disableTilt={true} cornerRadius={32} style={{ width: '100%', height: '100%' }}>
        <div className="response-wrapper">
          <div className="m-root">
            {/* Header row inside the glass */}
            <div className="m-header">
              {page !== 'selection' && (
                <button className="m-back-btn" onClick={() => setPage(page === 'ended' ? 'selection' : page === 'active' ? 'setup' : 'selection')}>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"/></svg>
                </button>
              )}
              <span className="m-header-title">
                {page === 'active' ? title : page === 'ended' ? 'Summary' : 'Meeting'}
              </span>
              <button className="m-back-btn" onClick={openSettings} aria-label="Meeting settings">
                <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M19.14 12.94c.04-.31.06-.63.06-.94s-.02-.63-.06-.94l2.03-1.58a.5.5 0 0 0 .12-.64l-1.92-3.32a.5.5 0 0 0-.6-.22l-2.39.96a7.03 7.03 0 0 0-1.63-.94l-.36-2.54a.5.5 0 0 0-.49-.42h-3.84a.5.5 0 0 0-.49.42l-.36 2.54c-.58.23-1.12.54-1.63.94l-2.39-.96a.5.5 0 0 0-.6.22L2.7 8.84a.5.5 0 0 0 .12.64l2.03 1.58c-.04.31-.06.63-.06.94s.02.63.06.94L2.82 14.5a.5.5 0 0 0-.12.64l1.92 3.32c.13.22.39.31.6.22l2.39-.96c.5.4 1.05.72 1.63.94l.36 2.54c.04.24.25.42.49.42h3.84c.24 0 .45-.18.49-.42l.36-2.54c.58-.23 1.12-.54 1.63-.94l2.39.96c.22.09.47 0 .6-.22l1.92-3.32a.5.5 0 0 0-.12-.64l-2.03-1.56ZM12 15.5A3.5 3.5 0 1 1 12 8.5a3.5 3.5 0 0 1 0 7Z"/></svg>
              </button>
              <button className="m-close-btn" onClick={onBackToChat}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>
              </button>
            </div>

            {page === 'selection' && renderSelectionPage()}
            {page === 'setup' && renderSetupPage()}
            {page === 'active' && renderActivePage()}
            {page === 'ended' && renderEndedPage()}
            {settingsOpen && renderSettingsOverlay()}
          </div>
        </div>
      </LiquidGlass>
    </div>
  )
}

import { useEffect, useMemo, useRef, useState } from 'react'
import { Power, RotateCw } from 'lucide-react'
import './island.css'
import Settings from './Settings'
import WeatherAnimation from './WeatherAnimation'
import type { SearchPosition } from './App'
import CalendarMonthView from './CalendarMonthView'

interface DynamicIslandProps {
  searchActive: boolean
  hovered: boolean
  searchPosition: SearchPosition
  onPositionChange: (pos: SearchPosition) => void
  staybackTime: number
  onStaybackChange: (time: number) => void
  islandOpacity: number
  onOpacityChange: (opacity: number) => void
  debug: boolean
  keyStatus: { gemini: boolean; perplexity: boolean }
}

interface MediaState {
  title: string
  artist: string
  source: string
  appId?: string
  thumbnail: string | null
  isPlaying: boolean
  volume: number | null
  trackKey?: string
  position?: number
  duration?: number
  device?: string
}

interface WindowInfo {
  title: string
  process: string
  appName: string
}

interface WeatherState {
  temp: number
  condition: string
  isDay: boolean
  city: string
  wmo: number
  wind?: number
  humidity?: number
  high?: number
  low?: number
  precip_prob?: number
  snowfall?: number
}

function getSourceColor(source: string): string {
  const s = source.toLowerCase()
  if (s.includes('spotify')) return '#1DB954'
  if (s.includes('music') || s.includes('apple')) return '#FA243C'
  if (s.includes('youtube')) return '#FF0000'
  if (s.includes('chrome')) return '#4285F4'
  if (s.includes('edge')) return '#0078D7'
  return '#007AFF'
}

function SourceIcon({ source, color }: { source: string, color: string }) {
  const s = source.toLowerCase()
  if (s.includes('spotify')) {
    return (
      <svg width="100%" height="100%" viewBox="0 0 24 24" fill="currentColor" style={{ color }}>
        <path d="M12 2a10 10 0 1 0 .001 20.001A10 10 0 0 0 12 2zm4.6 14.5a.9.9 0 0 1-1.24.3c-2.9-1.77-6.55-2.17-10.86-1.2a.9.9 0 1 1-.4-1.76c4.76-1.07 8.86-.6 12.19 1.44.42.26.55.82.31 1.22z" />
      </svg>
    )
  }
  if (s.includes('youtube')) {
    return (
      <svg width="100%" height="100%" viewBox="0 0 24 24" fill="currentColor" style={{ color }}>
        <path d="M23.5 6.2c-.3-1-1-1.8-2-2C19.8 3.7 12 3.7 12 3.7s-7.8 0-9.5.5c-1 .3-1.8 1-2 2C0 8 0 12 0 12s0 4 .5 5.8c.3 1 1 1.8 2 2 1.7.5 9.5.5 9.5.5s7.8 0 9.5-.5c1-.3 1.8-1 2-2 .5-1.8.5-5.8.5-5.8s0-4-.5-5.8zM9.6 15.6V8.4l6.4 3.6-6.4 3.6z" />
      </svg>
    )
  }
  return (
    <svg width="100%" height="100%" viewBox="0 0 24 24" fill="currentColor" style={{ color }}>
      <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z" />
    </svg>
  )
}

function SmartDeviceIcon({ deviceName }: { deviceName: string }) {
  const name = (deviceName || "").toLowerCase()

  if (name.includes('headphone') || name.includes('airpod') || name.includes('buds') || name.includes('headset')) {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
        <path d="M12 3a9 9 0 0 0-9 9v7c0 1.1.9 2 2 2h4v-8H5v-1c0-3.87 3.13-7 7-7s7 3.13 7 7v1h-4v8h4c1.1 0 2-.9 2-2v-7a9 9 0 0 0-9-9z" />
      </svg>
    )
  }

  if (name.includes('monitor') || name.includes('tv') || name.includes('display')) {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
        <path d="M21 3H3c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h5v2h8v-2h5c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 14H3V5h18v12z" />
      </svg>
    )
  }

  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
      <path d="M7 9v6h4l5 5V4L11 9H7z" />
    </svg>
  )
}

function toDataUrlMaybe(raw: string | null | undefined): string | null {
  if (!raw) return null
  const s0 = String(raw).trim()
  if (!s0) return null
  if (s0.startsWith('data:')) return s0
  if (s0.startsWith('http')) return s0
  if (s0.includes('base64,')) return s0
  return null
}

function dataUrlToBlobUrl(dataUrl: string): string | null {
  try {
    const m = dataUrl.match(/^data:([^;]+);base64,(.+)$/)
    if (!m) return null
    const mime = m[1]
    const b64 = m[2].replace(/\s/g, '')
    const bin = atob(b64)
    const bytes = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
    const blob = new Blob([bytes], { type: mime })
    return URL.createObjectURL(blob)
  } catch {
    return null
  }
}



export default function DynamicIsland({
  searchActive,
  hovered,
  searchPosition,
  onPositionChange,
  staybackTime,
  onStaybackChange,
  islandOpacity,
  onOpacityChange,
  keyStatus
}: DynamicIslandProps) {
  const [activeSlide, setActiveSlide] = useState(0)
  const TOTAL_SLIDES = 5

  const [showVolume, setShowVolume] = useState(false)
  const [internalHover, setInternalHover] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [isAnchored, setIsAnchored] = useState(false)

  // New State for Notification
  const [notificationEvent, setNotificationEvent] = useState<any | null>(null)
  // Track notified events to prevent double notification
  const notifiedEventsRef = useRef<Set<string>>(new Set())

  const shouldExpand = searchActive || hovered || internalHover || showSettings || isAnchored || !!notificationEvent
  const [expanded, setExpanded] = useState(shouldExpand)

  useEffect(() => {
    let timer: NodeJS.Timeout
    if (shouldExpand) {
      setExpanded(true)
    } else {
      const delayMs = staybackTime * 1000
      if (delayMs > 0) {
        timer = setTimeout(() => { setExpanded(false) }, delayMs)
      } else {
        setExpanded(false)
      }
    }
    return () => clearTimeout(timer)
  }, [shouldExpand, staybackTime])

  const wasExpanded = useRef(expanded)
  useEffect(() => {
    // If we just collapsed (expanded went from true -> false)
    if (!expanded && wasExpanded.current) {
      setActiveSlide(0)
      setShowMonthView(false)
      setNotificationEvent(null) // Clear notification on collapse
    }
    wasExpanded.current = expanded
  }, [expanded])

  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(id)
  }, [])

  const draggingRef = useRef(false)
  const [localVolume, setLocalVolume] = useState(0)

  const [media, setMedia] = useState<MediaState>({
    title: 'Not Playing',
    artist: 'System Audio',
    source: 'System',
    appId: 'System',
    thumbnail: null,
    isPlaying: false,
    volume: null,
    trackKey: 'System::Not Playing::System Audio',
    position: 0,
    duration: 0,
    device: 'System'
  })

  const [windowInfo, setWindowInfo] = useState<WindowInfo>({
    title: 'Desktop',
    process: 'explorer.exe',
    appName: 'Windows'
  })

  const [weather, setWeather] = useState<WeatherState | null>(null)

  const [isMusicActive, setIsMusicActive] = useState(false)

  useEffect(() => {
    let timer: NodeJS.Timeout
    if (media.title === 'Not Playing') {
      setIsMusicActive(false)
      return
    }

    if (media.isPlaying) {
      setIsMusicActive(true)
    } else {
      timer = setTimeout(() => {
        setIsMusicActive(false)
      }, 60000)
    }
    return () => clearTimeout(timer)
  }, [media.isPlaying, media.title])

  const prevIsMusicActive = useRef(isMusicActive)

  useEffect(() => {
    if (!prevIsMusicActive.current && isMusicActive) {
      if (activeSlide === 1) {
        setActiveSlide(0)
      }
    }
    prevIsMusicActive.current = isMusicActive
  }, [isMusicActive, activeSlide])

  const slideContentMap = useMemo(() => {
    if (notificationEvent) return ['notification'] as const
    if (isMusicActive) return ['music', 'home', 'weather', 'calendar', 'utilities'] as const
    return ['home', 'music', 'weather', 'calendar', 'utilities'] as const
  }, [isMusicActive, notificationEvent])

  useEffect(() => {
    if (!window.cosmic?.onWindowUpdate) return
    const unsub = window.cosmic.onWindowUpdate((data: WindowInfo) => setWindowInfo(data))
    return () => unsub?.()
  }, [])

  useEffect(() => {
    if (!window.cosmic?.onMediaUpdate) return
    const unsub = window.cosmic.onMediaUpdate((data: Partial<MediaState>) => {
      setMedia((prev) => {
        const next: MediaState = { ...prev, ...data }
        if (!draggingRef.current && typeof data.volume === 'number' && Number.isFinite(data.volume)) {
          setLocalVolume(Math.max(0, Math.min(100, Math.round(data.volume))))
        }
        return next
      })
    })
    return () => unsub?.()
  }, [])

  useEffect(() => {
    if (!window.cosmic?.onWeatherUpdate) return
    const unsub = window.cosmic.onWeatherUpdate((data: WeatherState) => {
      setWeather(data)
    })
    window.cosmic?.requestWeather()
    return () => unsub?.()
  }, [])

  const [thumbSrc, setThumbSrc] = useState<string | null>(null)
  const lastObjectUrl = useRef<string | null>(null)
  const lastProcessedThumb = useRef<string | null>(null)

  useEffect(() => {
    const currentThumb = media.thumbnail
    if (currentThumb === lastProcessedThumb.current) return
    lastProcessedThumb.current = currentThumb
    if (lastObjectUrl.current) {
      URL.revokeObjectURL(lastObjectUrl.current)
      lastObjectUrl.current = null
    }
    const dataUrl = toDataUrlMaybe(currentThumb)
    if (!dataUrl) {
      setThumbSrc(null)
      return
    }

    if (dataUrl.startsWith('http')) {
      setThumbSrc(dataUrl)
      return
    }

    const blobUrl = dataUrlToBlobUrl(dataUrl)
    if (blobUrl) {
      lastObjectUrl.current = blobUrl
      setThumbSrc(blobUrl)
    } else {
      setThumbSrc(dataUrl)
    }
  }, [media.thumbnail, media.trackKey])

  const handleControl = (action: 'playpause' | 'next' | 'prev') => {
    window.cosmic?.controlMedia(action)
  }

  const switchSlide = (dir: 'next' | 'prev') => {
    if (dir === 'next' && activeSlide < TOTAL_SLIDES - 1) setActiveSlide(p => p + 1)
    if (dir === 'prev' && activeSlide > 0) setActiveSlide(p => p - 1)
  }

  const lastWheel = useRef(0)
  const onWheel = (e: React.WheelEvent) => {
    if (showMonthView) return
    const now = Date.now()
    if (now - lastWheel.current < 400) return
    const delta = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY
    if (Math.abs(delta) > 20) {
      if (delta > 0) {
        if (activeSlide < TOTAL_SLIDES - 1) {
          setActiveSlide(s => s + 1)
          lastWheel.current = now
        }
      } else {
        if (activeSlide > 0) {
          setActiveSlide(s => s - 1)
          lastWheel.current = now
        }
      }
    }
  }

  const renderHome = () => (
    <div className="slide slide-home">
      <div className="home-left">
        <div className="home-time">{now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</div>
        <div className="home-date">{now.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })}</div>
      </div>
      <div className="home-right">
        <div className="status-label">ACTIVE</div>
        <div className="status-app">
          <span>{windowInfo.appName}</span>
          <div className="status-dot" />
        </div>
        {windowInfo.title !== windowInfo.appName && windowInfo.title !== 'Desktop' && (
          <div className="status-sub">{windowInfo.title.slice(0, 30)}</div>
        )}
      </div>

    </div>
  )


  const renderMusic = () => {
    const brandColor = getSourceColor(media.source)
    let displaySource = media.source || "System"
    if (displaySource.includes('.')) displaySource = displaySource.split('.')[0]
    displaySource = displaySource.charAt(0).toUpperCase() + displaySource.slice(1)

    const progress = (media.duration && media.duration > 0)
      ? Math.min(100, Math.max(0, ((media.position || 0) / media.duration) * 100))
      : 0

    return (
      <div className="slide slide-music">
        <div className="music-art">
          <div className="art-glow" style={{ backgroundImage: thumbSrc ? `url(${thumbSrc})` : 'none' }} />
          <div className="art-box">
            {thumbSrc ? <img src={thumbSrc} alt="" /> : <div className="art-empty">♪</div>}
            {media.isPlaying && <div className="art-viz">{[0, 1, 2, 3].map(i => <div key={i} style={{ animationDelay: `${i * 0.1}s` }} />)}</div>}
          </div>
        </div>

        <div
          className="music-info"
          onMouseLeave={() => {
            if (showVolume && !draggingRef.current) {
              setShowVolume(false)
            }
          }}
        >
          {!showVolume ? (
            <>
              <div className="music-text-row">
                <div className="music-title">{media.title}</div>
                <div className="music-artist">{media.artist}</div>
              </div>

              <div className="music-mid-row">
                <div className="music-source">
                  <div style={{ width: 10, height: 10 }}>
                    <SourceIcon source={media.source} color={brandColor} />
                  </div>
                  <span>{displaySource}</span>
                </div>

                <div className="music-device-wrapper">
                  <div className="music-device">
                    <SmartDeviceIcon deviceName={media.device || ""} />
                  </div>
                  <div className="custom-tooltip">{media.device || "Speaker"}</div>
                </div>
              </div>

              {media.duration && media.duration > 0 ? (
                <div className="music-progress-container">
                  <div className="music-progress-track">
                    <div className="music-progress-fill" style={{ width: `${progress}%` }} />
                  </div>
                  <div className="music-time-labels">
                    <span>{formatTime(media.position || 0)}</span>
                    <span>{formatTime(media.duration)}</span>
                  </div>
                </div>
              ) : (
                <div style={{ height: 14 }} />
              )}

              <div className="music-controls">
                <button onClick={() => handleControl('prev')} type="button">
                  <svg width="10" height="10" fill="currentColor" viewBox="0 0 24 24"><path d="M6 6h2v12H6zm3.5 6l8.5 6V6z" /></svg>
                </button>
                <button onClick={() => handleControl('playpause')} className="main" type="button">
                  {media.isPlaying ?
                    <svg width="10" height="10" fill="currentColor" viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z" /></svg> :
                    <svg width="10" height="10" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" /></svg>
                  }
                </button>
                <button onClick={() => handleControl('next')} type="button">
                  <svg width="10" height="10" fill="currentColor" viewBox="0 0 24 24"><path d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z" /></svg>
                </button>
                <button onClick={() => setShowVolume(true)} type="button">
                  <svg width="12" height="12" fill="currentColor" viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z" /></svg>
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="volume-header"><span>Volume</span><span>{localVolume}%</span></div>
              <input type="range" min="0" max="100" value={localVolume} onChange={(e) => { setLocalVolume(parseInt(e.target.value)); window.cosmic?.setVolume(parseInt(e.target.value)) }} onPointerDown={() => draggingRef.current = true} onPointerUp={() => { draggingRef.current = false; setTimeout(() => setShowVolume(false), 1000) }} className="volume-slider" />
            </>
          )}
        </div>
      </div>
    )
  }


  const renderWeather = () => {
    const temp = weather?.temp ?? '--'
    const condition = weather?.condition ?? 'Loading'
    const isDay = weather?.isDay ?? true
    const city = weather?.city ?? 'Locating...'
    const wind = weather?.wind ?? 0
    const humidity = weather?.humidity ?? 0
    const precip = weather?.precip_prob ?? 0
    const snowfall = weather?.snowfall ?? 0

    const wmo = weather?.wmo ?? 0
    const isSevere = [95, 96, 99, 71, 73, 75, 85, 86].includes(wmo)
    const alertMessage = [95, 96, 99].includes(wmo) ? "Thunderstorm Alert" :
      [71, 73, 75, 85, 86].includes(wmo) ? "Heavy Snow Alert" : ""

    return (
      <div className="slide slide-weather-clean">
        <WeatherAnimation
          condition={condition}
          isDay={isDay}
          snowfall={snowfall}
          className="weather-particles"
        />

        <div className="weather-clean-content">
          <div className="weather-col-left">
            <div className="weather-temp-huge">{temp}°</div>
            <div className="weather-meta-row">
              <span className="weather-city-clean">{city}</span>
              <span className="weather-dot-sep">•</span>
              <span className="weather-cond-clean">{condition}</span>
            </div>
            {isSevere && (
              <div className="weather-alert-badge">
                ⚠️ {alertMessage}
              </div>
            )}
          </div>

          <div className="weather-col-right">
            {/* Wind */}
            <div className="stat-row">
              <svg className="stat-icon" viewBox="0 0 24 24" fill="currentColor">
                <path d="M12.79 20c-.11 0-.2-.09-.2-.2v-7.25l-2.07 2.07c-.08.08-.2.08-.28 0l-1.66-1.66a.19.19 0 0 1 0-.28L12 9.21l3.42 3.42c.08.08.08.2 0 .28l-1.66 1.66c-.08.08-.2.08-.28 0l-2.07-2.07v7.25c0 .11-.09.2-.2.2h-2.42z" />
              </svg>
              <div>
                <span className="stat-val">{wind}</span>
                <span className="stat-unit">km/h</span>
              </div>
            </div>

            {/* Humidity */}
            <div className="stat-row">
              <svg className="stat-icon" viewBox="0 0 24 24" fill="currentColor">
                <path d="M12 2c-5.33 4.55-8 8.48-8 11.8 0 4.98 3.8 8.2 8 8.2s8-3.22 8-8.2c0-3.32-2.67-7.25-8-11.8zm0 18c-3.31 0-6-2.63-6-6.2 0-2.61 2.43-5.98 6-9.59 3.57 3.61 6 6.98 6 9.59 0 3.57-2.69 6.2-6 6.2z" opacity="0.9" />
              </svg>
              <div>
                <span className="stat-val">{humidity}</span>
                <span className="stat-unit">%</span>
              </div>
            </div>

            {/* Rain Chance */}
            <div className="stat-row">
              <svg className="stat-icon" viewBox="0 0 24 24" fill="currentColor">
                <path d="M9 13c.55 0 1 .45 1 1s-.45 1-1 1-1-.45-1-1 .45-1 1-1zm3-3c.55 0 1 .45 1 1s-.45 1-1 1-1-.45-1-1 .45-1 1-1zm3 3c.55 0 1 .45 1 1s-.45 1-1 1-1-.45-1-1 .45-1 1-1z" />
                <path d="M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96zM19 18H6c-2.21 0-4-1.79-4-4 0-2.05 1.53-3.76 3.56-3.97l1.07-.11.5-.95C8.08 7.14 9.94 6 12 6c2.62 0 4.88 1.86 5.39 4.43l.3 1.5 1.53.11c1.56.1 2.78 1.41 2.78 2.96 0 1.65-1.35 3-3 3z" opacity="0.6" />
              </svg>
              <div>
                <span className="stat-val">{precip}</span>
                <span className="stat-unit">%</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    )
  }

  const [calendarData, setCalendarData] = useState<{ events: any[], email: string }>({ events: [], email: '' })
  const [showMonthView, setShowMonthView] = useState(false)

  // Add Listener
  useEffect(() => {
    if (!window.cosmic?.onCalendarUpdate) return
    const unsub = window.cosmic.onCalendarUpdate((data) => {
      setCalendarData(data)
    })
    return () => unsub?.()
  }, [])

  // --- NOTIFICATION LOGIC ---
  useEffect(() => {
    if (!calendarData.events.length) return

    // Check for events starting in exactly 5 minutes (approx window)
    const checkNotification = () => {
      const nowMs = Date.now()

      for (const evt of calendarData.events) {
        // Already notified?
        if (notifiedEventsRef.current.has(evt.id)) continue

        const startMs = new Date(evt.start).getTime()
        const diffMin = (startMs - nowMs) / 60000

        // Trigger if between 4.8 and 5.2 minutes away
        if (diffMin > 4.8 && diffMin < 5.2) {
          setNotificationEvent(evt)
          notifiedEventsRef.current.add(evt.id)
          setExpanded(true)

          // Auto dismiss after 10 seconds if user doesn't interact
          setTimeout(() => {
            setNotificationEvent(null)
            // Only collapse if we are not hovering/searching
            if (!searchActive && !hovered && !internalHover) {
              setExpanded(false)
            }
          }, 10000)

          break // Only one notification at a time
        }
      }
    }

    // Check every 15 seconds
    const interval = setInterval(checkNotification, 15000)
    return () => clearInterval(interval)
  }, [calendarData, searchActive, hovered, internalHover])

  const renderNotification = () => {
    if (!notificationEvent) return null
    const startTime = new Date(notificationEvent.start).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

    return (
      <div className="slide slide-notification" style={{ display: 'flex', flexDirection: 'column', height: '100%', padding: '20px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
          <div style={{ width: 32, height: 32, background: '#FF3B30', borderRadius: 8, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="white"><path d="M19 3h-1V1h-2v2H8V1H6v2H5c-1.11 0-2 .9-2 2v14c0 1.1.89 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16H5V8h14v11z" /></svg>
          </div>
          <div>
            <div style={{ fontSize: 12, color: 'rgba(255,255,255,0.6)', fontWeight: 500 }}>UPCOMING • 5 MIN</div>
            <div style={{ fontSize: 15, fontWeight: 600, color: '#fff' }}>{startTime}</div>
          </div>
        </div>

        <div style={{ fontSize: 18, fontWeight: 600, color: '#fff', marginBottom: 4, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {notificationEvent.summary}
        </div>

        <button
          onClick={(e) => { e.stopPropagation(); setNotificationEvent(null); }}
          style={{
            marginTop: 'auto',
            background: 'rgba(255,255,255,0.15)',
            border: 'none',
            color: 'white',
            padding: '8px',
            borderRadius: 8,
            fontSize: 13,
            fontWeight: 600,
            cursor: 'pointer',
            width: '100%'
          }}
        >
          Dismiss
        </button>
      </div>
    )
  }

  // --- OPACITY STATE ---
  // Managed by App.tsx now


  // Override 'expanded' style if Month View is open
  const islandStyle = showMonthView
    ? { width: '380px', height: '360px', borderRadius: '0 0 40px 40px' }
    : (notificationEvent ? { width: '300px', height: '160px' } : {})

  // Dynamic background style
  const dynamicBgStyle = {
    background: `rgba(0, 0, 0, ${islandOpacity})`
  }

  // ... renderCalendar function
  const renderCalendar = () => {
    if (showMonthView) {
      return <CalendarMonthView currentDate={now} events={calendarData.events} />
    }

    // Sort next few events - showing more now with scroll
    const upcoming = calendarData.events.slice(0, 10)
    const upcomingCountLabel = upcoming.length === 1 ? '1 upcoming' : `${upcoming.length} upcoming`

    const formatTimeSimple = (dateStr: string) => {
      const date = new Date(dateStr)
      if (Number.isNaN(date.getTime())) return '--'
      return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }).toLowerCase()
    }

    return (
      <div className="slide slide-calendar">
        <div className="cal-left">
          <button type="button" className="cal-today" onClick={() => setShowMonthView(true)} aria-label="Open month calendar">
            <div className="cal-header">
              <span>{now.toLocaleDateString([], { month: 'short' })}</span>
            </div>
            <div className="cal-body">
              <span>{now.getDate()}</span>
            </div>
          </button>
          <div className="cal-meta">
            <span className="dt-label">{now.toLocaleDateString([], { weekday: 'long' })}</span>
            <span className="cal-sub">{upcomingCountLabel}</span>
          </div>
        </div>

        <div className="cal-right">
          <div className="cal-events-list">
            {upcoming.length > 0 ? upcoming.map((evt, i) => (
              <div
                key={i}
                className="cal-task"
                tabIndex={0}
                role="button"
                onClick={() => setShowMonthView(true)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    setShowMonthView(true)
                  }
                }}
              >
                <div
                  className="task-bar"
                  style={{ backgroundColor: evt.colorId === '1' ? '#a4b0be' : '#007AFF' }}
                />
                <div className="task-content">
                  <span className="task-time">{formatTimeSimple(evt.start)}</span>
                  <span className="task-title">{evt.summary}</span>
                </div>
              </div>
            )) : (
              <div className="no-events">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="3" y="4" width="18" height="18" rx="2" ry="2" />
                  <line x1="16" y1="2" x2="16" y2="6" />
                  <line x1="8" y1="2" x2="8" y2="6" />
                  <line x1="3" y1="10" x2="21" y2="10" />
                </svg>
                <span>No upcoming events</span>
              </div>
            )}
          </div>

          {calendarData.email && (
            <div className="cal-email">
              {calendarData.email}
            </div>
          )}
        </div>
      </div>
    )
  }

  const renderUtilities = () => {
    return (
      <div className="slide slide-utilities">
        <div className="utility-wrapper">
          <div className="utility-item restart" onClick={() => window.cosmic?.restartApp()}>
            <div className="utility-circle">
              <RotateCw size={24} strokeWidth={2} />
            </div>
            <span className="utility-label">Restart</span>
          </div>

          <div className="utility-item shutdown" onClick={() => window.cosmic?.quitApp()}>
            <div className="utility-circle">
              <Power size={24} strokeWidth={2} />
            </div>
            <span className="utility-label">Shut Down</span>
          </div>
        </div>
      </div>
    )
  }

  const renderContent = () => {
    if (notificationEvent) return renderNotification()
    const type = slideContentMap[activeSlide]
    if (type === 'home') return renderHome()
    if (type === 'music') return renderMusic()
    if (type === 'weather') return renderWeather()
    if (type === 'utilities') return renderUtilities() // ADDED
    return renderCalendar()
  }

  return (
    <>
      <div
        className={`island ${expanded ? 'expanded' : ''}`}
        onMouseEnter={() => setInternalHover(true)}
        onMouseLeave={() => setInternalHover(false)}
        onWheel={onWheel}
        style={{
          ...dynamicBgStyle, // Apply background opacity here
          ...(expanded && (showMonthView || notificationEvent) ? islandStyle : {}),
          pointerEvents: 'auto'
        }}
      >
        {!expanded && <div className="notch"><div className="notch-bar" /></div>}

        {expanded && (
          <>
            {!showMonthView && !notificationEvent && (
              <>
                <div style={{ position: 'absolute', top: 0, bottom: '50px', left: 0, width: '40px', zIndex: 50, cursor: activeSlide > 0 ? 'w-resize' : 'default' }} onMouseEnter={() => switchSlide('prev')} />
                <div style={{ position: 'absolute', top: 0, bottom: '50px', right: 0, width: '40px', zIndex: 50, cursor: activeSlide < TOTAL_SLIDES - 1 ? 'e-resize' : 'default' }} onMouseEnter={() => switchSlide('next')} />
              </>
            )}

            <div className="island-content">
              {renderContent()}
            </div>

            {showMonthView && (
              <button
                onClick={() => setShowMonthView(false)}
                style={{ position: 'absolute', top: '20px', right: '20px', background: 'none', border: 'none', color: 'rgba(255,255,255,0.3)', cursor: 'pointer', zIndex: 100 }}
              >
                ✕
              </button>
            )}

            {!showMonthView && !notificationEvent && (
              <>
                <div className="island-anchor-container">
                  <button className={`anchor-btn ${isAnchored ? 'active' : ''}`} onClick={(e) => { e.stopPropagation(); setIsAnchored(!isAnchored) }}>
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d={isAnchored ? "M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z" : "M16 9h-1V7c0-1.66-1.34-3-3-3S9 5.34 9 7v2H8c-1.1 0-2 .9-2 2v8c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2v-8c0-1.1-.9-2-2-2zm-1 0H9V7c0-1.66 1.34-3 3-3s3 1.34 3 3v2z"} /></svg>
                  </button>
                </div>

                <div className="island-settings-container">
                  <button
                    className={`settings-btn ${showSettings ? 'active' : ''}`}
                    onClick={(e) => { e.stopPropagation(); setShowSettings(s => !s) }}
                  >
                    <svg width="12" height="12" fill="currentColor" viewBox="0 0 24 24"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32a.488.488 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 0 0-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 0 0-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z" /></svg>
                  </button>
                </div>

                <div className="island-dots">
                  {Array.from({ length: TOTAL_SLIDES }).map((_, idx) => (
                    <button key={idx} className={`dot ${activeSlide === idx ? 'active' : ''}`} onClick={(e) => { e.stopPropagation(); setActiveSlide(idx) }} type="button" />
                  ))}
                </div>
              </>
            )}
          </>
        )}
      </div>

      {showSettings && (
        <Settings
          isOpen={showSettings}
          searchPosition={searchPosition}
          onPositionChange={onPositionChange}
          staybackTime={staybackTime}
          onStaybackChange={onStaybackChange}
          onClose={() => setShowSettings(false)}
          keyStatus={keyStatus}
          googleEmail={calendarData.email} // Pass email prop
          islandOpacity={islandOpacity}
          onOpacityChange={onOpacityChange}
        />
      )}
    </>
  )
}

function formatTime(seconds: number) {
  const mins = Math.floor(seconds / 60)
  const secs = Math.floor(seconds % 60)
  return `${mins}:${secs.toString().padStart(2, '0')}`
}

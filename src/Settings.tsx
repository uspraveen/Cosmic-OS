import { useState, useEffect } from 'react'
import LiquidGlass from './LiquidGlass'
import MonitorSelector from './MonitorSelector'
import ApiConfiguration from './ApiConfiguration'
import type { SearchPosition } from './App'
import './settings.css'

interface SettingsProps {
  isOpen: boolean
  searchPosition: SearchPosition
  onPositionChange: (pos: SearchPosition) => void
  staybackTime: number
  onStaybackChange: (time: number) => void
  onClose: () => void
  keyStatus: { gemini: boolean; perplexity: boolean }
  googleEmail?: string
  islandOpacity: number
  onOpacityChange: (opacity: number) => void
}

export default function Settings({
  isOpen,
  searchPosition,
  onPositionChange,
  staybackTime,
  onStaybackChange,
  onClose,
  keyStatus,
  googleEmail,
  islandOpacity,
  onOpacityChange
}: SettingsProps) {

  useEffect(() => {
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleEsc)
    return () => window.removeEventListener('keydown', handleEsc)
  }, [onClose])

  const [currentView, setCurrentView] = useState<'main' | 'monitors' | 'api'>('main')

  useEffect(() => {
    if (!isOpen) setCurrentView('main')
  }, [isOpen])

  // Local state for the Calendar input box
  const [calUrl, setCalUrl] = useState('')

  const handleSaveCal = () => {
    if (calUrl.includes('calendar.google.com')) {
      // Using (window as any) here ensures it works even if you haven't 
      // updated your vite-env.d.ts file yet.
      (window as any).cosmic?.saveCalendarUrl(calUrl)
      setCalUrl('') // clear input after save
    }
  }

  if (!isOpen) return null

  return (
    <div className="settings-overlay" onMouseDown={(e) => {
      if (e.target === e.currentTarget) onClose()
    }}>
      <div className="settings-panel" onClick={(e) => e.stopPropagation()}>
        <LiquidGlass cornerRadius={20}>
          <div className="settings-content">

            {/* --- HEADER --- */}
            <div className="settings-header">
              {currentView === 'main' ? (
                <span>Settings</span>
              ) : (
                <button className="settings-back-btn" onClick={() => setCurrentView('main')}>
                  <span style={{ fontSize: 18, marginRight: 4 }}>‹</span> Back
                </button>
              )}
              <button className="close-btn" onClick={onClose}>✕</button>
            </div>

            {/* --- MAIN PAGE --- */}
            {currentView === 'main' && (
              <>
                {/* Position */}
                <div className="setting-row">
                  <span className="setting-label">Search Position</span>
                  <div className="toggle-group">
                    <button className={`toggle-btn ${searchPosition === 'bottom' ? 'active' : ''}`} onClick={() => onPositionChange('bottom')}>Bottom</button>
                    <button className={`toggle-btn ${searchPosition === 'middle' ? 'active' : ''}`} onClick={() => onPositionChange('middle')}>Middle</button>
                  </div>
                </div>

                {/* Stayback Time */}
                <div className="setting-row vertical">
                  <div className="setting-header-row">
                    <span className="setting-label">Stayback Time</span>
                    <span className="setting-value">{staybackTime}s</span>
                  </div>
                  <input type="range" min="0" max="10" value={staybackTime} onChange={(e) => onStaybackChange(parseInt(e.target.value))} className="settings-slider" />
                </div>

                {/* Island Opacity */}
                <div className="setting-row vertical">
                  <div className="setting-header-row">
                    <span className="setting-label">Island Opacity</span>
                    <span className="setting-value">{Math.round(islandOpacity * 100)}%</span>
                  </div>
                  <input
                    type="range"
                    min="0.2"
                    max="1"
                    step="0.05"
                    value={islandOpacity}
                    onChange={(e) => onOpacityChange(parseFloat(e.target.value))}
                    className="settings-slider"
                  />
                </div>

                {/* --- GOOGLE CALENDAR (iCal) SECTION --- */}
                <div className="setting-row vertical" style={{ borderTop: '1px solid rgba(255,255,255,0.1)', paddingTop: 16, width: '100%' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                    <span className="api-name">Google Calendar</span>
                    <span className={`api-badge ${googleEmail ? 'connected' : 'missing'}`}>
                      {googleEmail ? 'Linked' : 'Not Linked'}
                    </span>
                  </div>

                  <div style={{ fontSize: 10, color: 'rgba(255,255,255,0.5)', marginBottom: 8, lineHeight: '1.4' }}>
                    Paste your <b>Secret address in iCal format</b> here.<br />
                    (Settings &gt; Select Calendar &gt; Scroll to bottom)
                  </div>

                  <div style={{ display: 'flex', gap: 8, width: '100%' }}>
                    <input
                      type="text"
                      placeholder="https://calendar.google.com/..."
                      className="input"
                      style={{
                        background: 'rgba(255,255,255,0.05)',
                        padding: 8,
                        borderRadius: 6,
                        flex: 1,
                        fontSize: 12,
                        color: '#fff',
                        border: '1px solid rgba(255,255,255,0.1)'
                      }}
                      value={calUrl}
                      onChange={(e) => setCalUrl(e.target.value)}
                    />
                    <button className="edit-key-btn" onClick={handleSaveCal}>
                      Save
                    </button>
                  </div>
                </div>

                {/* NAVIGATION BUTTONS */}

                {/* API Keys Page Button */}
                <button className="setting-nav-btn" onClick={() => setCurrentView('api')}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ fontWeight: 600 }}>API Configuration</span>
                    <span style={{
                      fontSize: 10,
                      background: (keyStatus.gemini && keyStatus.perplexity) ? 'rgba(76, 175, 80, 0.2)' : 'rgba(255, 193, 7, 0.2)',
                      color: (keyStatus.gemini && keyStatus.perplexity) ? '#4caf50' : '#ffc107',
                      padding: '2px 6px',
                      borderRadius: 4
                    }}>
                      {(keyStatus.gemini && keyStatus.perplexity) ? 'All Set' : 'Action Needed'}
                    </span>
                  </div>
                  <span style={{ opacity: 0.5 }}>›</span>
                </button>

                {/* Multi-Monitor Display Selection Button */}
                <button className="setting-nav-btn" onClick={() => setCurrentView('monitors')} style={{ marginTop: 8 }}>
                  <span style={{ fontWeight: 600 }}>Display Preferences</span>
                  <span style={{ opacity: 0.5 }}>›</span>
                </button>

              </>
            )}

            {/* --- MONITORS SUB-PAGE --- */}
            {currentView === 'monitors' && (
              <div className="setting-subpage">
                <div style={{ fontSize: 13, color: 'rgba(255,255,255,0.6)', marginBottom: 16, lineHeight: '1.5' }}>
                  Select which monitor the app should appear on when triggered.
                  If the monitor is disconnected, we'll try to find a suitable fallback.
                </div>
                <MonitorSelector />
              </div>
            )}

            {/* --- API SUB-PAGE --- */}
            {currentView === 'api' && (
              <ApiConfiguration keyStatus={keyStatus} />
            )}

          </div>
        </LiquidGlass>
      </div>
    </div>
  )
}

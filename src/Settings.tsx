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

  const [currentView, setCurrentView] = useState<'main' | 'monitors' | 'api' | 'ui'>('main')

  useEffect(() => {
    if (!isOpen) setCurrentView('main')
  }, [isOpen])



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


                {/* NAVIGATION BUTTONS */}

                {/* UI Settings Page Button */}
                <button className="setting-nav-btn" onClick={() => setCurrentView('ui')}>
                  <span style={{ fontWeight: 600 }}>UI Settings</span>
                  <span style={{ opacity: 0.5 }}>›</span>
                </button>

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

            {/* --- UI SUB-PAGE --- */}
            {currentView === 'ui' && (
              <div className="setting-subpage ui-settings-page">
                <div className="ui-settings-intro">
                  Fine-tune where the island appears and how it behaves after interaction.
                </div>

                {/* Position */}
                <div className="ui-setting-card">
                  <div className="ui-setting-head">
                    <div>
                      <span className="setting-label">Search Position</span>
                      <div className="ui-setting-note">Choose where search opens on screen.</div>
                    </div>
                    <div className="toggle-group">
                      <button className={`toggle-btn ${searchPosition === 'bottom' ? 'active' : ''}`} onClick={() => onPositionChange('bottom')}>Bottom</button>
                      <button className={`toggle-btn ${searchPosition === 'middle' ? 'active' : ''}`} onClick={() => onPositionChange('middle')}>Middle</button>
                    </div>
                  </div>
                </div>

                {/* Stayback Time */}
                <div className="ui-setting-card">
                  <div className="setting-header-row">
                    <span className="setting-label">Stayback Time</span>
                    <span className="setting-value">{staybackTime}s</span>
                  </div>
                  <input type="range" min="0" max="10" value={staybackTime} onChange={(e) => onStaybackChange(parseInt(e.target.value))} className="settings-slider" />
                  <div className="slider-labels ui-slider-labels">
                    <span>Instant</span>
                    <span>10s linger</span>
                  </div>
                </div>

                {/* Island Opacity */}
                <div className="ui-setting-card">
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
                  <div className="slider-labels ui-slider-labels">
                    <span>20%</span>
                    <span>100%</span>
                  </div>
                </div>
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

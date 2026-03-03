import { useEffect, useState } from 'react'
import LiquidGlass from './LiquidGlass'
import MonitorSelector from './MonitorSelector'
import ApiConfiguration from './ApiConfiguration'
import GoogleIntegrationsSettings from './GoogleIntegrationsSettings'
import { GOOGLE_TOOL_DEFINITIONS } from './integrations'
import type { SearchPosition } from './App'
import './settings.css'

interface SettingsProps {
  isOpen: boolean
  searchPosition: SearchPosition
  onPositionChange: (pos: SearchPosition) => void
  staybackTime: number
  onStaybackChange: (time: number) => void
  onClose: () => void
  keyStatus: {
    gemini: boolean
    perplexity: boolean
    deepgram?: boolean
    groq?: boolean
    anthropic?: boolean
  }
  islandOpacity: number
  onOpacityChange: (opacity: number) => void
}

type SettingsView = 'main' | 'monitors' | 'api' | 'ui' | 'integrations' | 'integrations-google'

export default function Settings({
  isOpen,
  searchPosition,
  onPositionChange,
  staybackTime,
  onStaybackChange,
  onClose,
  keyStatus,
  islandOpacity,
  onOpacityChange,
}: SettingsProps) {
  const [currentView, setCurrentView] = useState<SettingsView>('main')

  useEffect(() => {
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleEsc)
    return () => window.removeEventListener('keydown', handleEsc)
  }, [onClose])

  useEffect(() => {
    if (!isOpen) setCurrentView('main')
  }, [isOpen])

  if (!isOpen) return null

  const viewTitle =
    currentView === 'main'
      ? 'Settings'
      : currentView === 'api'
        ? 'API Configuration'
        : currentView === 'monitors'
          ? 'Display Preferences'
          : currentView === 'ui'
            ? 'UI Settings'
            : currentView === 'integrations'
              ? 'Integrations'
              : 'Google'

  const handleBack = () => {
    if (currentView === 'integrations-google') {
      setCurrentView('integrations')
      return
    }
    setCurrentView('main')
  }

  const integrationsPreview = GOOGLE_TOOL_DEFINITIONS.map((tool) => tool.label).join(' • ')
  const allKeysConfigured =
    Boolean(keyStatus.gemini) &&
    Boolean(keyStatus.perplexity) &&
    Boolean(keyStatus.deepgram) &&
    Boolean(keyStatus.anthropic)

  return (
    <div
      className="settings-overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div className="settings-panel" onClick={(e) => e.stopPropagation()}>
        <LiquidGlass cornerRadius={20}>
          <div className="settings-content">
            <div className="settings-header">
              {currentView === 'main' ? (
                <span>Settings</span>
              ) : (
                <div className="settings-header-title">
                  <button className="settings-back-btn" onClick={handleBack}>
                    <span style={{ fontSize: 18, marginRight: 4 }}>‹</span> Back
                  </button>
                  <span>{viewTitle}</span>
                </div>
              )}
              <button className="close-btn" onClick={onClose}>
                ✕
              </button>
            </div>

            {currentView === 'main' && (
              <>
                <button className="setting-nav-btn" onClick={() => setCurrentView('integrations')}>
                  <div className="setting-nav-copy">
                    <span style={{ fontWeight: 600 }}>Integrations</span>
                    <span className="setting-nav-subcopy">
                      Google accounts, tool bundles, and future provider slots.
                    </span>
                  </div>
                  <span style={{ opacity: 0.5 }}>›</span>
                </button>

                <button className="setting-nav-btn" onClick={() => setCurrentView('api')}>
                  <div className="setting-nav-copy">
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <span style={{ fontWeight: 600 }}>API Configuration</span>
                      <span
                        style={{
                          fontSize: 10,
                          background: allKeysConfigured ? 'rgba(76, 175, 80, 0.2)' : 'rgba(255, 193, 7, 0.2)',
                          color: allKeysConfigured ? '#4caf50' : '#ffc107',
                          padding: '2px 6px',
                          borderRadius: 4,
                        }}
                      >
                        {allKeysConfigured ? 'All Set' : 'Action Needed'}
                      </span>
                    </div>
                    <span className="setting-nav-subcopy">Model, voice, and meeting provider keys.</span>
                  </div>
                  <span style={{ opacity: 0.5 }}>›</span>
                </button>

                <button className="setting-nav-btn" onClick={() => setCurrentView('monitors')}>
                  <div className="setting-nav-copy">
                    <span style={{ fontWeight: 600 }}>Display Preferences</span>
                    <span className="setting-nav-subcopy">Choose which monitor Cosmic should target.</span>
                  </div>
                  <span style={{ opacity: 0.5 }}>›</span>
                </button>

                <button className="setting-nav-btn" onClick={() => setCurrentView('ui')}>
                  <div className="setting-nav-copy">
                    <span style={{ fontWeight: 600 }}>UI Settings</span>
                    <span className="setting-nav-subcopy">Position, linger timing, and island transparency.</span>
                  </div>
                  <span style={{ opacity: 0.5 }}>›</span>
                </button>
              </>
            )}

            {currentView === 'integrations' && (
              <div className="setting-subpage prx-integrations-page">
                <div className="prx-intro">
                  <p>Connect accounts once and let Cosmic use them across supported apps.</p>
                </div>

                <button className="prx-provider-card" onClick={() => setCurrentView('integrations-google')}>
                  <div className="prx-provider-icon">
                    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                      <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4"/>
                      <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>
                      <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/>
                      <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>
                    </svg>
                  </div>
                  <div className="prx-provider-info">
                    <strong>Google Workspace</strong>
                    <span>{integrationsPreview}</span>
                  </div>
                  <div className="prx-provider-arrow">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m9 18 6-6-6-6"/></svg>
                  </div>
                </button>
              </div>
            )}

            {currentView === 'integrations-google' && (
              <GoogleIntegrationsSettings active={currentView === 'integrations-google'} />
            )}

            {currentView === 'monitors' && (
              <div className="setting-subpage">
                <div style={{ fontSize: 13, color: 'rgba(255,255,255,0.6)', marginBottom: 16, lineHeight: '1.5' }}>
                  Select which monitor the app should appear on when triggered. If the monitor is disconnected, Cosmic
                  will fall back to a suitable display.
                </div>
                <MonitorSelector />
              </div>
            )}

            {currentView === 'ui' && (
              <div className="setting-subpage ui-settings-page">
                <div className="ui-settings-intro">
                  Fine-tune where the island appears and how it behaves after interaction.
                </div>

                <div className="ui-setting-card">
                  <div className="ui-setting-head">
                    <div>
                      <span className="setting-label">Search Position</span>
                      <div className="ui-setting-note">Choose where search opens on screen.</div>
                    </div>
                    <div className="toggle-group">
                      <button
                        className={`toggle-btn ${searchPosition === 'bottom' ? 'active' : ''}`}
                        onClick={() => onPositionChange('bottom')}
                      >
                        Bottom
                      </button>
                      <button
                        className={`toggle-btn ${searchPosition === 'middle' ? 'active' : ''}`}
                        onClick={() => onPositionChange('middle')}
                      >
                        Middle
                      </button>
                    </div>
                  </div>
                </div>

                <div className="ui-setting-card">
                  <div className="setting-header-row">
                    <span className="setting-label">Stayback Time</span>
                    <span className="setting-value">{staybackTime}s</span>
                  </div>
                  <input
                    type="range"
                    min="0"
                    max="10"
                    value={staybackTime}
                    onChange={(e) => onStaybackChange(parseInt(e.target.value))}
                    className="settings-slider"
                  />
                  <div className="slider-labels ui-slider-labels">
                    <span>Instant</span>
                    <span>10s linger</span>
                  </div>
                </div>

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

            {currentView === 'api' && <ApiConfiguration keyStatus={keyStatus} />}
          </div>
        </LiquidGlass>
      </div>
    </div>
  )
}

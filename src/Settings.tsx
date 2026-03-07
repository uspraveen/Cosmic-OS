import { useEffect, useState } from 'react'
import LiquidGlass from './LiquidGlass'
import MonitorSelector from './MonitorSelector'
import ApiConfiguration from './ApiConfiguration'
import GoogleIntegrationsSettings from './GoogleIntegrationsSettings'
import WhatsAppIntegrationSettings from './WhatsAppIntegrationSettings'
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
  authData?: any
  onLogout?: () => void
}

type SettingsView = 'main' | 'monitors' | 'api' | 'ui' | 'integrations' | 'integrations-google' | 'integrations-whatsapp'

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
  authData,
  onLogout,
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
              : currentView === 'integrations-whatsapp'
                ? 'WhatsApp'
                : 'Google'

  const handleBack = () => {
    if (currentView === 'integrations-google' || currentView === 'integrations-whatsapp') {
      setCurrentView('integrations')
      return
    }
    setCurrentView('main')
  }

  const integrationsPreview = GOOGLE_TOOL_DEFINITIONS.map((tool) => tool.label).join(' • ')
  const allKeysConfigured =
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
                {authData && (
                  <div style={{
                    padding: '14px 16px',
                    background: 'rgba(255,255,255,0.03)',
                    border: '1px solid rgba(255,255,255,0.08)',
                    borderRadius: 12,
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    marginBottom: 8,
                  }}>
                    <div>
                      <div style={{ color: '#fff', fontWeight: 600, fontSize: 14 }}>
                        {authData.fullName || 'Cosmic User'}
                      </div>
                      <div style={{ color: 'rgba(255,255,255,0.45)', fontSize: 12, marginTop: 2 }}>
                        Connected to VM
                      </div>
                    </div>
                    <button
                      onClick={() => onLogout?.()}
                      style={{
                        padding: '6px 14px',
                        background: 'rgba(255, 69, 58, 0.15)',
                        color: '#ff6b6b',
                        border: 'none',
                        borderRadius: 8,
                        fontSize: 12,
                        fontWeight: 600,
                        cursor: 'pointer',
                      }}
                    >
                      Log out
                    </button>
                  </div>
                )}

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
                    <span className="setting-nav-subcopy">Local voice and meeting provider keys. Cosmic chat uses your VM backend.</span>
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
                      <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4" />
                      <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853" />
                      <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05" />
                      <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335" />
                    </svg>
                  </div>
                  <div className="prx-provider-info">
                    <strong>Google Workspace</strong>
                    <span>{integrationsPreview}</span>
                  </div>
                  <div className="prx-provider-arrow">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m9 18 6-6-6-6" /></svg>
                  </div>
                </button>

                <button className="prx-provider-card" onClick={() => setCurrentView('integrations-whatsapp')} style={{ marginTop: '12px' }}>
                  <div className="prx-provider-icon" style={{ background: '#25D366' }}>
                    <svg width="22" height="22" viewBox="0 0 24 24" fill="white" xmlns="http://www.w3.org/2000/svg">
                      <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.888-.788-1.489-1.761-1.663-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51a12.8 12.8 0 0 0-.57-.01c-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 0 1-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 0 1-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 0 1 2.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0 0 12.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 0 0 5.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 0 0-3.48-8.413z" />
                    </svg>
                  </div>
                  <div className="prx-provider-info">
                    <strong>WhatsApp <span style={{ fontSize: '10px', background: 'rgba(255,255,255,0.1)', padding: '2px 6px', borderRadius: '4px', marginLeft: '4px', verticalAlign: 'middle' }}>Beta</span></strong>
                    <span>Connect your WhatsApp number</span>
                  </div>
                  <div className="prx-provider-arrow">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m9 18 6-6-6-6" /></svg>
                  </div>
                </button>
              </div>
            )}

            {currentView === 'integrations-google' && (
              <GoogleIntegrationsSettings active={currentView === 'integrations-google'} />
            )}

            {currentView === 'integrations-whatsapp' && (
              <WhatsAppIntegrationSettings
                active={currentView === 'integrations-whatsapp'}
                cosmicAuth={authData ? { gatewayUrl: authData.gatewayUrl, gatewayApiToken: authData.gatewayApiToken } : undefined}
              />
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

import { useEffect, useState } from 'react'
import {
  AlertCircle,
  Bot,
  ChevronRight,
  KeyRound,
  Monitor,
  Plug,
  Settings2,
  SlidersHorizontal,
  Smartphone,
  type LucideIcon,
} from 'lucide-react'
import { CursorMark, OpenAIMark, OpenCodeMark, ZCodeMark } from './brandIcons'
import LiquidGlass from './LiquidGlass'
import { SPACES_BLACK_GLASS_BACKGROUND, SPACES_BLACK_GLASS_BACKDROP } from './glassTones'
import MonitorSelector from './MonitorSelector'
import ApiConfiguration from './ApiConfiguration'
import GoogleIntegrationsSettings from './GoogleIntegrationsSettings'
import WhatsAppIntegrationSettings from './WhatsAppIntegrationSettings'
import TelegramIntegrationSettings from './TelegramIntegrationSettings'
import GitHubIntegrationSettings from './GitHubIntegrationSettings'
import MobileDevicesSettings from './MobileDevicesSettings'
import GatewayPreferencesSettings from './GatewayPreferencesSettings'
import CodexAgentSettings from './CodexAgentSettings'
import CursorAgentSettings from './CursorAgentSettings'
import OpenCodeAgentSettings from './OpenCodeAgentSettings'
import ZCodeAgentSettings from './ZCodeAgentSettings'
import { GOOGLE_TOOL_DEFINITIONS } from './integrations'
import type { SearchPosition } from './App'
import './settings.css'

export type AlphaPreferredHarness = 'opencode' | 'codex' | 'cursor' | 'zcode'

function normalizeAlphaHarness(value: unknown): AlphaPreferredHarness {
  const normalized = String(value ?? '').trim().toLowerCase()
  if (normalized === 'cursor') return 'cursor'
  if (normalized === 'codex') return 'codex'
  if (normalized === 'zcode') return 'zcode'
  return 'opencode'
}

type MainSettingsNavItem = {
  view: 'integrations' | 'agents' | 'api' | 'devices' | 'monitors' | 'ui' | 'preferences'
  title: string
  description: string
  icon: LucideIcon
}

const MAIN_SETTINGS_GROUPS: Array<{ label: string; items: MainSettingsNavItem[] }> = [
  {
    label: 'Connections',
    items: [
      {
        view: 'integrations',
        title: 'Integrations',
        description: 'Google accounts, tool bundles, and future provider slots.',
        icon: Plug,
      },
      {
        view: 'agents',
        title: 'Agents',
        description: 'Configure Alpha agent providers and coding backends.',
        icon: Bot,
      },
      {
        view: 'api',
        title: 'API Configuration',
        description: 'Local voice and meeting provider keys. Cosmic chat uses your VM backend.',
        icon: KeyRound,
      },
    ],
  },
  {
    label: 'This Device',
    items: [
      {
        view: 'devices',
        title: 'Mobile Devices',
        description: 'Review linked phones and remove access device-by-device or all at once.',
        icon: Smartphone,
      },
      {
        view: 'monitors',
        title: 'Display Preferences',
        description: 'Pin Cosmic to one monitor or leave it on Automatic.',
        icon: Monitor,
      },
      {
        view: 'ui',
        title: 'UI Settings',
        description: 'Position, chat width, linger timing, and island transparency.',
        icon: SlidersHorizontal,
      },
    ],
  },
  {
    label: 'General',
    items: [
      {
        view: 'preferences',
        title: 'Preferences',
        description: 'VM-wide response behavior and future backend-backed product preferences.',
        icon: Settings2,
      },
    ],
  },
]

function accountInitials(name: string) {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  if (!parts.length) return 'C'
  return parts
    .slice(0, 2)
    .map((part) => part.charAt(0).toUpperCase())
    .join('')
}

interface SettingsProps {
  isOpen: boolean
  searchPosition: SearchPosition
  onPositionChange: (pos: SearchPosition) => void
  staybackTime: number
  onStaybackChange: (time: number) => void
  onClose: () => void
  keyStatus: {
    haiku: boolean
    perplexity: boolean
    deepgram?: boolean
    groq?: boolean
    anthropic?: boolean
  }
  islandOpacity: number
  onOpacityChange: (opacity: number) => void
  chatWideMode: boolean
  onChatWideModeChange: (wide: boolean) => void
  authData?: {
    fullName?: string
    gatewayUrl?: string
    gatewayApiToken?: string
  }
  gatewayConnection?: {
    state: 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'error'
    connected: boolean
    detail?: string
  }
  onLogout?: () => void
  initialView?: SettingsView
  authAttentionCount?: number
}

export type SettingsView =
  | 'main'
  | 'monitors'
  | 'api'
  | 'preferences'
  | 'ui'
  | 'devices'
  | 'agents'
  | 'agents-codex'
  | 'agents-cursor'
  | 'agents-opencode'
  | 'agents-zcode'
  | 'integrations'
  | 'integrations-google'
  | 'integrations-whatsapp'
  | 'integrations-telegram'
  | 'integrations-github'

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
  chatWideMode,
  onChatWideModeChange,
  authData,
  gatewayConnection,
  onLogout,
  initialView,
  authAttentionCount = 0,
}: SettingsProps) {
  const [currentView, setCurrentView] = useState<SettingsView>(initialView || 'main')
  const [alphaPreferredHarness, setAlphaPreferredHarness] = useState<AlphaPreferredHarness>('opencode')
  const [alphaConfigLoading, setAlphaConfigLoading] = useState(false)
  const [alphaConfigError, setAlphaConfigError] = useState('')

  useEffect(() => {
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleEsc)
    return () => window.removeEventListener('keydown', handleEsc)
  }, [onClose])

  // A connect/login button whose whole job is to hand the user to the browser
  // should not leave the panel sitting on top of it. Main announces every
  // browser hand-off, so this covers Google, Codex, Cursor, Telegram and
  // anything added later without each one having to opt in.
  useEffect(() => {
    if (!isOpen) return
    return window.cosmic?.onExternalOpened?.(() => onClose())
  }, [isOpen, onClose])

  useEffect(() => {
    if (!isOpen) setCurrentView('main')
  }, [isOpen])

  useEffect(() => {
    if (isOpen && initialView) setCurrentView(initialView)
  }, [isOpen, initialView])

  useEffect(() => {
    if (!isOpen || currentView !== 'agents') return
    let cancelled = false
    const loadAlphaConfig = async () => {
      setAlphaConfigLoading(true)
      setAlphaConfigError('')
      try {
        const config = await window.cosmic?.getGatewayAlphaAgentConfig()
        const harness = String(config?.preferred_harness || '').trim().toLowerCase()
        if (!cancelled) setAlphaPreferredHarness(normalizeAlphaHarness(harness))
      } catch (err) {
        if (!cancelled) setAlphaConfigError(err instanceof Error ? err.message : 'Unable to load Alpha provider selection.')
      } finally {
        if (!cancelled) setAlphaConfigLoading(false)
      }
    }
    void loadAlphaConfig()
    return () => {
      cancelled = true
    }
  }, [isOpen, currentView])

  if (!isOpen) return null

  const viewTitle =
    currentView === 'main'
      ? 'Settings'
      : currentView === 'api'
        ? 'API Configuration'
      : currentView === 'monitors'
          ? 'Display Preferences'
          : currentView === 'preferences'
            ? 'Preferences'
          : currentView === 'ui'
            ? 'UI Settings'
            : currentView === 'devices'
              ? 'Mobile Devices'
            : currentView === 'agents'
              ? 'Agents'
            : currentView === 'agents-codex'
              ? 'Codex'
            : currentView === 'agents-cursor'
              ? 'Cursor'
            : currentView === 'agents-opencode'
              ? 'OpenCode'
            : currentView === 'agents-zcode'
              ? 'ZCode'
            : currentView === 'integrations'
              ? 'Integrations'
              : currentView === 'integrations-whatsapp'
                ? 'WhatsApp'
                : currentView === 'integrations-telegram'
                  ? 'Telegram'
                : currentView === 'integrations-github'
                  ? 'GitHub'
                : 'Google'

  const handleBack = () => {
    if (
      currentView === 'integrations-google' ||
      currentView === 'integrations-whatsapp' ||
      currentView === 'integrations-telegram' ||
      currentView === 'integrations-github'
    ) {
      setCurrentView('integrations')
      return
    }
    if (currentView === 'agents-codex' || currentView === 'agents-cursor' || currentView === 'agents-opencode' || currentView === 'agents-zcode') {
      setCurrentView('agents')
      return
    }
    setCurrentView('main')
  }

  const integrationsPreview = GOOGLE_TOOL_DEFINITIONS.map((tool) => tool.label).join(' • ')
  const hasAuthAttention = authAttentionCount > 0
  const allKeysConfigured =
    Boolean(keyStatus.deepgram) &&
    Boolean(keyStatus.anthropic)
  const gatewayConnectionLabel = gatewayConnection?.connected
    ? 'Gateway live'
    : gatewayConnection?.state === 'connecting' || gatewayConnection?.state === 'reconnecting'
      ? 'Connecting to VM'
      : gatewayConnection?.state === 'error'
        ? 'Gateway unavailable'
        : 'Signed in to VM'
  const saveAlphaPreferredHarness = async (preferredHarness: AlphaPreferredHarness) => {
    setAlphaPreferredHarness(preferredHarness)
    setAlphaConfigError('')
    setAlphaConfigLoading(true)
    try {
      const config = await window.cosmic?.saveGatewayAlphaAgentConfig({ preferredHarness })
      const harness = String(config?.preferred_harness || preferredHarness).trim().toLowerCase()
      setAlphaPreferredHarness(normalizeAlphaHarness(harness))
    } catch (err) {
      setAlphaConfigError(err instanceof Error ? err.message : 'Unable to save Alpha provider selection.')
    } finally {
      setAlphaConfigLoading(false)
    }
  }

  return (
    <div
      className="settings-overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div className="settings-panel" onClick={(e) => e.stopPropagation()}>
        <LiquidGlass
          cornerRadius={20}
          bodyBackground={SPACES_BLACK_GLASS_BACKGROUND}
          bodyBackdropFilter={SPACES_BLACK_GLASS_BACKDROP}
        >
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
              <div className="settings-main-page">
                {authData && (
                  <section className="settings-main-section">
                    <div className="settings-account-card">
                      <div className="settings-account-avatar" aria-hidden="true">
                        {accountInitials(authData.fullName || 'Cosmic User')}
                      </div>
                      <div className="settings-account-copy">
                        <div className="settings-account-name">
                          {authData.fullName || 'Cosmic User'}
                        </div>
                        <div className="settings-account-meta">
                          <span
                            className={`settings-account-status ${gatewayConnection?.connected ? 'on' : ''}`}
                            aria-hidden="true"
                          />
                          {gatewayConnectionLabel}
                        </div>
                      </div>
                    </div>
                  </section>
                )}

                {MAIN_SETTINGS_GROUPS.map((group) => (
                  <section key={group.label} className="settings-main-section">
                    <h3 className="settings-section-label">{group.label}</h3>
                    <div className="settings-inset-group" role="list">
                      {group.items.map((item) => {
                        const Icon = item.icon
                        const trailing =
                          item.view === 'integrations' && hasAuthAttention
                            ? (
                              <span className="setting-nav-trailing warn">Action needed</span>
                            )
                            : item.view === 'api'
                              ? (
                                <span className={`setting-nav-trailing ${allKeysConfigured ? 'ready' : 'warn'}`}>
                                  {allKeysConfigured ? 'All set' : 'Action needed'}
                                </span>
                              )
                              : null

                        return (
                          <button
                            key={item.view}
                            type="button"
                            className="setting-nav-btn"
                            role="listitem"
                            onClick={() => setCurrentView(item.view)}
                          >
                            <span className="setting-nav-icon" aria-hidden="true">
                              <Icon size={16} strokeWidth={2} />
                            </span>
                            <div className="setting-nav-copy">
                              <span className="setting-nav-title">{item.title}</span>
                              <span className="setting-nav-subcopy">{item.description}</span>
                            </div>
                            <div className="setting-nav-trailing-slot">
                              {trailing}
                              <ChevronRight className="setting-nav-chevron" size={14} strokeWidth={2.25} aria-hidden="true" />
                            </div>
                          </button>
                        )
                      })}
                    </div>
                  </section>
                ))}

                {authData ? (
                  <section className="settings-main-section">
                    <div className="settings-inset-group settings-signout-group" role="list">
                      <button
                        type="button"
                        className="setting-nav-btn destructive"
                        role="listitem"
                        onClick={() => onLogout?.()}
                      >
                        Sign Out
                      </button>
                    </div>
                  </section>
                ) : null}
              </div>
            )}

            {currentView === 'integrations' && (
              <div className="setting-subpage prx-integrations-page">
                <div className="prx-intro">
                  <p>Connect accounts once and let Cosmic use them across supported apps.</p>
                  {hasAuthAttention ? (
                    <div className="prx-attention-callout" role="status">
                      <span className="prx-attention-icon" aria-hidden="true">
                        <AlertCircle size={15} strokeWidth={2.1} />
                      </span>
                      <div className="prx-attention-copy">
                        <strong>
                          {authAttentionCount === 1
                            ? '1 account needs attention'
                            : `${authAttentionCount} accounts need attention`}
                        </strong>
                        <span>Reconnect the highlighted integration below to restore access on your VM.</span>
                      </div>
                    </div>
                  ) : null}
                </div>

                <button className={`prx-provider-card ${hasAuthAttention ? 'needs-attention' : ''}`} onClick={() => setCurrentView('integrations-google')}>
                  <div className="prx-provider-icon">
                    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                      <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4" />
                      <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853" />
                      <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05" />
                      <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335" />
                    </svg>
                  </div>
                  <div className="prx-provider-info">
                    <div className="settings-nav-title-row">
                      <strong>Google Workspace</strong>
                      {hasAuthAttention && <span className="settings-attention-pill compact">Reconnect</span>}
                    </div>
                    <span>{integrationsPreview}</span>
                  </div>
                  <div className="prx-provider-arrow">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m9 18 6-6-6-6" /></svg>
                  </div>
                </button>

                <button className="prx-provider-card" onClick={() => setCurrentView('integrations-github')} style={{ marginTop: '12px' }}>
                  <div className="prx-provider-icon" style={{ background: '#24292f' }}>
                    <svg width="22" height="22" viewBox="0 0 24 24" fill="white" xmlns="http://www.w3.org/2000/svg">
                      <path d="M12 .5C5.73.5.5 5.73.5 12c0 5.08 3.29 9.39 7.86 10.91.58.11.79-.25.79-.56 0-.28-.01-1.02-.02-2-3.2.7-3.88-1.54-3.88-1.54-.52-1.33-1.28-1.69-1.28-1.69-1.05-.72.08-.7.08-.7 1.16.08 1.77 1.19 1.77 1.19 1.03 1.77 2.7 1.26 3.36.96.1-.75.4-1.26.73-1.55-2.55-.29-5.24-1.28-5.24-5.68 0-1.25.45-2.28 1.19-3.08-.12-.29-.52-1.46.11-3.05 0 0 .97-.31 3.18 1.18a11 11 0 0 1 5.79 0c2.2-1.49 3.17-1.18 3.17-1.18.63 1.59.23 2.76.12 3.05.74.8 1.18 1.83 1.18 3.08 0 4.41-2.69 5.38-5.25 5.67.41.35.78 1.05.78 2.12 0 1.53-.01 2.77-.01 3.15 0 .31.21.68.8.56A11.51 11.51 0 0 0 23.5 12C23.5 5.73 18.27.5 12 .5z" />
                    </svg>
                  </div>
                  <div className="prx-provider-info">
                    <strong>GitHub</strong>
                    <span>Let Alpha work in the repositories you choose — commit, push, open PRs.</span>
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

                <button className="prx-provider-card" onClick={() => setCurrentView('integrations-telegram')} style={{ marginTop: '12px' }}>
                  <div className="prx-provider-icon" style={{ background: '#229ED9' }}>
                    <svg width="22" height="22" viewBox="0 0 24 24" fill="white" xmlns="http://www.w3.org/2000/svg">
                      <path d="M21.944 4.507c.315-.982-.278-1.506-1.22-1.167L3.54 10.016c-.953.37-.939.902-.173 1.137l4.422 1.38 10.233-6.456c.483-.294.925-.136.563.186l-8.292 7.483-.311 4.469c.456 0 .657-.208.912-.454l2.21-2.148 4.598 3.395c.847.468 1.457.227 1.669-.786l2.573-12.215z" />
                    </svg>
                  </div>
                  <div className="prx-provider-info">
                    <strong>Telegram</strong>
                    <span>Manage the per-VM bot, webhook health, and linked private DM.</span>
                  </div>
                  <div className="prx-provider-arrow">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m9 18 6-6-6-6" /></svg>
                  </div>
                </button>
              </div>
            )}

            {currentView === 'agents' && (
              <div className="cosmic-agents-page">
                <div className="cosmic-agents-hero">
                  <span className="cosmic-agents-hero-mark" aria-hidden="true">
                    {/* Alpha's mark elsewhere in the app (AgentGlyph terminal). */}
                    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.85} strokeLinecap="round" strokeLinejoin="round">
                      <path d="M3.8 6.2 9.6 12l-5.8 5.8" />
                      <path d="M12.4 17.8h7.8" />
                    </svg>
                  </span>
                  <div className="cosmic-agents-hero-copy">
                    <h3>Alpha Agents</h3>
                    <p>Connect coding providers that Alpha can use for project-level work on your VM.</p>
                  </div>
                </div>

                <div className="cosmic-alpha-provider-switch">
                  <div className="cosmic-alpha-provider-switch-copy">
                    <span className="cosmic-agents-section-label">Default Alpha Runner</span>
                    <strong>
                      {alphaPreferredHarness === 'cursor'
                        ? 'Cursor CLI'
                        : alphaPreferredHarness === 'codex'
                          ? 'Codex'
                          : alphaPreferredHarness === 'zcode'
                            ? 'ZCode'
                            : 'OpenCode'}
                    </strong>
                    <small>Used when the orchestrator delegates `alpha.execute` without an explicit harness.</small>
                  </div>
                  <div className="cosmic-alpha-provider-toggle" role="group" aria-label="Default Alpha runner">
                    <button
                      type="button"
                      className={alphaPreferredHarness === 'opencode' ? 'active' : ''}
                      onClick={() => void saveAlphaPreferredHarness('opencode')}
                      disabled={alphaConfigLoading}
                    >
                      <OpenCodeMark size={13} tone={alphaPreferredHarness === 'opencode' ? 'dark' : 'light'} />
                      OpenCode
                    </button>
                    <button
                      type="button"
                      className={alphaPreferredHarness === 'codex' ? 'active' : ''}
                      onClick={() => void saveAlphaPreferredHarness('codex')}
                      disabled={alphaConfigLoading}
                    >
                      <OpenAIMark size={13} />
                      Codex
                    </button>
                    <button
                      type="button"
                      className={alphaPreferredHarness === 'cursor' ? 'active' : ''}
                      onClick={() => void saveAlphaPreferredHarness('cursor')}
                      disabled={alphaConfigLoading}
                    >
                      <CursorMark size={13} mono={alphaPreferredHarness === 'cursor'} />
                      Cursor
                    </button>
                    <button
                      type="button"
                      className={alphaPreferredHarness === 'zcode' ? 'active' : ''}
                      onClick={() => void saveAlphaPreferredHarness('zcode')}
                      disabled={alphaConfigLoading}
                    >
                      <ZCodeMark size={13} />
                      ZCode
                    </button>
                  </div>
                </div>
                {alphaConfigError ? <div className="cosmic-agents-inline-error">{alphaConfigError}</div> : null}

                <div className="cosmic-agents-section-label">Active Providers</div>
                <button className="cosmic-agents-provider-card" onClick={() => setCurrentView('agents-opencode')}>
                  <div className="cosmic-agents-provider-icon" aria-hidden="true">
                    <OpenCodeMark size={22} />
                  </div>
                  <div className="cosmic-agents-provider-body">
                    <div className="cosmic-agents-provider-head">
                      <strong>OpenCode</strong>
                      <div className="cosmic-agents-provider-badges">
                        <span className="cosmic-agents-provider-tag default">Default</span>
                        <span className={`cosmic-agents-provider-tag ${alphaPreferredHarness === 'opencode' ? 'ready' : 'pending'}`}>
                          {alphaPreferredHarness === 'opencode' ? 'Selected' : 'Available'}
                        </span>
                      </div>
                    </div>
                    <span className="cosmic-agents-provider-desc">Connect models with API keys from 15+ providers.</span>
                  </div>
                  <div className="cosmic-agents-provider-arrow" aria-hidden="true">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m9 18 6-6-6-6" /></svg>
                  </div>
                </button>

                <button className="cosmic-agents-provider-card" onClick={() => setCurrentView('agents-codex')}>
                  <div className="cosmic-agents-provider-icon" aria-hidden="true">
                    <OpenAIMark size={22} />
                  </div>
                  <div className="cosmic-agents-provider-body">
                    <div className="cosmic-agents-provider-head">
                      <strong>Codex</strong>
                      <div className="cosmic-agents-provider-badges">
                        <span className={`cosmic-agents-provider-tag ${alphaPreferredHarness === 'codex' ? 'ready' : 'pending'}`}>
                          {alphaPreferredHarness === 'codex' ? 'Selected' : 'Available'}
                        </span>
                      </div>
                    </div>
                    <span className="cosmic-agents-provider-desc">Connect to Codex using API or OAuth.</span>
                  </div>
                  <div className="cosmic-agents-provider-arrow" aria-hidden="true">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m9 18 6-6-6-6" /></svg>
                  </div>
                </button>

                <button className="cosmic-agents-provider-card" onClick={() => setCurrentView('agents-cursor')}>
                  <div className="cosmic-agents-provider-icon cursor" aria-hidden="true">
                    <CursorMark size={22} />
                  </div>
                  <div className="cosmic-agents-provider-body">
                    <div className="cosmic-agents-provider-head">
                      <strong>Cursor CLI</strong>
                      <div className="cosmic-agents-provider-badges">
                        <span className={`cosmic-agents-provider-tag ${alphaPreferredHarness === 'cursor' ? 'ready' : 'pending'}`}>
                          {alphaPreferredHarness === 'cursor' ? 'Selected' : 'Available'}
                        </span>
                      </div>
                    </div>
                    <span className="cosmic-agents-provider-desc">Connect your Cursor account.</span>
                  </div>
                  <div className="cosmic-agents-provider-arrow" aria-hidden="true">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m9 18 6-6-6-6" /></svg>
                  </div>
                </button>

                <button className="cosmic-agents-provider-card" onClick={() => setCurrentView('agents-zcode')}>
                  <div className="cosmic-agents-provider-icon" aria-hidden="true">
                    <ZCodeMark size={22} />
                  </div>
                  <div className="cosmic-agents-provider-body">
                    <div className="cosmic-agents-provider-head">
                      <strong>ZCode</strong>
                      <div className="cosmic-agents-provider-badges">
                        <span className={`cosmic-agents-provider-tag ${alphaPreferredHarness === 'zcode' ? 'ready' : 'pending'}`}>
                          {alphaPreferredHarness === 'zcode' ? 'Selected' : 'Available'}
                        </span>
                      </div>
                    </div>
                    <span className="cosmic-agents-provider-desc">Connect Z.ai for GLM-5.3 and GLM-5.3 Flash.</span>
                  </div>
                  <div className="cosmic-agents-provider-arrow" aria-hidden="true">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m9 18 6-6-6-6" /></svg>
                  </div>
                </button>
              </div>
            )}

            {currentView === 'agents-codex' && (
              <CodexAgentSettings active={currentView === 'agents-codex'} />
            )}

            {currentView === 'agents-cursor' && (
              <CursorAgentSettings active={currentView === 'agents-cursor'} />
            )}

            {currentView === 'agents-opencode' && (
              <OpenCodeAgentSettings active={currentView === 'agents-opencode'} />
            )}

            {currentView === 'agents-zcode' && (
              <ZCodeAgentSettings active={currentView === 'agents-zcode'} />
            )}

            {currentView === 'integrations-google' && (
              <GoogleIntegrationsSettings active={currentView === 'integrations-google'} />
            )}

            {currentView === 'integrations-whatsapp' && (
              <WhatsAppIntegrationSettings
                active={currentView === 'integrations-whatsapp'}
                cosmicAuth={authData ? { gatewayUrl: authData.gatewayUrl || '', gatewayApiToken: authData.gatewayApiToken || '' } : undefined}
              />
            )}

            {currentView === 'integrations-telegram' && (
              <TelegramIntegrationSettings
                active={currentView === 'integrations-telegram'}
                cosmicAuth={authData ? { gatewayUrl: authData.gatewayUrl || '', gatewayApiToken: authData.gatewayApiToken || '' } : undefined}
              />
            )}

            {currentView === 'integrations-github' && (
              <GitHubIntegrationSettings active={currentView === 'integrations-github'} />
            )}

            {currentView === 'devices' && (
              <MobileDevicesSettings active={currentView === 'devices'} />
            )}

            {currentView === 'monitors' && (
              <div className="setting-subpage">
                <div style={{ fontSize: 13, color: 'rgba(255,255,255,0.6)', marginBottom: 16, lineHeight: '1.5' }}>
                  Select which monitor the app should appear on when triggered, or keep it on Automatic to follow the
                  display nearest your cursor. If a saved monitor is disconnected, Cosmic falls back to a suitable display.
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
                  <div className="ui-setting-head">
                    <div>
                      <span className="setting-label">Chat Width</span>
                      <div className="ui-setting-note">
                        The default surface width for chat. Panel keeps the spotlight feel; Wide
                        stretches for code, consoles, and tables. The expand button in the
                        composer flips it anytime.
                      </div>
                    </div>
                    <div className="toggle-group">
                      <button
                        className={`toggle-btn ${!chatWideMode ? 'active' : ''}`}
                        onClick={() => onChatWideModeChange(false)}
                      >
                        Panel
                      </button>
                      <button
                        className={`toggle-btn ${chatWideMode ? 'active' : ''}`}
                        onClick={() => onChatWideModeChange(true)}
                      >
                        Wide
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

            {currentView === 'preferences' && (
              <GatewayPreferencesSettings
                active={currentView === 'preferences'}
                isAuthenticated={Boolean(authData)}
                gatewayConnection={gatewayConnection}
              />
            )}

            {currentView === 'api' && <ApiConfiguration keyStatus={keyStatus} />}
          </div>
        </LiquidGlass>
      </div>
    </div>
  )
}

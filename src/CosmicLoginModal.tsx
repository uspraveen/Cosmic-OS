import { useState } from 'react'
import LiquidGlass from './LiquidGlass'
import './settings.css'

interface CosmicLoginModalProps {
  onAuthenticated: (authData: any) => void
}

export default function CosmicLoginModal({ onAuthenticated }: CosmicLoginModalProps) {
  const [apiKey, setApiKey] = useState('')
  const [loading, setLoading] = useState(false)
  const [googleLoading, setGoogleLoading] = useState(false)
  const [error, setError] = useState('')

  const busy = loading || googleLoading

  const handleLogin = async () => {
    if (!apiKey.trim() || busy) return
    setLoading(true)
    setError('')

    try {
      const result = await window.cosmic?.login(apiKey.trim())
      if (result?.success) {
        onAuthenticated(result)
      } else {
        switch (result?.error) {
          case 'invalid_api_key':
            setError('Invalid API key. Please check and try again.')
            break
          case 'not_privileged':
            setError('This account does not have access to COSMIC desktop.')
            break
          case 'no_active_vm':
            setError('No active VM found for this account. Please contact support.')
            break
          default:
            setError(result?.message || 'Authentication failed. Please try again.')
        }
      }
    } catch {
      setError('Unable to connect. Check your internet connection.')
    } finally {
      setLoading(false)
    }
  }

  const handleGoogleLogin = async () => {
    if (busy) return
    setGoogleLoading(true)
    setError('')
    queueMicrotask(() => {
      window.cosmic?.minimizeApp()
    })
    try {
      const result = await window.cosmic?.loginWithGoogle()
      if (result?.success) {
        onAuthenticated(result)
      } else {
        switch (result?.error) {
          case 'oauth_cancelled':
            break
          case 'no_account':
            setError('No Cosmic account is linked to this Google account.')
            break
          case 'not_privileged':
            setError('This account does not have access to COSMIC desktop.')
            break
          case 'no_api_key':
            setError('No API key on file for this account. Contact support.')
            break
          case 'no_active_vm':
            setError('No active VM found for this account. Please contact support.')
            break
          default:
            setError(result?.message || 'Google sign-in failed. Please try again.')
        }
      }
    } catch {
      setError('Unable to connect. Check your internet connection.')
    } finally {
      setGoogleLoading(false)
      window.cosmic?.restoreApp()
    }
  }

  return (
    <div className="settings-overlay" style={{ backdropFilter: 'blur(20px)', zIndex: 20000 }}>
      <div className="settings-panel" style={{ width: 420 }} onClick={(e) => e.stopPropagation()}>
        <LiquidGlass cornerRadius={24}>
          <div style={{ position: 'relative' }}>
            <div className="cosmic-login-window-controls" role="toolbar" aria-label="Window">
              <button
                type="button"
                className="cosmic-login-window-btn minimize"
                aria-label="Minimize"
                onClick={() => window.cosmic?.minimizeApp()}
              >
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
                  <path d="M6 12h12" />
                </svg>
              </button>
              <button
                type="button"
                className="cosmic-login-window-btn close"
                aria-label="Quit Cosmic"
                onClick={() => window.cosmic?.quitApp()}
              >
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M7 7 17 17" />
                  <path d="M17 7 7 17" />
                </svg>
              </button>
            </div>
            <header className="cosmic-login-header">
              <h1 className="cosmic-login-wordmark">COSMIC</h1>
            </header>
            <div className="settings-content cosmic-login-settings-content">
              <div className="cosmic-login-intro">
                <p>
                  Sign in with Google or enter your Cosmic API key.
                </p>
              </div>

              <button
                type="button"
                onClick={handleGoogleLogin}
                disabled={busy}
                style={{
                  width: '100%',
                  padding: '13px',
                  background: busy ? 'rgba(255,255,255,0.06)' : 'rgba(255,255,255,0.12)',
                  color: '#fff',
                  border: '1px solid rgba(255,255,255,0.18)',
                  borderRadius: 12,
                  marginBottom: 16,
                  cursor: busy ? 'not-allowed' : 'pointer',
                  opacity: busy ? 0.55 : 1,
                  fontSize: 15,
                  fontWeight: 600,
                  transition: 'all 0.2s',
                }}
              >
                {googleLoading ? 'Opening Google…' : 'Continue with Google'}
              </button>

              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 12,
                  marginBottom: 16,
                  color: 'rgba(255,255,255,0.35)',
                  fontSize: 11,
                  fontWeight: 600,
                  letterSpacing: '0.06em',
                }}
              >
                <div style={{ flex: 1, height: 1, background: 'rgba(255,255,255,0.12)' }} />
                OR
                <div style={{ flex: 1, height: 1, background: 'rgba(255,255,255,0.12)' }} />
              </div>

              <div className="setting-row vertical">
                <span className="setting-label">Cosmic API Key</span>
                <input
                  type="password"
                  placeholder="cosmic_..."
                  value={apiKey}
                  onChange={(e) => { setApiKey(e.target.value); setError('') }}
                  onKeyDown={(e) => { if (e.key === 'Enter') handleLogin() }}
                  autoFocus
                  disabled={busy}
                  style={{
                    width: '100%',
                    background: 'rgba(255,255,255,0.1)',
                    border: error
                      ? '1px solid rgba(255, 69, 58, 0.5)'
                      : '1px solid rgba(255,255,255,0.1)',
                    padding: '12px',
                    borderRadius: 10,
                    color: '#fff',
                    outline: 'none',
                    marginTop: 8,
                    fontSize: 14,
                    transition: 'border-color 0.2s',
                  }}
                />
              </div>

              {error && (
                <div style={{
                  marginTop: 12,
                  padding: '10px 14px',
                  borderRadius: 10,
                  background: 'rgba(255, 69, 58, 0.12)',
                  border: '1px solid rgba(255, 69, 58, 0.2)',
                  color: 'rgba(255, 200, 195, 0.95)',
                  fontSize: 13,
                  lineHeight: 1.4,
                }}>
                  {error}
                </div>
              )}

              <button
                type="button"
                onClick={handleLogin}
                disabled={!apiKey.trim() || busy}
                style={{
                  width: '100%',
                  padding: '13px',
                  background: apiKey.trim() && !busy ? '#007AFF' : 'rgba(255,255,255,0.1)',
                  color: '#fff',
                  border: 'none',
                  borderRadius: 12,
                  marginTop: 20,
                  cursor: apiKey.trim() && !busy ? 'pointer' : 'not-allowed',
                  opacity: apiKey.trim() && !busy ? 1 : 0.5,
                  fontSize: 15,
                  fontWeight: 600,
                  transition: 'all 0.2s',
                }}
              >
                {loading ? 'Connecting...' : 'Connect'}
              </button>

              <p style={{
                marginTop: 20,
                fontSize: 11,
                color: 'rgba(255,255,255,0.3)',
                textAlign: 'center',
                lineHeight: 1.5,
              }}>
                Your API key is stored locally on this device and never shared.
              </p>
            </div>
          </div>
        </LiquidGlass>
      </div>
    </div>
  )
}

import { useState } from 'react'
import LiquidGlass from './LiquidGlass'
import './settings.css'

interface CosmicLoginModalProps {
  onAuthenticated: (authData: any) => void
}

export default function CosmicLoginModal({ onAuthenticated }: CosmicLoginModalProps) {
  const [apiKey, setApiKey] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const handleLogin = async () => {
    if (!apiKey.trim() || loading) return
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

  return (
    <div className="settings-overlay" style={{ backdropFilter: 'blur(20px)', zIndex: 20000 }}>
      <div className="settings-panel" style={{ width: 420 }} onClick={(e) => e.stopPropagation()}>
        <LiquidGlass cornerRadius={24}>
          <div className="settings-content" style={{ padding: 32 }}>
            <div style={{ textAlign: 'center', marginBottom: 28 }}>
              <h2 style={{ color: '#fff', fontSize: 24, marginBottom: 8, letterSpacing: '-0.02em' }}>
                Welcome to Cosmic
              </h2>
              <p style={{ color: 'rgba(255,255,255,0.55)', fontSize: 14, lineHeight: 1.5 }}>
                Enter your Cosmic API key to connect.
              </p>
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
                disabled={loading}
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
              onClick={handleLogin}
              disabled={!apiKey.trim() || loading}
              style={{
                width: '100%',
                padding: '13px',
                background: apiKey.trim() && !loading ? '#007AFF' : 'rgba(255,255,255,0.1)',
                color: '#fff',
                border: 'none',
                borderRadius: 12,
                marginTop: 20,
                cursor: apiKey.trim() && !loading ? 'pointer' : 'not-allowed',
                opacity: apiKey.trim() && !loading ? 1 : 0.5,
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
        </LiquidGlass>
      </div>
    </div>
  )
}

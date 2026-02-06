import { useState, useEffect } from 'react'
import LiquidGlass from './LiquidGlass'
import './settings.css'

interface SetupModalProps {
  onComplete: () => void
}

export default function SetupModal({ onComplete }: SetupModalProps) {
  const [geminiKey, setGeminiKey] = useState('')
  const [pplxKey, setPplxKey] = useState('')

  useEffect(() => {
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onComplete()
    }
    window.addEventListener('keydown', handleEsc)
    return () => window.removeEventListener('keydown', handleEsc)
  }, [onComplete])

  const handleSave = () => {
    if (!geminiKey && !pplxKey) return
    const payload = JSON.stringify({ gemini: geminiKey, perplexity: pplxKey })
    window.cosmic?.sendToGemini(`SAVE_KEYS:${payload}`)
    onComplete()
  }

  return (
    <div className="settings-overlay" style={{ backdropFilter: 'blur(20px)', zIndex: 20000 }} onMouseDown={(e) => {
      if (e.target === e.currentTarget) onComplete()
    }}>
      <div className="settings-panel" style={{ width: 400 }} onClick={(e) => e.stopPropagation()}>
        <LiquidGlass cornerRadius={24}>
          <div className="settings-content" style={{ padding: 32 }}>
            <div style={{ textAlign: 'center', marginBottom: 24 }}>
              <h2 style={{ color: '#fff', fontSize: 24, marginBottom: 8 }}>Welcome to Cosmic</h2>
              <p style={{ color: 'rgba(255,255,255,0.6)', fontSize: 14 }}>
                Please enter your API keys to get started. Keys are stored locally.
              </p>
            </div>

            <div className="setting-row vertical">
              <span className="setting-label">Google Gemini API Key</span>
              <input type="password" placeholder="AIzaSy..." value={geminiKey}
                onChange={(e) => setGeminiKey(e.target.value)}
                style={{ width: '100%', background: 'rgba(255,255,255,0.1)', border: '1px solid rgba(255,255,255,0.1)', padding: '10px', borderRadius: 8, color: '#fff', outline: 'none', marginTop: 8 }}
              />
            </div>

            <div className="setting-row vertical" style={{ marginTop: 16 }}>
              <span className="setting-label">Perplexity API Key (Optional)</span>
              <input type="password" placeholder="pplx-..." value={pplxKey}
                onChange={(e) => setPplxKey(e.target.value)}
                style={{ width: '100%', background: 'rgba(255,255,255,0.1)', border: '1px solid rgba(255,255,255,0.1)', padding: '10px', borderRadius: 8, color: '#fff', outline: 'none', marginTop: 8 }}
              />
            </div>

            <button onClick={handleSave} disabled={!geminiKey}
              style={{ width: '100%', padding: '12px', background: geminiKey ? '#007AFF' : 'rgba(255,255,255,0.1)', color: '#fff', border: 'none', borderRadius: 12, marginTop: 24, cursor: geminiKey ? 'pointer' : 'not-allowed', opacity: geminiKey ? 1 : 0.5 }}
            >
              Start Cosmic
            </button>
          </div>
        </LiquidGlass>
      </div>
    </div>
  )
}
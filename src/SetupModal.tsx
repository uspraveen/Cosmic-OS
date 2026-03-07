import { useEffect, useState } from 'react'
import LiquidGlass from './LiquidGlass'
import './settings.css'

interface SetupModalProps {
  onComplete: () => void
}

export default function SetupModal({ onComplete }: SetupModalProps) {
  const [deepgramKey, setDeepgramKey] = useState('')
  const [anthropicKey, setAnthropicKey] = useState('')
  const [groqKey, setGroqKey] = useState('')

  useEffect(() => {
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onComplete()
    }
    window.addEventListener('keydown', handleEsc)
    return () => window.removeEventListener('keydown', handleEsc)
  }, [onComplete])

  const handleSave = () => {
    if (!deepgramKey && !anthropicKey && !groqKey) {
      onComplete()
      return
    }

    window.cosmic?.saveLocalApiKeys({
      deepgram: deepgramKey,
      anthropic: anthropicKey,
      groq: groqKey,
    })
    window.cosmic?.getLocalKeyStatus()
    onComplete()
  }

  const isReady = Boolean(deepgramKey || anthropicKey || groqKey)

  return (
    <div className="settings-overlay" style={{ backdropFilter: 'blur(20px)', zIndex: 20000 }} onMouseDown={(e) => {
      if (e.target === e.currentTarget) onComplete()
    }}>
      <div className="settings-panel" style={{ width: 400 }} onClick={(e) => e.stopPropagation()}>
        <LiquidGlass cornerRadius={24}>
          <div className="settings-content" style={{ padding: 32 }}>
            <div style={{ textAlign: 'center', marginBottom: 24 }}>
              <h2 style={{ color: '#fff', fontSize: 24, marginBottom: 8 }}>Optional local setup</h2>
              <p style={{ color: 'rgba(255,255,255,0.6)', fontSize: 14 }}>
                Cosmic chat is already handled by your VM. Add local meeting keys here only if you want desktop voice and meeting features.
              </p>
            </div>

            <div className="setting-row vertical">
              <span className="setting-label">Deepgram API Key (Meeting)</span>
              <input type="password" placeholder="dg_..." value={deepgramKey}
                onChange={(e) => setDeepgramKey(e.target.value)}
                style={{ width: '100%', background: 'rgba(255,255,255,0.1)', border: '1px solid rgba(255,255,255,0.1)', padding: '10px', borderRadius: 8, color: '#fff', outline: 'none', marginTop: 8 }}
              />
            </div>

            <div className="setting-row vertical" style={{ marginTop: 16 }}>
              <span className="setting-label">Anthropic API Key (Meeting)</span>
              <input type="password" placeholder="sk-ant-..." value={anthropicKey}
                onChange={(e) => setAnthropicKey(e.target.value)}
                style={{ width: '100%', background: 'rgba(255,255,255,0.1)', border: '1px solid rgba(255,255,255,0.1)', padding: '10px', borderRadius: 8, color: '#fff', outline: 'none', marginTop: 8 }}
              />
            </div>

            <div className="setting-row vertical" style={{ marginTop: 16 }}>
              <span className="setting-label">Groq API Key (Meeting, optional)</span>
              <input type="password" placeholder="gsk_..." value={groqKey}
                onChange={(e) => setGroqKey(e.target.value)}
                style={{ width: '100%', background: 'rgba(255,255,255,0.1)', border: '1px solid rgba(255,255,255,0.1)', padding: '10px', borderRadius: 8, color: '#fff', outline: 'none', marginTop: 8 }}
              />
            </div>

            <button onClick={handleSave}
              style={{ width: '100%', padding: '12px', background: isReady ? '#007AFF' : 'rgba(255,255,255,0.1)', color: '#fff', border: 'none', borderRadius: 12, marginTop: 24, cursor: 'pointer', opacity: isReady ? 1 : 0.75 }}
            >
              {isReady ? 'Save and continue' : 'Skip for now'}
            </button>
          </div>
        </LiquidGlass>
      </div>
    </div>
  )
}

import { useState } from 'react'
import './settings.css'

interface ApiConfigurationProps {
  keyStatus: {
    haiku: boolean
    perplexity: boolean
    deepgram?: boolean
    groq?: boolean
    anthropic?: boolean
  }
}

type LocalProvider = 'deepgram' | 'groq' | 'anthropic'

export default function ApiConfiguration({ keyStatus }: ApiConfigurationProps) {
  const [deepgramKey, setDeepgramKey] = useState('')
  const [groqKey, setGroqKey] = useState('')
  const [anthropicKey, setAnthropicKey] = useState('')
  const [savedMsg, setSavedMsg] = useState<{ provider: LocalProvider; msg: string } | null>(null)

  const handleSave = (provider: LocalProvider, key: string) => {
    if (key.length < 5) return

    window.cosmic?.saveLocalApiKeys({ [provider]: key })

    if (provider === 'deepgram') setDeepgramKey('')
    else if (provider === 'groq') setGroqKey('')
    else setAnthropicKey('')

    setSavedMsg({ provider, msg: 'Saved!' })
    setTimeout(() => setSavedMsg(null), 2000)
    window.cosmic?.getLocalKeyStatus()
  }

  const renderKeyBlock = (
    provider: LocalProvider,
    label: string,
    placeholder: string,
    value: string,
    setValue: (value: string) => void,
    isActive: boolean,
  ) => (
    <div className="setting-row vertical" style={{
      background: 'rgba(255,255,255,0.03)',
      padding: 12,
      borderRadius: 10,
      border: '1px solid rgba(255,255,255,0.05)',
      width: '100%',
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', width: '100%', alignItems: 'center', marginBottom: 8 }}>
        <span className="setting-label">{label}</span>
        <span className={`api-badge ${isActive ? 'connected' : 'missing'}`}>
          {isActive ? 'Active' : 'Missing'}
        </span>
      </div>

      <div style={{ display: 'flex', gap: 8, width: '100%' }}>
        <input
          type="password"
          placeholder={placeholder}
          className="input"
          style={{
            background: 'rgba(255,255,255,0.05)',
            padding: '8px 12px',
            borderRadius: 6,
            flex: 1,
            color: 'white',
            border: '1px solid rgba(255,255,255,0.1)',
            fontSize: 13,
          }}
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        <button
          className="edit-key-btn"
          onClick={() => handleSave(provider, value)}
          disabled={value.length < 5}
          style={{ opacity: value.length < 5 ? 0.5 : 1, cursor: value.length < 5 ? 'default' : 'pointer' }}
        >
          {savedMsg?.provider === provider ? savedMsg.msg : 'Save'}
        </button>
      </div>
    </div>
  )

  return (
    <div className="setting-subpage">
      <div style={{ fontSize: 13, color: 'rgba(255,255,255,0.6)', marginBottom: 16, lineHeight: '1.5' }}>
        Cosmic chat now runs through your personal VM Gateway. These local keys are only for desktop voice and meeting features.
      </div>

      {renderKeyBlock(
        'deepgram',
        'Deepgram API Key (Meeting)',
        'Paste Deepgram API Key...',
        deepgramKey,
        setDeepgramKey,
        !!keyStatus.deepgram,
      )}

      {renderKeyBlock(
        'anthropic',
        'Anthropic API Key (Meeting)',
        'Paste Anthropic API Key (sk-ant-...)',
        anthropicKey,
        setAnthropicKey,
        !!keyStatus.anthropic,
      )}

      {renderKeyBlock(
        'groq',
        'Groq API Key (Meeting, optional)',
        'Paste Groq API Key (gsk_...)',
        groqKey,
        setGroqKey,
        !!keyStatus.groq,
      )}

      <div style={{ marginTop: 24, fontSize: 11, color: 'rgba(255,255,255,0.3)', fontStyle: 'italic', textAlign: 'center' }}>
        Chat model keys are configured on the VM in `Backend/gateway.env`, not on this desktop.
      </div>
    </div>
  )
}

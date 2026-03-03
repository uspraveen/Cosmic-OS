import { useState } from 'react'
import './settings.css'

interface ApiConfigurationProps {
    keyStatus: {
        gemini: boolean
        perplexity: boolean
        deepgram?: boolean
        groq?: boolean
        anthropic?: boolean
    }
}

export default function ApiConfiguration({ keyStatus }: ApiConfigurationProps) {
    const [geminiKey, setGeminiKey] = useState('')
    const [pplxKey, setPplxKey] = useState('')
    const [deepgramKey, setDeepgramKey] = useState('')
    const [groqKey, setGroqKey] = useState('')
    const [anthropicKey, setAnthropicKey] = useState('')
    const [savedMsg, setSavedMsg] = useState<{ provider: string; msg: string } | null>(null)

    const handleSave = (provider: 'gemini' | 'perplexity' | 'deepgram' | 'groq' | 'anthropic', key: string) => {
        if (key.length < 5) return

        const payload = JSON.stringify({ [provider]: key })
        window.cosmic?.sendToGemini(`SAVE_KEYS:${payload}`)

        // Clear input and show feedback
        if (provider === 'gemini') setGeminiKey('')
        else if (provider === 'perplexity') setPplxKey('')
        else if (provider === 'deepgram') setDeepgramKey('')
        else if (provider === 'groq') setGroqKey('')
        else setAnthropicKey('')

        setSavedMsg({ provider, msg: 'Saved!' })
        setTimeout(() => setSavedMsg(null), 2000)
    }

    const renderKeyBlock = (
        provider: 'gemini' | 'perplexity' | 'deepgram' | 'groq' | 'anthropic',
        label: string,
        placeholder: string,
        value: string,
        setValue: (value: string) => void,
        isActive: boolean
    ) => (
        <div className="setting-row vertical" style={{
            background: 'rgba(255,255,255,0.03)',
            padding: 12,
            borderRadius: 10,
            border: '1px solid rgba(255,255,255,0.05)',
            width: '100%'
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
                        fontSize: 13
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
                Configure your AI provider API keys here. Keys are stored securely on your device.
            </div>

            {renderKeyBlock(
                'gemini',
                'Gemini API Key',
                'Paste Gemini API Key...',
                geminiKey,
                setGeminiKey,
                keyStatus.gemini
            )}

            {renderKeyBlock(
                'perplexity',
                'Perplexity API Key',
                'Paste Perplexity API Key...',
                pplxKey,
                setPplxKey,
                keyStatus.perplexity
            )}

            {renderKeyBlock(
                'deepgram',
                'Deepgram API Key (Meeting)',
                'Paste Deepgram API Key...',
                deepgramKey,
                setDeepgramKey,
                !!keyStatus.deepgram
            )}

            {renderKeyBlock(
                'anthropic',
                'Anthropic API Key (Meeting)',
                'Paste Anthropic API Key (sk-ant-...)',
                anthropicKey,
                setAnthropicKey,
                !!keyStatus.anthropic
            )}

            {renderKeyBlock(
                'groq',
                'Groq API Key (Meeting, optional)',
                'Paste Groq API Key (gsk_...)',
                groqKey,
                setGroqKey,
                !!keyStatus.groq
            )}

            <div style={{ marginTop: 24, fontSize: 11, color: 'rgba(255,255,255,0.3)', fontStyle: 'italic', textAlign: 'center' }}>
                Keys are saved securely on your local machine using SQLite.
            </div>
        </div>
    )
}

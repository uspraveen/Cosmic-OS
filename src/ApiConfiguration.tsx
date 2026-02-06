import { useState } from 'react'
import './settings.css'

interface ApiConfigurationProps {
    keyStatus: { gemini: boolean; perplexity: boolean }
}

export default function ApiConfiguration({ keyStatus }: ApiConfigurationProps) {
    const [geminiKey, setGeminiKey] = useState('')
    const [pplxKey, setPplxKey] = useState('')
    const [savedMsg, setSavedMsg] = useState<{ provider: string; msg: string } | null>(null)

    const handleSave = (provider: 'gemini' | 'perplexity', key: string) => {
        if (key.length < 5) return

        const payload = JSON.stringify({ [provider]: key })
        window.cosmic?.sendToGemini(`SAVE_KEYS:${payload}`)

        // Clear input and show feedback
        if (provider === 'gemini') setGeminiKey('')
        else setPplxKey('')

        setSavedMsg({ provider, msg: 'Saved!' })
        setTimeout(() => setSavedMsg(null), 2000)
    }

    return (
        <div className="setting-subpage">
            <div style={{ fontSize: 13, color: 'rgba(255,255,255,0.6)', marginBottom: 16, lineHeight: '1.5' }}>
                Configure your AI provider API keys here. Keys are stored securely on your device.
            </div>

            {/* GEMINI SECTION */}
            <div className="setting-row vertical" style={{
                background: 'rgba(255,255,255,0.03)',
                padding: 12,
                borderRadius: 10,
                border: '1px solid rgba(255,255,255,0.05)',
                marginBottom: 16,
                width: '100%'
            }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', width: '100%', alignItems: 'center', marginBottom: 8 }}>
                    <span className="setting-label">Gemini API Key</span>
                    <span className={`api-badge ${keyStatus.gemini ? 'connected' : 'missing'}`}>
                        {keyStatus.gemini ? 'Active' : 'Missing'}
                    </span>
                </div>

                <div style={{ display: 'flex', gap: 8, width: '100%' }}>
                    <input
                        type="password"
                        placeholder="Paste Gemini API Key..."
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
                        value={geminiKey}
                        onChange={(e) => setGeminiKey(e.target.value)}
                    />
                    <button
                        className="edit-key-btn"
                        onClick={() => handleSave('gemini', geminiKey)}
                        disabled={geminiKey.length < 5}
                        style={{ opacity: geminiKey.length < 5 ? 0.5 : 1, cursor: geminiKey.length < 5 ? 'default' : 'pointer' }}
                    >
                        {savedMsg?.provider === 'gemini' ? savedMsg.msg : 'Save'}
                    </button>
                </div>
            </div>

            {/* PERPLEXITY SECTION */}
            <div className="setting-row vertical" style={{
                background: 'rgba(255,255,255,0.03)',
                padding: 12,
                borderRadius: 10,
                border: '1px solid rgba(255,255,255,0.05)',
                width: '100%'
            }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', width: '100%', alignItems: 'center', marginBottom: 8 }}>
                    <span className="setting-label">Perplexity API Key</span>
                    <span className={`api-badge ${keyStatus.perplexity ? 'connected' : 'missing'}`}>
                        {keyStatus.perplexity ? 'Active' : 'Missing'}
                    </span>
                </div>

                <div style={{ display: 'flex', gap: 8, width: '100%' }}>
                    <input
                        type="password"
                        placeholder="Paste Perplexity API Key..."
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
                        value={pplxKey}
                        onChange={(e) => setPplxKey(e.target.value)}
                    />
                    <button
                        className="edit-key-btn"
                        onClick={() => handleSave('perplexity', pplxKey)}
                        disabled={pplxKey.length < 5}
                        style={{ opacity: pplxKey.length < 5 ? 0.5 : 1, cursor: pplxKey.length < 5 ? 'default' : 'pointer' }}
                    >
                        {savedMsg?.provider === 'perplexity' ? savedMsg.msg : 'Save'}
                    </button>
                </div>
            </div>

            <div style={{ marginTop: 24, fontSize: 11, color: 'rgba(255,255,255,0.3)', fontStyle: 'italic', textAlign: 'center' }}>
                Keys are saved securely on your local machine using SQLite.
            </div>
        </div>
    )
}

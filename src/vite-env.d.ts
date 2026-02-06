/// <reference types="vite/client" />

interface Window {
  cosmic?: {
    hide: () => void
    toggle: () => void
    onShown: (cb: () => void) => () => void
    onHiding: (cb: () => void) => () => void
    onMediaUpdate: (cb: (data: any) => void) => () => void
    onWindowUpdate: (cb: (data: any) => void) => () => void
    onWeatherUpdate: (cb: (data: any) => void) => () => void
    requestWeather: () => void
    controlMedia: (action: 'playpause' | 'next' | 'prev') => void
    setVolume: (level: number) => void
    sendToGemini: (prompt: string) => void

    // Settings
    getSettings: () => void
    saveSetting: (key: string, value: any) => void
    onSettingsUpdate: (cb: (data: any) => void) => () => void

    // Perplexity APIs
    sendToPerplexity: (prompt: string) => void
    onPerplexityChunk: (cb: (data: { chunk: string, done: boolean }) => void) => () => void
    onPerplexitySources: (cb: (data: any[]) => void) => () => void

    // Gemini/LLM APIs
    onGeminiChunk: (cb: (data: { chunk: string, done: boolean }) => void) => () => void

    // DB / History APIs
    onKeyStatus: (cb: (data: any) => void) => () => void
    onSessionList: (cb: (data: any[]) => void) => () => void
    onHistoryLoad: (cb: (data: any[]) => void) => () => void
    onSessionSet: (cb: (id: string) => void) => () => void

    // Calendar APIs
    onCalendarUpdate: (cb: (data: any) => void) => () => void
    saveCalendarUrl: (url: string) => void
    calendarAuth: (action: 'LOGOUT' | 'CONNECT') => void
    quitApp: () => void
    restartApp: () => void
    openExternal: (url: string) => void
  }
}
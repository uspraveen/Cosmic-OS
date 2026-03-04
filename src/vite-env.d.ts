/// <reference types="vite/client" />

interface GatewayTunnelPayload {
  enabled?: boolean
  host?: string
  port?: number
  username?: string
  privateKeyPath?: string
  remoteHost?: string
  remotePort?: number
}

interface GatewayConnectionPayload {
  baseUrl: string
  apiToken: string
  tunnel?: GatewayTunnelPayload
}

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
    getIntegrations: () => void
    getCalendarAgenda: () => void
    saveIntegrationAccount: (payload: any) => void
    deleteIntegrationAccount: (accountId: string) => void
    connectGoogleAccount: (payload: any) => void
    disconnectGoogleAccount: (accountId: string) => void
    getWhatsAppStatus: (payload: GatewayConnectionPayload) => Promise<any>
    getWhatsAppConfig: (payload: GatewayConnectionPayload) => Promise<any>
    requestWhatsAppPairingQr: (payload: GatewayConnectionPayload & { refresh?: boolean; waitTimeoutMs?: number }) => Promise<any>
    clearWhatsAppSession: (payload: GatewayConnectionPayload) => Promise<any>
    saveWhatsAppConfig: (payload: {
      baseUrl: string
      apiToken: string
      tunnel?: GatewayTunnelPayload
      allowedPhone?: string | null
      selfChatOnly?: boolean | null
    }) => Promise<any>
    onCalendarAgendaUpdate: (cb: (data: any) => void) => () => void
    onIntegrationsUpdate: (cb: (data: any) => void) => () => void
    onIntegrationEvent: (cb: (data: any) => void) => () => void

    // Perplexity APIs
    sendToPerplexity: (prompt: string) => void
    onPerplexityChunk: (cb: (data: { chunk: string, done: boolean }) => void) => () => void
    onPerplexitySources: (cb: (data: any[]) => void) => () => void

    // Gemini/LLM APIs
    onGeminiChunk: (cb: (data: { chunk: string, done: boolean }) => void) => () => void

    // DB / History APIs
    onKeyStatus: (
      cb: (data: {
        hasKeys: boolean
        gemini: boolean
        perplexity: boolean
        deepgram?: boolean
        groq?: boolean
        anthropic?: boolean
      }) => void
    ) => () => void
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

    // Voice APIs
    startVoice: () => void
    stopVoice: () => void
    setVoiceKey: (key: string) => void
    onVoiceTranscript: (cb: (data: { text: string; is_final: boolean; timestamp: number }) => void) => () => void
    onVoiceStatus: (cb: (data: { status: string; error?: string; timestamp: number }) => void) => () => void

    // Meeting APIs
    startMeeting: (payload: {
      title: string
      goal?: string
      user_name?: string
      custom_instructions?: string
      mic_sensitivity?: number
      update_interval_sec?: number
      meeting_type?: 'online' | 'physical' | string
      web_search_enabled?: boolean
    }) => void
    stopMeeting: () => void
    pauseMeeting: () => void
    resumeMeeting: () => void
    setMeetingWebSearch: (enabled: boolean) => void
    askMeeting: (payload: { question: string; web_search_enabled?: boolean }) => void
    checkMeetingKeys: () => void
    getMeetingSettings: () => void
    saveMeetingSettings: (payload: { name_on_call?: string; mic_sensitivity?: number; update_interval_sec?: number }) => void
    onMeetingInvoke: (cb: () => void) => () => void
    onMeetingToggle: (cb: () => void) => () => void
    onMeetingStatus: (cb: (data: any) => void) => () => void
    onMeetingTranscript: (cb: (data: any) => void) => () => void
    onMeetingUpdate: (cb: (data: any) => void) => () => void
    onMeetingAnswer: (cb: (data: any) => void) => () => void
    onMeetingAnswerChunk: (cb: (data: { question: string; chunk: string }) => void) => () => void
    onMeetingFinal: (cb: (data: any) => void) => () => void
    onMeetingSettings: (cb: (data: { name_on_call?: string; mic_sensitivity?: number; update_interval_sec?: number }) => void) => () => void
  }
}

/// <reference types="vite/client" />

interface GatewayConnectionPayload {
  baseUrl: string
  apiToken: string
}

interface GatewaySocketState {
  status?: {
    state: 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'error'
    connected: boolean
    detail?: string
    sessionId?: string | null
  }
  sessionId?: string | null
  historyTail?: any[]
  knownTaskIds?: string[]
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

    // Auth
    login: (apiKey: string) => Promise<{ success: boolean; error?: string; message?: string; [key: string]: any }>
    logout: () => Promise<{
      success: boolean
      scopes?: {
        clearedAuth: boolean
        clearedGatewayTransport: boolean
        websocketClosed: boolean
        sessionCacheCleared: boolean
        reconnectStopped: boolean
        deviceIdRetained: boolean
      }
    }>

    // Settings
    getSettings: () => void
    saveSetting: (key: string, value: any) => void
    getLocalKeyStatus: () => void
    saveLocalApiKeys: (payload: { deepgram?: string; anthropic?: string; groq?: string }) => void
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
      allowedPhone?: string | null
      selfChatOnly?: boolean | null
    }) => Promise<any>
    sendWhatsAppTest: (payload: GatewayConnectionPayload & { number: string; message: string }) => Promise<any>
    onCalendarAgendaUpdate: (cb: (data: any) => void) => () => void
    onIntegrationsUpdate: (cb: (data: any) => void) => () => void
    onIntegrationEvent: (cb: (data: any) => void) => () => void

    // Gateway chat APIs
    getGatewayState: () => Promise<GatewaySocketState | null>
    requestGatewayResume: () => Promise<{ ok: boolean }>
    sendGatewayQuery: (payload: { content: string; conversationContext?: any[]; requestId?: string; routeOverride?: string }) => Promise<{ requestId: string }>
    cancelGatewayResponse: (payload: { requestId?: string; taskId?: string }) => Promise<{ ok: boolean }>
    listGatewaySessions: () => Promise<{ sessions: any[] }>
    getGatewaySessionHistory: (sessionId: string) => Promise<{ session_id: string; messages: any[] }>
    onGatewayEvent: (cb: (data: any) => void) => () => void
    onGatewayStatus: (cb: (data: GatewaySocketState['status']) => void) => () => void

    // Local key status APIs
    onKeyStatus: (
      cb: (data: {
        hasKeys: boolean
        haiku: boolean
        perplexity: boolean
        deepgram?: boolean
        groq?: boolean
        anthropic?: boolean
      }) => void
    ) => () => void

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

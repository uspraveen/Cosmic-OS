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
  foregroundStreams?: any[]
}

interface GatewayPendingDocumentAttachment {
  filePath: string
  filename: string
  mimeType: string
  sizeBytes: number
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
    loginWithGoogle: () => Promise<{ success: boolean; error?: string; message?: string; [key: string]: any }>
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
    getTelegramStatus: (payload: GatewayConnectionPayload) => Promise<any>
    syncTelegramWebhook: (payload: GatewayConnectionPayload) => Promise<any>
    clearTelegramWebhook: (payload: GatewayConnectionPayload & { dropPendingUpdates?: boolean }) => Promise<any>
    sendTelegramTest: (payload: GatewayConnectionPayload & { chatId: number; message: string }) => Promise<any>
    getGatewayAgentEmailStatus: () => Promise<any>
    saveGatewayAgentEmailConfig: (payload: {
      baseUrl: string
      apiToken: string
      primaryMailboxAddress?: string | null
    }) => Promise<any>
    clearGatewayAgentEmailConfig: () => Promise<any>
    getGatewayAgentEmailDesktopConfig: () => Promise<{
      available: boolean
      base_url?: string
      api_token?: string
      primary_mailbox_address?: string
      organization_id?: string | null
    }>
    saveGatewayAgentEmailTrustedSenders: (payload: {
      trustedSenders: string[]
    }) => Promise<any>
    cosmicMailRequest: (payload: GatewayConnectionPayload & {
      path: string
      method?: string
      body?: unknown
      timeoutMs?: number
    }) => Promise<any>
    recordCosmicMailGatewayNotification: (payload: any) => Promise<any>
    cosmicMailUploadDraftAttachment: (payload: GatewayConnectionPayload & {
      draftId: string
      filePath: string
      filename?: string
      timeoutMs?: number
    }) => Promise<unknown>
    cosmicMailDownloadAttachment: (payload: GatewayConnectionPayload & {
      attachmentId: string
      suggestedFilename?: string
      timeoutMs?: number
    }) => Promise<{ cancelled: true } | { cancelled: false; path: string }>
    onCosmicMailInbound: (cb: (data: any) => void) => () => void
    onCosmicMailApproval: (cb: (data: any) => void) => () => void
    onCalendarAgendaUpdate: (cb: (data: any) => void) => () => void
    onIntegrationsUpdate: (cb: (data: any) => void) => () => void
    onIntegrationEvent: (cb: (data: any) => void) => () => void

    // Gateway chat APIs
    getGatewayState: () => Promise<GatewaySocketState | null>
    requestGatewayResume: () => Promise<{ ok: boolean }>
    getGatewayPreferences: () => Promise<any>
    saveGatewayPreferences: (payload: {
      visualResponseEnhancementEnabled?: boolean
      cosmicOrchestratorProvider?: 'anthropic' | 'fireworks_kimi'
      cosmicOrchestratorModel?: string
      cosmicHeartbeatEnabled?: boolean
    }) => Promise<any>
    getGatewayCodexStatus: () => Promise<any>
    saveGatewayCodexConfig: (payload: {
      authMode?: string
      apiKey?: string
      preferredModel?: string
      reasoningEffort?: string
      approvalMode?: string
      vmSyncEnabled?: boolean
    }) => Promise<any>
    startGatewayCodexLogin: () => Promise<any>
    logoutGatewayCodex: () => Promise<any>
    getGatewayCursorStatus: () => Promise<any>
    saveGatewayCursorConfig: (payload: {
      preferredModel?: string
      approvalMode?: string
      vmSyncEnabled?: boolean
    }) => Promise<any>
    startGatewayCursorLogin: () => Promise<any>
    logoutGatewayCursor: () => Promise<any>
    getGatewayAlphaAgentConfig: () => Promise<any>
    saveGatewayAlphaAgentConfig: (payload: {
      preferredHarness?: string
    }) => Promise<any>
    pickGatewayDocuments: () => Promise<{ documents: GatewayPendingDocumentAttachment[] }>
    sendGatewayQuery: (payload: {
      content: string
      conversationContext?: any[]
      requestId?: string
      routeOverride?: string
      attachments?: GatewayPendingDocumentAttachment[]
    }) => Promise<{ requestId: string }>
    submitGatewayTaskInputReply: (payload: { inputRequestId: string; taskId: string; content: string }) => Promise<{ ok: boolean; requestId: string }>
    cancelGatewayResponse: (payload: { requestId?: string; taskId?: string }) => Promise<{ ok: boolean }>
    backgroundGatewayRequest: (payload: { requestId: string }) => Promise<{ ok: boolean; requestId: string }>
    foregroundGatewayRequest: (payload: { requestId: string }) => Promise<{ ok: boolean; requestId: string }>
    listGatewaySessions: () => Promise<{ sessions: any[] }>
    getGatewaySessionHistory: (sessionId: string) => Promise<{ session_id: string; messages: any[] }>
    getGatewayRequestTraces: (sessionId: string) => Promise<{ session_id: string; request_traces: any[] }>
    listMobileDevices: () => Promise<{ devices: any[] }>
    authorizeMobileDevice: (deviceId: string) => Promise<{ device: any }>
    revokeMobileDevice: (deviceId: string) => Promise<{ device: any }>
    revokeAllMobileDevices: () => Promise<{ revoked_count: number; device_ids: string[]; revoked_at: string; reason: string }>
    onGatewayEvent: (cb: (data: any) => void) => () => void
    onGatewayStatus: (cb: (data: GatewaySocketState['status']) => void) => () => void
    getGatewaySystemMetrics: (
      opts?:
        | boolean
        | {
            forceRefresh?: boolean
            usage?: {
              usage_days?: number
              usage_hours?: number
              usage_start?: string
              usage_end?: string
            }
          },
    ) => Promise<unknown>
    getGatewayRegistryAgents: () => Promise<unknown>
    downloadGatewayOutputArtifact: (payload: {
      messageId: string
      artifactId: string
      suggestedFilename?: string
      mimeType?: string
    }) => Promise<{ cancelled: true } | { cancelled: false; filePath: string; filename: string }>

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
    minimizeApp: () => void
    restoreApp: () => void
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

import { ipcRenderer, contextBridge } from 'electron'

console.log("--- PRELOAD SCRIPT LOADED ---")

type AnyListener = (...args: any[]) => void
const listenerMap = new Map<AnyListener, AnyListener>()

contextBridge.exposeInMainWorld('ipcRenderer', {
  on(channel: string, listener: AnyListener) {
    const wrapped = (_event: any, ...args: any[]) => listener(_event, ...args)
    listenerMap.set(listener, wrapped)
    ipcRenderer.on(channel, wrapped)
    return () => {
      const w = listenerMap.get(listener)
      if (w) ipcRenderer.removeListener(channel, w)
      listenerMap.delete(listener)
    }
  },
  off(channel: string, listener: AnyListener) {
    const w = listenerMap.get(listener)
    if (w) {
      ipcRenderer.removeListener(channel, w)
      listenerMap.delete(listener)
      return
    }
    ipcRenderer.removeListener(channel, listener as any)
  },
  send(channel: string, ...args: any[]) {
    return ipcRenderer.send(channel, ...args)
  },
  invoke(channel: string, ...args: any[]) {
    return ipcRenderer.invoke(channel, ...args)
  },
})

contextBridge.exposeInMainWorld('cosmic', {
  hide: () => ipcRenderer.send('cosmic:hide'),
  toggle: () => ipcRenderer.send('cosmic:toggle'),

  onShown: (cb: () => void) => {
    const listener = () => cb()
    ipcRenderer.on('cosmic:shown', listener)
    return () => ipcRenderer.removeListener('cosmic:shown', listener)
  },

  onHiding: (cb: () => void) => {
    const listener = () => cb()
    ipcRenderer.on('cosmic:hiding', listener)
    return () => ipcRenderer.removeListener('cosmic:hiding', listener)
  },

  onMediaUpdate: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('media:update', listener)
    return () => ipcRenderer.removeListener('media:update', listener)
  },

  onWindowUpdate: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('window:update', listener)
    return () => ipcRenderer.removeListener('window:update', listener)
  },

  // --- WEATHER BRIDGE ---
  onWeatherUpdate: (cb: (data: any) => void) => {
    console.log("Bridge: Registering weather listener")
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('weather:update', listener)
    return () => ipcRenderer.removeListener('weather:update', listener)
  },

  requestWeather: () => ipcRenderer.send('weather:request'),

  // --- VOICE BRIDGE ---
  startVoice: () => ipcRenderer.send('voice:start'),
  stopVoice: () => ipcRenderer.send('voice:stop'),
  setVoiceKey: (key: string) => ipcRenderer.send('voice:set-key', key),
  onVoiceTranscript: (cb: (data: { text: string; is_final: boolean; timestamp: number }) => void) => {
    const listener = (_: any, data: { text: string; is_final: boolean; timestamp: number }) => cb(data)
    ipcRenderer.on('voice:transcript', listener)
    return () => ipcRenderer.removeListener('voice:transcript', listener)
  },
  onVoiceStatus: (cb: (data: { status: string; error?: string; timestamp: number }) => void) => {
    const listener = (_: any, data: { status: string; error?: string; timestamp: number }) => cb(data)
    ipcRenderer.on('voice:status', listener)
    return () => ipcRenderer.removeListener('voice:status', listener)
  },

  onKeyStatus: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('key-status', listener)
    return () => ipcRenderer.removeListener('key-status', listener)
  },
  getLocalKeyStatus: () => ipcRenderer.send('settings:get-key-status'),
  saveLocalApiKeys: (payload: any) => ipcRenderer.send('settings:save-api-keys', payload),
  getGatewayState: () => ipcRenderer.invoke('gateway:get-state'),
  requestGatewayResume: () => ipcRenderer.invoke('gateway:request-resume'),
  getGatewayPreferences: () => ipcRenderer.invoke('gateway:get-preferences'),
  saveGatewayPreferences: (payload: any) => ipcRenderer.invoke('gateway:save-preferences', payload),
  getGatewayCodexStatus: () => ipcRenderer.invoke('gateway:get-codex-status'),
  saveGatewayCodexConfig: (payload: any) => ipcRenderer.invoke('gateway:save-codex-config', payload),
  startGatewayCodexLogin: () => ipcRenderer.invoke('gateway:start-codex-login'),
  logoutGatewayCodex: () => ipcRenderer.invoke('gateway:logout-codex'),
  getGatewayCursorStatus: () => ipcRenderer.invoke('gateway:get-cursor-status'),
  saveGatewayCursorConfig: (payload: any) => ipcRenderer.invoke('gateway:save-cursor-config', payload),
  startGatewayCursorLogin: () => ipcRenderer.invoke('gateway:start-cursor-login'),
  logoutGatewayCursor: () => ipcRenderer.invoke('gateway:logout-cursor'),
  getGatewayAlphaAgentConfig: () => ipcRenderer.invoke('gateway:get-alpha-agent-config'),
  saveGatewayAlphaAgentConfig: (payload: any) => ipcRenderer.invoke('gateway:save-alpha-agent-config', payload),
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
  ) => ipcRenderer.invoke('gateway:get-system-metrics', opts ?? false),
  getGatewayRegistryAgents: () => ipcRenderer.invoke('gateway:get-registry-agents'),
  downloadGatewayOutputArtifact: (payload: { messageId: string; artifactId: string; suggestedFilename?: string; mimeType?: string; timeoutMs?: number }) => ipcRenderer.invoke('gateway:download-output-artifact', payload),
  pickGatewayDocuments: () => ipcRenderer.invoke('gateway:pick-documents'),
  sendGatewayQuery: (payload: { content: string; conversationContext?: any[]; requestId?: string; routeOverride?: string; attachments?: any[] }) => ipcRenderer.invoke('gateway:send-query', payload),
  submitGatewayTaskInputReply: (payload: { inputRequestId: string; taskId: string; content: string }) => ipcRenderer.invoke('gateway:submit-task-input-reply', payload),
  cancelGatewayResponse: (payload: { requestId?: string; taskId?: string }) => ipcRenderer.invoke('gateway:cancel-response', payload),
  backgroundGatewayRequest: (payload: { requestId: string }) => ipcRenderer.invoke('gateway:background-request', payload),
  foregroundGatewayRequest: (payload: { requestId: string }) => ipcRenderer.invoke('gateway:foreground-request', payload),
  listGatewaySessions: () => ipcRenderer.invoke('gateway:list-sessions'),
  getGatewaySessionHistory: (sessionId: string) => ipcRenderer.invoke('gateway:get-session-history', sessionId),
  getGatewayRequestTraces: (sessionId: string) => ipcRenderer.invoke('gateway:get-request-traces', sessionId),
  listMobileDevices: () => ipcRenderer.invoke('gateway:list-mobile-devices'),
  authorizeMobileDevice: (deviceId: string) => ipcRenderer.invoke('gateway:authorize-mobile-device', deviceId),
  revokeMobileDevice: (deviceId: string) => ipcRenderer.invoke('gateway:revoke-mobile-device', deviceId),
  revokeAllMobileDevices: () => ipcRenderer.invoke('gateway:revoke-all-mobile-devices'),
  onGatewayEvent: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('gateway:event', listener)
    return () => ipcRenderer.removeListener('gateway:event', listener)
  },
  onGatewayStatus: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('gateway:status', listener)
    return () => ipcRenderer.removeListener('gateway:status', listener)
  },

  // --- MEETING MODE ---
  startMeeting: (payload: any) => ipcRenderer.send('meeting:start', payload),
  stopMeeting: () => ipcRenderer.send('meeting:stop'),
  pauseMeeting: () => ipcRenderer.send('meeting:pause'),
  resumeMeeting: () => ipcRenderer.send('meeting:resume'),
  setMeetingWebSearch: (enabled: boolean) => ipcRenderer.send('meeting:set-web-search', { web_search_enabled: enabled }),
  askMeeting: (payload: any) => ipcRenderer.send('meeting:ask', payload),
  checkMeetingKeys: () => ipcRenderer.send('meeting:check-keys'),
  getMeetingSettings: () => ipcRenderer.send('meeting:get-settings'),
  saveMeetingSettings: (payload: any) => ipcRenderer.send('meeting:save-settings', payload),
  onMeetingInvoke: (cb: () => void) => {
    const listener = () => cb()
    ipcRenderer.on('meeting:invoke', listener)
    return () => ipcRenderer.removeListener('meeting:invoke', listener)
  },
  onMeetingStatus: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('meeting:status', listener)
    return () => ipcRenderer.removeListener('meeting:status', listener)
  },
  onMeetingTranscript: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('meeting:transcript', listener)
    return () => ipcRenderer.removeListener('meeting:transcript', listener)
  },
  onMeetingUpdate: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('meeting:update', listener)
    return () => ipcRenderer.removeListener('meeting:update', listener)
  },
  onMeetingAnswer: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('meeting:answer', listener)
    return () => ipcRenderer.removeListener('meeting:answer', listener)
  },
  onMeetingAnswerChunk: (cb: (data: { question: string; chunk: string }) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('meeting:answer-chunk', listener)
    return () => ipcRenderer.removeListener('meeting:answer-chunk', listener)
  },
  onMeetingFinal: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('meeting:final', listener)
    return () => ipcRenderer.removeListener('meeting:final', listener)
  },
  onMeetingSettings: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('meeting:settings', listener)
    return () => ipcRenderer.removeListener('meeting:settings', listener)
  },
  onMeetingToggle: (cb: () => void) => {
    const listener = () => cb()
    ipcRenderer.on('meeting:toggle-visibility', listener)
    return () => ipcRenderer.removeListener('meeting:toggle-visibility', listener)
  },


  // AUTH
  login: (apiKey: string) => ipcRenderer.invoke('auth:login', apiKey),
  loginWithGoogle: () => ipcRenderer.invoke('auth:loginWithGoogle'),
  logout: () => ipcRenderer.invoke('auth:logout'),

  quitApp: () => ipcRenderer.send('app:quit'),
  minimizeApp: () => ipcRenderer.send('app:minimize'),
  restoreApp: () => ipcRenderer.send('app:restore'),
  restartApp: () => ipcRenderer.send('app:restart'),

  controlMedia: (action: string) => ipcRenderer.send('media:control', action),
  setVolume: (level: number) => ipcRenderer.send('media:set_volume', level),
  openExternal: (url: string) => ipcRenderer.send('open-external', url),

  // SETTINGS
  getSettings: () => ipcRenderer.send('settings:get-all'),
  saveSetting: (key: string, value: any) => ipcRenderer.send('settings:save', { key, value }),
  onSettingsUpdate: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('settings:all', listener)
    return () => ipcRenderer.removeListener('settings:all', listener)
  },
  getIntegrations: () => ipcRenderer.send('integrations:get-all'),
  getCalendarAgenda: () => ipcRenderer.send('calendar:get-agenda'),
  saveIntegrationAccount: (payload: any) => ipcRenderer.send('integrations:save-account', payload),
  deleteIntegrationAccount: (accountId: string) => ipcRenderer.send('integrations:delete-account', accountId),
  connectGoogleAccount: (payload: any) => ipcRenderer.send('integrations:connect-google', payload),
  disconnectGoogleAccount: (accountId: string) => ipcRenderer.send('integrations:disconnect-google', accountId),
  getWhatsAppStatus: (payload: any) => ipcRenderer.invoke('whatsapp:get-status', payload),
  getWhatsAppConfig: (payload: any) => ipcRenderer.invoke('whatsapp:get-config', payload),
  requestWhatsAppPairingQr: (payload: any) => ipcRenderer.invoke('whatsapp:request-pairing-qr', payload),
  clearWhatsAppSession: (payload: any) => ipcRenderer.invoke('whatsapp:clear-session', payload),
  saveWhatsAppConfig: (payload: any) => ipcRenderer.invoke('whatsapp:save-config', payload),
  sendWhatsAppTest: (payload: any) => ipcRenderer.invoke('whatsapp:send-test', payload),
  getTelegramStatus: (payload: any) => ipcRenderer.invoke('telegram:get-status', payload),
  syncTelegramWebhook: (payload: any) => ipcRenderer.invoke('telegram:sync-webhook', payload),
  clearTelegramWebhook: (payload: any) => ipcRenderer.invoke('telegram:clear-webhook', payload),
  sendTelegramTest: (payload: any) => ipcRenderer.invoke('telegram:send-test', payload),
  getGatewayAgentEmailStatus: () => ipcRenderer.invoke('gateway:get-agent-email-status'),
  saveGatewayAgentEmailConfig: (payload: any) => ipcRenderer.invoke('gateway:save-agent-email-config', payload),
  clearGatewayAgentEmailConfig: () => ipcRenderer.invoke('gateway:clear-agent-email-config'),
  getGatewayAgentEmailDesktopConfig: () => ipcRenderer.invoke('gateway:get-agent-email-desktop-config'),
  saveGatewayAgentEmailTrustedSenders: (payload: any) => ipcRenderer.invoke('gateway:save-agent-email-trusted-senders', payload),
  cosmicMailRequest: (payload: any) => ipcRenderer.invoke('cosmic-mail:request', payload),
  recordCosmicMailGatewayNotification: (payload: any) => ipcRenderer.invoke('cosmic-mail:record-gateway-notification', payload),
  cosmicMailUploadDraftAttachment: (payload: any) => ipcRenderer.invoke('cosmic-mail:upload-draft-attachment', payload),
  cosmicMailDownloadAttachment: (payload: any) => ipcRenderer.invoke('cosmic-mail:download-attachment', payload),
  onCosmicMailInbound: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('cosmic-mail:new-inbound', listener)
    return () => ipcRenderer.removeListener('cosmic-mail:new-inbound', listener)
  },
  onCosmicMailApproval: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('cosmic-mail:new-approval', listener)
    return () => ipcRenderer.removeListener('cosmic-mail:new-approval', listener)
  },
  onCalendarAgendaUpdate: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('calendar:agenda', listener)
    return () => ipcRenderer.removeListener('calendar:agenda', listener)
  },
  onIntegrationsUpdate: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('integrations:all', listener)
    return () => ipcRenderer.removeListener('integrations:all', listener)
  },
  onIntegrationEvent: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('integration:event', listener)
    return () => ipcRenderer.removeListener('integration:event', listener)
  },
})

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

  onGeminiChunk: (cb: (data: { chunk: string, done: boolean }) => void) => {
    const listener = (_: any, data: { chunk: string, done: boolean }) => cb(data)
    ipcRenderer.on('gemini:chunk', listener)
    return () => ipcRenderer.removeListener('gemini:chunk', listener)
  },

  onPerplexityChunk: (cb: (data: { chunk: string, done: boolean }) => void) => {
    const listener = (_: any, data: { chunk: string, done: boolean }) => cb(data)
    ipcRenderer.on('perplexity:chunk', listener)
    return () => ipcRenderer.removeListener('perplexity:chunk', listener)
  },

  onPerplexitySources: (cb: (data: any[]) => void) => {
    const listener = (_: any, data: any[]) => cb(data)
    ipcRenderer.on('perplexity:sources', listener)
    return () => ipcRenderer.removeListener('perplexity:sources', listener)
  },

  onKeyStatus: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('key-status', listener)
    return () => ipcRenderer.removeListener('key-status', listener)
  },
  onSessionList: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('session-list', listener)
    return () => ipcRenderer.removeListener('session-list', listener)
  },
  onHistoryLoad: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('history-load', listener)
    return () => ipcRenderer.removeListener('history-load', listener)
  },
  onSessionSet: (cb: (id: string) => void) => {
    const listener = (_: any, id: string) => cb(id)
    ipcRenderer.on('session-set', listener)
    return () => ipcRenderer.removeListener('session-set', listener)
  },

  onCalendarUpdate: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('calendar:update', listener)
    return () => ipcRenderer.removeListener('calendar:update', listener)
  },

  // NEW: Calendar Auth
  saveCalendarUrl: (url: string) => ipcRenderer.send('calendar:save-url', url),

  quitApp: () => ipcRenderer.send('app:quit'),
  restartApp: () => ipcRenderer.send('app:restart'),

  controlMedia: (action: string) => ipcRenderer.send('media:control', action),
  setVolume: (level: number) => ipcRenderer.send('media:set_volume', level),
  sendToGemini: (prompt: string) => ipcRenderer.send('gemini:send', prompt),
  sendToPerplexity: (prompt: string) => ipcRenderer.send('perplexity:send', prompt),
  openExternal: (url: string) => ipcRenderer.send('open-external', url),

  // SETTINGS
  getSettings: () => ipcRenderer.send('settings:get-all'),
  saveSetting: (key: string, value: any) => ipcRenderer.send('settings:save', { key, value }),
  onSettingsUpdate: (cb: (data: any) => void) => {
    const listener = (_: any, data: any) => cb(data)
    ipcRenderer.on('settings:all', listener)
    return () => ipcRenderer.removeListener('settings:all', listener)
  },
})
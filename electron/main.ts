import { app, BrowserWindow, globalShortcut, ipcMain, screen, shell } from 'electron'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { spawn } from 'node:child_process'
import Store from 'electron-store'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
process.env.APP_ROOT = path.join(__dirname, '..')

// Supabase public constants (anon key is safe to commit — only allows RLS-protected queries)
const SUPABASE_URL = 'https://hluenippcdiejenmteen.supabase.co'
const SUPABASE_ANON_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhsdWVuaXBwY2RpZWplbm10ZWVuIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTE4MzYwOTMsImV4cCI6MjA2NzQxMjA5M30.dm6YO4B9SAQ8hnGtR-OZS7jn5FcL-zz4s4XxP-TyCpk'

export const VITE_DEV_SERVER_URL = process.env['VITE_DEV_SERVER_URL']
export const MAIN_DIST = path.join(process.env.APP_ROOT, 'dist-electron')
export const RENDERER_DIST = path.join(process.env.APP_ROOT, 'dist')

process.env.VITE_PUBLIC = VITE_DEV_SERVER_URL
  ? path.join(process.env.APP_ROOT, 'public')
  : RENDERER_DIST

// Electron Store for persisting settings
const store = new Store({
  defaults: {
    preferredDisplayId: null,
    autoRepositionOnChange: true
  }
})

let win: BrowserWindow | null = null
let mediaProcess: any = null
let windowProcess: any = null
let geminiProcess: any = null
let perplexityProcess: any = null
let weatherProcess: any = null
let meetingProcess: any = null

let voiceProcess: any = null
let voiceActive = false
let searchVisible = false
let lastWeatherData: any = null

interface GatewayConnectionConfig {
  baseUrl: string
  apiToken: string
}

function unwrapWhatsAppBridgePayload(payload: any) {
  if (payload && typeof payload === 'object' && payload.bridge && typeof payload.bridge === 'object') {
    return payload.bridge
  }
  return payload
}

function normalizeGatewayBaseUrl(rawBaseUrl: string) {
  const trimmed = String(rawBaseUrl || '').trim()
  if (!trimmed) {
    throw new Error('Gateway URL is required.')
  }

  const hasScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed)
  const withScheme = hasScheme
    ? trimmed
    : /^(localhost|127(?:\.\d{1,3}){3}|\[::1\])/i.test(trimmed)
      ? `http://${trimmed}`
      : `https://${trimmed}`

  const url = new URL(withScheme)
  return url.toString().replace(/\/$/, '')
}

async function callGatewayJson(
  config: GatewayConnectionConfig,
  pathName: string,
  init: {
    method?: string
    body?: unknown
    timeoutMs?: number
  } = {},
) {
  const apiToken = String(config?.apiToken || '').trim()
  if (!apiToken) {
    throw new Error('Gateway API token is required.')
  }

  const baseUrl = normalizeGatewayBaseUrl(config?.baseUrl || '')
  const requestUrl = new URL(pathName, `${baseUrl}/`).toString()
  const controller = new AbortController()
  const timeoutMs = Math.max(1000, init.timeoutMs ?? 20000)
  const timeout = setTimeout(() => controller.abort(), timeoutMs)

  try {
    const response = await fetch(requestUrl, {
      method: init.method ?? 'GET',
      headers: {
        Accept: 'application/json',
        Authorization: `Bearer ${apiToken}`,
        ...(init.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      },
      body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
      signal: controller.signal,
    })

    const responseText = await response.text()
    let payload: any = null

    if (responseText) {
      try {
        payload = JSON.parse(responseText)
      } catch {
        payload = { raw: responseText }
      }
    }

    if (!response.ok) {
      const detail =
        (typeof payload?.detail === 'string' && payload.detail) ||
        (typeof payload?.error === 'string' && payload.error) ||
        response.statusText ||
        `Gateway request failed (${response.status})`
      throw new Error(detail)
    }

    return payload
  } catch (error: any) {
    if (error?.name === 'AbortError') {
      throw new Error('Gateway request timed out.')
    }
    throw error
  } finally {
    clearTimeout(timeout)
  }
}

function startGeminiBridge(window: BrowserWindow) {
  const scriptPath = path.join(process.env.APP_ROOT, 'resources', 'gemini_bridge.py')
  geminiProcess = spawn('python', ['-u', scriptPath])

  geminiProcess.stderr?.on('data', (d: any) => console.error('[GEMINI ERR]', d.toString()))

  let rawBuffer = ''

  geminiProcess.stdout.on('data', (chunk: any) => {
    rawBuffer += chunk.toString()

    const regex = /<<([A-Z_]+)>>(.*?)<<END>>/gs
    let match
    let lastIndex = 0

    while ((match = regex.exec(rawBuffer)) !== null) {
      const [, tag, content] = match
      lastIndex = regex.lastIndex

      try {
        const json = JSON.parse(content)

        if (tag === 'CHUNK') window.webContents.send('gemini:chunk', json)
        else if (tag === 'KEY_STATUS') window.webContents.send('key-status', json)
        else if (tag === 'SESSIONS') window.webContents.send('session-list', json)
        else if (tag === 'HISTORY') window.webContents.send('history-load', json)
        else if (tag === 'SESSION_SET') window.webContents.send('session-set', json)

      } catch (e) {
        console.error("Parse Error:", e)
      }
    }

    if (lastIndex > 0) {
      rawBuffer = rawBuffer.substring(lastIndex)
    }
  })
}

function startMediaBridge(window: BrowserWindow) {
  let scriptName = 'media_bridge_win.py'
  if (process.platform === 'darwin') {
    scriptName = 'media_bridge_mac.py'
  }

  const scriptPath = path.join(process.env.APP_ROOT, 'resources', scriptName)
  mediaProcess = spawn('python', ['-u', scriptPath])

  let rawBuffer = ''
  mediaProcess.stdout.on('data', (chunk: any) => {
    rawBuffer += chunk.toString()
    let startIndex = rawBuffer.indexOf('<<START>>')
    let endIndex = rawBuffer.indexOf('<<END>>')
    while (startIndex !== -1 && endIndex !== -1) {
      if (endIndex > startIndex) {
        try {
          const jsonStr = rawBuffer.substring(startIndex + 9, endIndex)
          window.webContents.send('media:update', JSON.parse(jsonStr))
        } catch { }
        rawBuffer = rawBuffer.substring(endIndex + 7)
      } else { rawBuffer = rawBuffer.substring(startIndex) }
      startIndex = rawBuffer.indexOf('<<START>>')
      endIndex = rawBuffer.indexOf('<<END>>')
    }
  })
}

function startWindowBridge(window: BrowserWindow) {
  let scriptName = 'window_bridge_win.py'
  if (process.platform === 'darwin') {
    scriptName = 'window_bridge_mac.py'
  }

  const scriptPath = path.join(process.env.APP_ROOT, 'resources', scriptName)
  windowProcess = spawn('python', ['-u', scriptPath])

  let rawBuffer = ''
  windowProcess.stdout.on('data', (chunk: any) => {
    rawBuffer += chunk.toString()
    let startIndex = rawBuffer.indexOf('<<WINDOW>>')
    let endIndex = rawBuffer.indexOf('<<END>>')
    while (startIndex !== -1 && endIndex !== -1) {
      if (endIndex > startIndex) {
        try {
          const jsonStr = rawBuffer.substring(startIndex + 10, endIndex)
          window.webContents.send('window:update', JSON.parse(jsonStr))
        } catch { }
        rawBuffer = rawBuffer.substring(endIndex + 7)
      } else { rawBuffer = rawBuffer.substring(startIndex) }
      startIndex = rawBuffer.indexOf('<<WINDOW>>')
      endIndex = rawBuffer.indexOf('<<END>>')
    }
  })
}

function startWeatherBridge(window: BrowserWindow) {
  const scriptPath = path.join(process.env.APP_ROOT, 'resources', 'weather_bridge.py')
  weatherProcess = spawn('python', ['-u', scriptPath])

  let rawBuffer = ''
  weatherProcess.stdout.on('data', (chunk: any) => {
    rawBuffer += chunk.toString()
    let startIndex = rawBuffer.indexOf('<<WEATHER>>')
    let endIndex = rawBuffer.indexOf('<<END>>')
    while (startIndex !== -1 && endIndex !== -1) {
      if (endIndex > startIndex) {
        try {
          const jsonStr = rawBuffer.substring(startIndex + 11, endIndex)
          lastWeatherData = JSON.parse(jsonStr)
          window.webContents.send('weather:update', lastWeatherData)
        } catch { }
        rawBuffer = rawBuffer.substring(endIndex + 7)
      } else { rawBuffer = rawBuffer.substring(startIndex) }
      startIndex = rawBuffer.indexOf('<<WEATHER>>')
      endIndex = rawBuffer.indexOf('<<END>>')
    }
  })
}

function startPerplexityBridge(window: BrowserWindow) {
  const scriptPath = path.join(process.env.APP_ROOT, 'resources', 'perplexity_bridge.py')
  perplexityProcess = spawn('python', ['-u', scriptPath])

  perplexityProcess.stderr?.on('data', (d: any) => console.error('[PERPLEXITY ERR]', d.toString()))

  let rawBuffer = ''

  perplexityProcess.stdout.on('data', (chunk: any) => {
    rawBuffer += chunk.toString()

    const regex = /<<([A-Z_]+)>>(.*?)<<END>>/gs
    let match
    let lastIndex = 0

    while ((match = regex.exec(rawBuffer)) !== null) {
      const [_, tag, content] = match
      lastIndex = regex.lastIndex

      try {
        const json = JSON.parse(content)

        if (tag === 'CHUNK') window.webContents.send('perplexity:chunk', json)
        else if (tag === 'SOURCES') window.webContents.send('perplexity:sources', json)
        else if (tag === 'KEY_STATUS') window.webContents.send('key-status', json)
        else if (tag === 'SESSION_SET') window.webContents.send('session-set', json)
        else if (tag === 'SESSIONS') window.webContents.send('session-list', json)
        else if (tag === 'HISTORY') window.webContents.send('history-load', json)

      } catch (e) {
        console.error("Perplexity Parse Error:", e)
      }
    }

    if (lastIndex > 0) {
      rawBuffer = rawBuffer.substring(lastIndex)
    }
  })
}

function startMeetingBridge(window: BrowserWindow) {
  const scriptPath = path.join(process.env.APP_ROOT, 'resources', 'meeting_bridge.py')
  meetingProcess = spawn('python', ['-u', scriptPath])

  meetingProcess.stderr?.on('data', (d: any) => console.error('[MEETING ERR]', d.toString()))

  let rawBuffer = ''
  meetingProcess.stdout.on('data', (chunk: any) => {
    rawBuffer += chunk.toString()

    const regex = /<<([A-Z_]+)>>(.*?)<<END>>/gs
    let match
    let lastIndex = 0

    while ((match = regex.exec(rawBuffer)) !== null) {
      const [, tag, content] = match
      lastIndex = regex.lastIndex

      try {
        const json = JSON.parse(content)
        if (tag === 'MEETING_STATUS') window.webContents.send('meeting:status', json)
        else if (tag === 'MEETING_TRANSCRIPT') window.webContents.send('meeting:transcript', json)
        else if (tag === 'MEETING_UPDATE') window.webContents.send('meeting:update', json)
        else if (tag === 'MEETING_ANSWER') window.webContents.send('meeting:answer', json)
        else if (tag === 'MEETING_ANSWER_CHUNK') window.webContents.send('meeting:answer-chunk', json)
        else if (tag === 'MEETING_FINAL') window.webContents.send('meeting:final', json)
        else if (tag === 'MEETING_SETTINGS') window.webContents.send('meeting:settings', json)
        else if (tag === 'KEY_STATUS') window.webContents.send('key-status', json)
      } catch (e) {
        console.error('Meeting Parse Error:', e)
      }
    }

    if (lastIndex > 0) {
      rawBuffer = rawBuffer.substring(lastIndex)
    }
  })
}


let settingsProcess: any = null

function startSettingsBridge(window: BrowserWindow) {
  const scriptPath = path.join(process.env.APP_ROOT, 'resources', 'settings_bridge.py')
  settingsProcess = spawn('python', ['-u', scriptPath])

  settingsProcess.stderr?.on('data', (d: any) => console.error('[SETTINGS ERR]', d.toString()))

  let rawBuffer = ''
  settingsProcess.stdout.on('data', (chunk: any) => {
    rawBuffer += chunk.toString()
    const regex = /<<([A-Z_]+)>>(.*?)<<END>>/gs
    let match
    let lastIndex = 0

    while ((match = regex.exec(rawBuffer)) !== null) {
      const [, tag, content] = match
      lastIndex = regex.lastIndex

      try {
        const json = JSON.parse(content)
        if (tag === 'SETTINGS') window.webContents.send('settings:all', json)
        else if (tag === 'INTEGRATIONS') window.webContents.send('integrations:all', json)
        else if (tag === 'CALENDAR_AGENDA') window.webContents.send('calendar:agenda', json)
        else if (tag === 'INTEGRATION_EVENT') window.webContents.send('integration:event', json)
      } catch (e) {
        console.error('Settings Parse Error:', e)
      }
    }

    if (lastIndex > 0) {
      rawBuffer = rawBuffer.substring(lastIndex)
    }
  })
}

function startVoiceBridge(window: BrowserWindow) {
  let scriptName = 'voice_bridge_win.py'
  if (process.platform === 'darwin') {
    scriptName = 'voice_bridge_mac.py'
  }

  const scriptPath = path.join(process.env.APP_ROOT, 'resources', scriptName)
  voiceProcess = spawn('python', ['-u', scriptPath])

  voiceProcess.stderr?.on('data', (d: any) => console.error('[VOICE ERR]', d.toString()))

  let rawBuffer = ''
  voiceProcess.stdout.on('data', (chunk: any) => {
    rawBuffer += chunk.toString()

    let transcriptIndex = rawBuffer.indexOf('<<VOICE_TRANSCRIPT>>')
    let statusIndex = rawBuffer.indexOf('<<VOICE_STATUS>>')

    while (transcriptIndex !== -1 || statusIndex !== -1) {
      let nextTag = -1
      let tagName = ''

      if (transcriptIndex === -1) {
        nextTag = statusIndex
        tagName = 'VOICE_STATUS'
      } else if (statusIndex === -1) {
        nextTag = transcriptIndex
        tagName = 'VOICE_TRANSCRIPT'
      } else {
        nextTag = Math.min(transcriptIndex, statusIndex)
        tagName = transcriptIndex < statusIndex ? 'VOICE_TRANSCRIPT' : 'VOICE_STATUS'
      }

      const endIndex = rawBuffer.indexOf('<<END>>', nextTag)
      if (endIndex === -1) break

      const contentStart = nextTag + (tagName === 'VOICE_TRANSCRIPT' ? 20 : 16)
      if (contentStart < endIndex) {
        try {
          const jsonStr = rawBuffer.substring(contentStart, endIndex)
          const json = JSON.parse(jsonStr)
          if (tagName === 'VOICE_TRANSCRIPT') {
            window.webContents.send('voice:transcript', json)
          } else {
            window.webContents.send('voice:status', json)
            // Update voiceActive state based on status
            if (json.status === 'stopped' || json.status === 'disconnected') {
              voiceActive = false
            } else if (json.status === 'error') {
              voiceActive = false
              console.error('[VOICE ERROR]', json.error)
              // Show error dialog to user
              import('electron').then(({ dialog }) => {
                dialog.showErrorBox('Voice Recognition Error', `Deepgram Error: ${json.error}\n\nPlease check your .env file and API key.`)
              })
            } else if (json.status === 'connected' || json.status === 'listening') {
              voiceActive = true
            }
          }
        } catch (e) { console.error('Voice Parse Error:', e) }
      }

      rawBuffer = rawBuffer.substring(endIndex + 7)
      transcriptIndex = rawBuffer.indexOf('<<VOICE_TRANSCRIPT>>')
      statusIndex = rawBuffer.indexOf('<<VOICE_STATUS>>')
    }
  })

  voiceProcess.on('close', (code: any) => {
    console.log('[VOICE] Process exited with code:', code)
  })
}

function sendToVoiceBridge(command: string) {
  if (voiceProcess && voiceProcess.stdin) {
    voiceProcess.stdin.write(command + '\n')
  }
}

// Get the preferred display or fallback to cursor display
function getTargetDisplay() {
  const preferredId = store.get('preferredDisplayId') as number | null
  const displays = screen.getAllDisplays()

  // Try to use preferred display
  if (preferredId) {
    const found = displays.find(d => d.id === preferredId)
    if (found) {
      console.log(`📺 Using preferred display: ${found.id}`)
      return found
    }
  }

  // Fallback: display with cursor
  const cursor = screen.getCursorScreenPoint()
  const cursorDisplay = screen.getDisplayNearestPoint(cursor)
  console.log(`📺 Using cursor display: ${cursorDisplay.id}`)
  return cursorDisplay
}

function sizeToDisplay() {
  if (!win) return
  const d = getTargetDisplay()
  win.setBounds(d.workArea, false)
}

function toggleSearch() {
  if (!win) return
  if (searchVisible) {
    searchVisible = false
    win.webContents.send('cosmic:hiding')
    win.setIgnoreMouseEvents(true, { forward: true })
  } else {
    sizeToDisplay()
    searchVisible = true
    win.setIgnoreMouseEvents(false)
    win.webContents.send('cosmic:shown')
    win.focus()
  }
}

function invokeMeetingMode() {
  if (!win) return

  if (!searchVisible) {
    sizeToDisplay()
    searchVisible = true
    win.setIgnoreMouseEvents(false)
    win.webContents.send('cosmic:shown')
  } else {
    win.setIgnoreMouseEvents(false)
  }

  win.webContents.send('meeting:invoke')
  win.focus()
}

function createWindow() {
  win = new BrowserWindow({
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    alwaysOnTop: true,
    skipTaskbar: true,
    hasShadow: false,
    type: 'toolbar',
    webPreferences: {
      preload: path.join(__dirname, 'preload.mjs'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  })

  win.setIgnoreMouseEvents(true, { forward: true })
  if (VITE_DEV_SERVER_URL) win.loadURL(VITE_DEV_SERVER_URL)
  else win.loadFile(path.join(RENDERER_DIST, 'index.html'))
}

// Cleanup function to kill all child processes
function cleanupProcesses() {
  const kill = (p: any) => {
    if (p) {
      try {
        p.kill()
      } catch (e) { console.error('Error killing process:', e) }
    }
  }

  kill(mediaProcess); mediaProcess = null
  kill(windowProcess); windowProcess = null
  kill(geminiProcess); geminiProcess = null
  kill(perplexityProcess); perplexityProcess = null
  kill(weatherProcess); weatherProcess = null

  kill(settingsProcess); settingsProcess = null
  kill(voiceProcess); voiceProcess = null
  kill(meetingProcess); meetingProcess = null
}

// Monitor change detection
function handleDisplayChange(_event: any, display: Electron.Display) {
  console.log('📺 Display changed:', display.id)
  if (store.get('autoRepositionOnChange')) {
    sizeToDisplay()
  }
}

app.on('before-quit', () => {
  cleanupProcesses()
})

app.whenReady().then(() => {
  createWindow()
  if (win) {
    startMediaBridge(win)
    startWindowBridge(win)
    startGeminiBridge(win)
    startPerplexityBridge(win)
    startWeatherBridge(win)
    startMeetingBridge(win)

    startSettingsBridge(win)
    startVoiceBridge(win)
    sizeToDisplay()
    win.show()
  }

  // Listen for display changes
  screen.on('display-added', handleDisplayChange)
  screen.on('display-removed', handleDisplayChange)
  screen.on('display-metrics-changed', handleDisplayChange)

  ipcMain.on('cosmic:hide', () => { if (searchVisible) toggleSearch() })
  ipcMain.on('cosmic:toggle', toggleSearch)

  ipcMain.on('set-ignore-mouse-events', (event, ignore, options) => {
    const w = BrowserWindow.fromWebContents(event.sender)
    if (w) w.setIgnoreMouseEvents(ignore, options)
  })

  ipcMain.on('app:quit', () => { app.quit() })
  ipcMain.on('app:restart', () => {
    cleanupProcesses()
    if (win && !win.isDestroyed()) {
      win.destroy()
      win = null
    }
    app.relaunch()
    app.exit(0)
  })

  ipcMain.on('media:control', (_, a) => mediaProcess?.stdin.write(`${a}\n`))
  let volumeTimeout: NodeJS.Timeout | null = null;
  ipcMain.on('media:set_volume', async (_, l) => {
    if (volumeTimeout) clearTimeout(volumeTimeout);
    volumeTimeout = setTimeout(() => {
      mediaProcess?.stdin.write(`setvol:${l}\n`);
    }, 50);
  })

  ipcMain.on('settings:get-all', () => {
    settingsProcess?.stdin.write('GET_ALL_SETTINGS\n')
  })

  ipcMain.on('integrations:get-all', () => {
    settingsProcess?.stdin.write('GET_ALL_INTEGRATIONS\n')
  })

  ipcMain.on('calendar:get-agenda', () => {
    settingsProcess?.stdin.write('GET_CALENDAR_AGENDA\n')
  })

  // Voice IPC handlers
  ipcMain.on('voice:start', () => {
    sendToVoiceBridge('START')
  })
  ipcMain.on('voice:stop', () => {
    sendToVoiceBridge('STOP')
  })
  ipcMain.on('voice:set-key', (_, key: string) => {
    sendToVoiceBridge(`SET_KEY:${key}`)
  })

  ipcMain.on('meeting:start', (_, payload) => {
    if (meetingProcess?.stdin) {
      meetingProcess.stdin.write(`START_MEETING:${JSON.stringify(payload || {})}\n`)
    }
  })
  ipcMain.on('meeting:stop', () => {
    meetingProcess?.stdin?.write('STOP_MEETING\n')
  })
  ipcMain.on('meeting:pause', () => {
    meetingProcess?.stdin?.write('PAUSE_MEETING\n')
  })
  ipcMain.on('meeting:resume', () => {
    meetingProcess?.stdin?.write('RESUME_MEETING\n')
  })
  ipcMain.on('meeting:set-web-search', (_, payload) => {
    if (meetingProcess?.stdin) {
      meetingProcess.stdin.write(`SET_MEETING_WEB_SEARCH:${JSON.stringify(payload || {})}\n`)
    }
  })
  ipcMain.on('meeting:ask', (_, payload) => {
    if (meetingProcess?.stdin) {
      meetingProcess.stdin.write(`ASK_MEETING:${JSON.stringify(payload || {})}\n`)
    }
  })
  ipcMain.on('meeting:check-keys', () => {
    meetingProcess?.stdin?.write('CHECK_MEETING_KEYS\n')
  })
  ipcMain.on('meeting:get-settings', () => {
    meetingProcess?.stdin?.write('GET_MEETING_SETTINGS\n')
  })
  ipcMain.on('meeting:save-settings', (_, payload) => {
    if (meetingProcess?.stdin) {
      meetingProcess.stdin.write(`SAVE_MEETING_SETTINGS:${JSON.stringify(payload || {})}\n`)
    }
  })

  ipcMain.on('settings:save', (_, { key, value }) => {
    settingsProcess?.stdin.write(`SAVE_SETTING:${key}:${value}\n`)
  })

  ipcMain.on('integrations:save-account', (_, payload) => {
    settingsProcess?.stdin.write(`SAVE_INTEGRATION_ACCOUNT:${JSON.stringify(payload || {})}\n`)
  })

  ipcMain.on('integrations:delete-account', (_, accountId: string) => {
    settingsProcess?.stdin.write(`DELETE_INTEGRATION_ACCOUNT:${accountId}\n`)
  })

  ipcMain.on('integrations:connect-google', (_, payload) => {
    settingsProcess?.stdin.write(`CONNECT_GOOGLE_ACCOUNT:${JSON.stringify(payload || {})}\n`)
  })

  ipcMain.on('integrations:disconnect-google', (_, accountId: string) => {
    settingsProcess?.stdin.write(`DISCONNECT_GOOGLE_ACCOUNT:${accountId}\n`)
  })

  ipcMain.on('gemini:send', (_, prompt) => {
    if (geminiProcess?.stdin) {
      if (
        prompt.startsWith("CHECK_KEYS") ||
        prompt.startsWith("SAVE_KEYS") ||
        prompt.startsWith("LIST_SESSIONS") ||
        prompt.startsWith("LOAD_SESSION") ||
        prompt.startsWith("NEW_CHAT") ||
        prompt.startsWith("DELETE_SESSION")
      ) {
        geminiProcess.stdin.write(`${prompt}\n`)
      } else {
        geminiProcess.stdin.write(`PROMPT:${prompt}\n`)
      }
    }
  })

  ipcMain.on('perplexity:send', (_, prompt) => {
    if (perplexityProcess?.stdin) {
      if (
        prompt.startsWith("CHECK_KEYS") ||
        prompt.startsWith("SAVE_KEYS") ||
        prompt.startsWith("LIST_SESSIONS") ||
        prompt.startsWith("LOAD_SESSION") ||
        prompt.startsWith("NEW_CHAT") ||
        prompt.startsWith("DELETE_SESSION")
      ) {
        perplexityProcess.stdin.write(`${prompt}\n`)
      } else {
        perplexityProcess.stdin.write(`PROMPT:${prompt}\n`)
      }
    }
  })

  ipcMain.on('weather:request', (event) => {
    if (lastWeatherData) event.sender.send('weather:update', lastWeatherData)
  })

  // NEW: Open External Link
  ipcMain.on('open-external', (_, url) => {
    shell.openExternal(url)
  })

  ipcMain.handle('whatsapp:get-status', async (_, payload: GatewayConnectionConfig) => {
    const response = await callGatewayJson(payload, '/channels/whatsapp/status')
    return unwrapWhatsAppBridgePayload(response)
  })

  ipcMain.handle('whatsapp:get-config', async (_, payload: GatewayConnectionConfig) => {
    return callGatewayJson(payload, '/channels/whatsapp/config')
  })

  ipcMain.handle('whatsapp:request-pairing-qr', async (_, payload: GatewayConnectionConfig & {
    refresh?: boolean
    waitTimeoutMs?: number
  }) => {
    return callGatewayJson(
      payload,
      '/channels/whatsapp/pairing/qr',
      {
        method: 'POST',
        body: {
          refresh: payload?.refresh !== false,
          wait_timeout_ms: payload?.waitTimeoutMs ?? 15000,
        },
        timeoutMs: Math.max(5000, Number(payload?.waitTimeoutMs ?? 15000) + 5000),
      },
    )
  })

  ipcMain.handle('whatsapp:clear-session', async (_, payload: GatewayConnectionConfig) => {
    return callGatewayJson(payload, '/channels/whatsapp/session', {
      method: 'DELETE',
    })
  })

  ipcMain.handle('whatsapp:save-config', async (_, payload: GatewayConnectionConfig & {
    allowedPhone?: string | null
    selfChatOnly?: boolean | null
  }) => {
    return callGatewayJson(payload, '/channels/whatsapp/config', {
      method: 'POST',
      body: {
        allowed_phone: payload?.allowedPhone ?? null,
        self_chat_only: payload?.selfChatOnly ?? null,
      },
    })
  })

  ipcMain.handle('whatsapp:send-test', async (_, payload: GatewayConnectionConfig & { number: string; message: string }) => {
    return callGatewayJson(payload, '/channels/whatsapp/send', {
      method: 'POST',
      body: {
        number: payload.number,
        message: payload.message,
      },
    })
  })


  // --- AUTH IPC HANDLERS ---
  ipcMain.handle('auth:login', async (_, apiKey: string) => {
    try {
      const response = await fetch(
        `${SUPABASE_URL}/rest/v1/rpc/authenticate_with_api_key`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'apikey': SUPABASE_ANON_KEY,
            'Authorization': `Bearer ${SUPABASE_ANON_KEY}`,
          },
          body: JSON.stringify({ p_api_key: apiKey.trim() }),
        }
      )

      if (!response.ok) {
        return { success: false, error: 'network_error', message: `Server error: ${response.status}` }
      }

      const result = await response.json()

      if (!result.success) {
        return { success: false, error: result.error, message: result.message }
      }

      const authData = {
        apiKey: apiKey.trim(),
        userId: result.user.id,
        fullName: result.user.full_name,
        isPrivileged: result.user.is_privileged,
        gatewayUrl: result.vm.gateway_url,
        gatewayApiToken: result.vm.api_token,
        vmIp: result.vm.vm_ip,
        vmDns: result.vm.vm_dns,
        authenticatedAt: Date.now(),
      }

      // Persist auth to SQLite via settings bridge
      const authJson = JSON.stringify(authData)
      settingsProcess?.stdin.write(`SAVE_SETTING:cosmicAuth:${authJson}\n`)
      settingsProcess?.stdin.write(`SAVE_SETTING:gatewayBaseUrl:${result.vm.gateway_url}\n`)
      settingsProcess?.stdin.write(`SAVE_SETTING:gatewayApiToken:${result.vm.api_token}\n`)

      return { success: true, ...authData }
    } catch (error: any) {
      return {
        success: false,
        error: 'network_error',
        message: error?.message || 'Unable to connect to authentication server.',
      }
    }
  })

  ipcMain.handle('auth:logout', () => {
    settingsProcess?.stdin.write('SAVE_SETTING:cosmicAuth:\n')
    settingsProcess?.stdin.write('SAVE_SETTING:gatewayBaseUrl:\n')
    settingsProcess?.stdin.write('SAVE_SETTING:gatewayApiToken:\n')
    return { success: true }
  })

  // Multi-monitor IPC handlers
  ipcMain.handle('get-all-displays', () => {
    const displays = screen.getAllDisplays()
    const primary = screen.getPrimaryDisplay()
    const preferredId = store.get('preferredDisplayId') as number | null

    return displays.map(d => ({
      id: d.id,
      label: d.label || `Display ${d.id}`,
      bounds: d.bounds,
      workArea: d.workArea,
      scaleFactor: d.scaleFactor,
      rotation: d.rotation,
      isPrimary: d.id === primary.id,
      isPreferred: d.id === preferredId
    }))
  })

  ipcMain.on('set-preferred-display', (event, displayId: number) => {
    console.log(`📺 Setting preferred display to: ${displayId}`)
    store.set('preferredDisplayId', displayId)
    sizeToDisplay()
    event.sender.send('display-preferences-updated', displayId)
  })

  globalShortcut.register('CommandOrControl+Shift+Space', toggleSearch)

  // Meeting mode shortcut - CommandOrControl+Left: affect only meeting UI
  const meetingShortcutRegistered = globalShortcut.register('CommandOrControl+Left', () => {
    if (!win) return
    // If overlay is not visible, invoke meeting UI in its last state
    if (!searchVisible) {
      invokeMeetingMode()
      return
    }
    // If overlay is visible, let the renderer decide based on current mode
    win.webContents.send('meeting:toggle-visibility')
  })
  if (!meetingShortcutRegistered) {
    console.warn('Failed to register meeting shortcut: CommandOrControl+Left')
  }

  // Voice activation shortcut - CommandOrControl+Shift+V to toggle voice
  globalShortcut.register('CommandOrControl+Shift+V', () => {
    // Toggle voice on/off
    if (voiceActive) {
      sendToVoiceBridge('STOP')
      voiceActive = false
    } else {
      sendToVoiceBridge('START')
      voiceActive = true
    }
  })
})

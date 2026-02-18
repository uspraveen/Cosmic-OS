import { app, BrowserWindow, globalShortcut, ipcMain, screen, shell } from 'electron'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { spawn } from 'node:child_process'
import Store from 'electron-store'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
process.env.APP_ROOT = path.join(__dirname, '..')

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
let calendarProcess: any = null
let searchVisible = false
let lastWeatherData: any = null

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
      const [fullMatch, tag, content] = match
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

function startCalendarBridge(window: BrowserWindow) {
  const scriptPath = path.join(process.env.APP_ROOT, 'resources', 'calendar_bridge.py')
  calendarProcess = spawn('python', ['-u', scriptPath])

  // ADDED: Error logging to catch crashes (e.g., missing libraries/credentials)
  calendarProcess.stderr?.on('data', (d: any) => console.error('[CALENDAR ERR]', d.toString()))

  let rawBuffer = ''
  calendarProcess.stdout.on('data', (chunk: any) => {
    rawBuffer += chunk.toString()
    let startIndex = rawBuffer.indexOf('<<CALENDAR>>')
    let endIndex = rawBuffer.indexOf('<<END>>')
    while (startIndex !== -1 && endIndex !== -1) {
      if (endIndex > startIndex) {
        try {
          const jsonStr = rawBuffer.substring(startIndex + 12, endIndex)
          window.webContents.send('calendar:update', JSON.parse(jsonStr))
        } catch (e) { console.error('Calendar Parse Error:', e) }
        rawBuffer = rawBuffer.substring(endIndex + 7)
      } else { rawBuffer = rawBuffer.substring(startIndex) }
      startIndex = rawBuffer.indexOf('<<CALENDAR>>')
      endIndex = rawBuffer.indexOf('<<END>>')
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
    let startIndex = rawBuffer.indexOf('<<SETTINGS>>')
    let endIndex = rawBuffer.indexOf('<<END>>')
    while (startIndex !== -1 && endIndex !== -1) {
      if (endIndex > startIndex) {
        try {
          const jsonStr = rawBuffer.substring(startIndex + 12, endIndex)
          window.webContents.send('settings:all', JSON.parse(jsonStr))
        } catch (e) { console.error('Settings Parse Error:', e) }
        rawBuffer = rawBuffer.substring(endIndex + 7)
      } else { rawBuffer = rawBuffer.substring(startIndex) }
      startIndex = rawBuffer.indexOf('<<SETTINGS>>')
      endIndex = rawBuffer.indexOf('<<END>>')
    }
  })
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
  kill(calendarProcess); calendarProcess = null
  kill(settingsProcess); settingsProcess = null
}

// Monitor change detection
function handleDisplayChange(event: any, display: Electron.Display) {
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
    startCalendarBridge(win)
    startSettingsBridge(win)
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
  ipcMain.on('media:set_volume', (_, l) => mediaProcess?.stdin.write(`setvol:${l}\n`))

  ipcMain.on('settings:get-all', () => {
    settingsProcess?.stdin.write('GET_ALL_SETTINGS\n')
  })

  ipcMain.on('settings:save', (_, { key, value }) => {
    settingsProcess?.stdin.write(`SAVE_SETTING:${key}:${value}\n`)
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

  ipcMain.on('calendar:save-url', (_, url) => {
    if (calendarProcess?.stdin) {
      // Send the "SAVE_URL:" command to our new Python script
      calendarProcess.stdin.write(`SAVE_URL:${url}\n`)
    }
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
})
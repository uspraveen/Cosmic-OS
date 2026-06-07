import { app, BrowserWindow, clipboard, dialog, globalShortcut, ipcMain, nativeImage, screen, shell } from 'electron'
import { existsSync, promises as fs, readFileSync } from 'node:fs'
import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import { PNG } from 'pngjs'
import { fileURLToPath } from 'node:url'
import { createClient } from '@supabase/supabase-js'
import path from 'node:path'
import { spawn } from 'node:child_process'
import Store from 'electron-store'
import { GatewayConnectionManager, type GatewayConnectionConfig as PersistentGatewayConnectionConfig } from './gatewayConnectionManager'

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

const APP_ICON_FILENAME = 'cosmic-ball-logo-v1.1.png'

/** Resolves the window/taskbar icon for dev (public/), production build (dist/), or packaged app (same paths inside asar). */
function resolveAppIconPath(): string | undefined {
  const root = process.env.APP_ROOT || path.join(__dirname, '..')
  const candidates = VITE_DEV_SERVER_URL
    ? [path.join(root, 'public', APP_ICON_FILENAME)]
    : [path.join(root, 'dist', APP_ICON_FILENAME), path.join(root, 'public', APP_ICON_FILENAME)]
  for (const candidate of candidates) {
    if (existsSync(candidate)) {
      return candidate
    }
  }
  return undefined
}

/** Windows taskbar squares are tight; nudge raster artwork slightly right inside the canvas. */
const WINDOWS_TASKBAR_ICON_NUDGE_PX = 3

/**
 * Center-crop then scale back to the original pixel size so the subject fills more of the icon.
 * Use `1` to disable (e.g. hand-cropped artwork); values above 1 zoom in (tighter crop).
 */
const WINDOWS_TASKBAR_ICON_CENTER_ZOOM = 1

function zoomCenterCropPngToOriginalSize(img: Electron.NativeImage, zoom: number): Electron.NativeImage {
  if (zoom <= 1) return img
  const { width, height } = img.getSize()
  if (width < 4 || height < 4) return img
  const cropW = Math.max(1, Math.round(width / zoom))
  const cropH = Math.max(1, Math.round(height / zoom))
  const x = Math.floor((width - cropW) / 2)
  const y = Math.floor((height - cropH) / 2)
  const cropped = img.crop({ x, y, width: cropW, height: cropH })
  return cropped.resize({ width, height, quality: 'best' })
}

function shiftPngContentRight(pngBuffer: Buffer, dx: number): Buffer | null {
  if (dx <= 0) return null
  try {
    const png = PNG.sync.read(pngBuffer)
    const { width, height, data } = png
    const out = new PNG({ width, height })
    out.data.fill(0)
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        const destIdx = (width * y + x) << 2
        const sx = x - dx
        if (sx >= 0) {
          const srcIdx = (width * y + sx) << 2
          out.data[destIdx] = data[srcIdx]
          out.data[destIdx + 1] = data[srcIdx + 1]
          out.data[destIdx + 2] = data[srcIdx + 2]
          out.data[destIdx + 3] = data[srcIdx + 3]
        }
      }
    }
    return Buffer.from(PNG.sync.write(out))
  } catch {
    return null
  }
}

function loadBrowserWindowIcon(iconPath: string): Electron.NativeImage | undefined {
  if (process.platform === 'win32') {
    try {
      const raw = readFileSync(iconPath)
      let image = nativeImage.createFromBuffer(raw)
      if (!image.isEmpty()) {
        image = zoomCenterCropPngToOriginalSize(image, WINDOWS_TASKBAR_ICON_CENTER_ZOOM)
        const zoomedPng = image.toPNG()
        const shifted = shiftPngContentRight(zoomedPng, WINDOWS_TASKBAR_ICON_NUDGE_PX)
        if (shifted) {
          image = nativeImage.createFromBuffer(shifted)
          if (!image.isEmpty()) return image
        }
      }
    } catch {
      // fall through to path-based load
    }
  }
  const fallback = nativeImage.createFromPath(iconPath)
  return fallback.isEmpty() ? undefined : fallback
}

// Electron Store for persisting settings
const store = new Store({
  defaults: {
    preferredDisplayId: null,
    autoRepositionOnChange: true,
    cosmicAuth: null,
    gatewayBaseUrl: '',
    gatewayApiToken: '',
    cosmicMailBaseUrl: '',
    cosmicMailApiToken: '',
    desktopDeviceId: '',
  }
})

let win: BrowserWindow | null = null
let mediaProcess: any = null
let windowProcess: any = null
let weatherProcess: any = null
let meetingProcess: any = null

let voiceProcess: any = null
let voiceActive = false
let searchVisible = false
let lastWeatherData: any = null
let gatewayConnectionManager: GatewayConnectionManager | null = null
let lastAppliedDisplayId: number | null = null
let lastAppliedScaleFactor: number | null = null

interface GatewayConnectionConfig {
  baseUrl: string
  apiToken: string
  deviceId?: string
}

interface PickedGatewayDocument {
  filePath: string
  filename: string
  mimeType: string
  sizeBytes: number
}

interface PendingGatewayDocumentUpload {
  filePath: string
  filename: string
  mimeType?: string
  sizeBytes?: number
}

const GATEWAY_DOCUMENT_UPLOAD_MAX_FILE_BYTES = 20 * 1024 * 1024
const GATEWAY_MAX_IMAGE_ATTACHMENTS_PER_MESSAGE = 20

function formatBinarySize(sizeBytes: number) {
  if (!Number.isFinite(sizeBytes) || sizeBytes <= 0) {
    return '0 B'
  }
  if (sizeBytes >= 1024 * 1024) {
    return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MB`
  }
  if (sizeBytes >= 1024) {
    return `${Math.max(1, Math.round(sizeBytes / 1024))} KB`
  }
  return `${Math.max(1, Math.round(sizeBytes))} B`
}

function buildDocumentSizeLimitError(items: Array<{ filename: string; sizeBytes: number }>) {
  const rendered = items
    .slice(0, 3)
    .map((item) => `${item.filename} (${formatBinarySize(item.sizeBytes)})`)
    .join(', ')
  const suffix = items.length > 3 ? ` and ${items.length - 3} more` : ''
  return `Attachments larger than ${formatBinarySize(GATEWAY_DOCUMENT_UPLOAD_MAX_FILE_BYTES)} are not supported yet: ${rendered}${suffix}.`
}

function isImageMimeType(mimeType: string | undefined | null) {
  return String(mimeType || '').trim().toLowerCase().startsWith('image/')
}

function countImageAttachments(items: Array<{ mimeType?: string | null }>) {
  return items.reduce((count, item) => count + (isImageMimeType(item?.mimeType) ? 1 : 0), 0)
}

function buildImageAttachmentLimitError(imageCount: number) {
  return `Up to ${GATEWAY_MAX_IMAGE_ATTACHMENTS_PER_MESSAGE} images can be attached in one message. You selected ${imageCount}.`
}

function getDesktopDeviceId() {
  const existing = String(store.get('desktopDeviceId') || '').trim()
  if (existing) {
    return existing
  }
  const generated = `desk_${crypto.randomUUID().replace(/-/g, '').slice(0, 16)}`
  store.set('desktopDeviceId', generated)
  settingsProcess?.stdin.write(`SAVE_SETTING:desktopDeviceId:${generated}\n`)
  return generated
}

function getStoredGatewayTransportConfig(): PersistentGatewayConnectionConfig | null {
  const baseUrl = String(store.get('gatewayBaseUrl') || '').trim()
  const apiToken = String(store.get('gatewayApiToken') || '').trim()
  if (!baseUrl || !apiToken) {
    return null
  }
  return {
    baseUrl,
    apiToken,
    deviceId: getDesktopDeviceId(),
  }
}

function configureGatewayConnection() {
  if (!gatewayConnectionManager) {
    return
  }
  gatewayConnectionManager.configure(getStoredGatewayTransportConfig())
  scheduleMeetingUsageFlush(1000)
}

function inferDesktopAttachmentMimeType(filename: string) {
  const extension = path.extname(String(filename || '')).toLowerCase()
  if (extension === '.pdf') return 'application/pdf'
  if (extension === '.docx') return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
  if (extension === '.pptx') return 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
  if (extension === '.xlsx') return 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
  if (extension === '.csv') return 'text/csv'
  if (extension === '.txt') return 'text/plain'
  if (extension === '.md') return 'text/markdown'
  if (extension === '.json') return 'application/json'
  if (extension === '.zip') return 'application/zip'
  if (extension === '.svg') return 'image/svg+xml'
  if (extension === '.jpg' || extension === '.jpeg') return 'image/jpeg'
  if (extension === '.png') return 'image/png'
  if (extension === '.gif') return 'image/gif'
  if (extension === '.webp') return 'image/webp'
  return 'application/octet-stream'
}

function inferExtensionFromMimeType(mimeType: string | undefined | null): string {
  const normalized = String(mimeType || '').trim().toLowerCase().split(';', 1)[0]
  if (!normalized) return ''
  if (normalized === 'application/pdf') return 'pdf'
  if (normalized === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document') return 'docx'
  if (normalized === 'application/vnd.openxmlformats-officedocument.presentationml.presentation') return 'pptx'
  if (normalized === 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet') return 'xlsx'
  if (normalized === 'text/csv') return 'csv'
  if (normalized === 'text/plain') return 'txt'
  if (normalized === 'text/markdown') return 'md'
  if (normalized === 'application/json') return 'json'
  if (normalized === 'application/zip') return 'zip'
  if (normalized === 'image/svg+xml') return 'svg'
  if (normalized === 'image/jpeg') return 'jpg'
  if (normalized === 'image/png') return 'png'
  if (normalized === 'image/gif') return 'gif'
  if (normalized === 'image/webp') return 'webp'
  return ''
}

function resolveDownloadFilename(filename: string, mimeType?: string | null) {
  const trimmed = String(filename || '').trim() || 'download'
  const existingExtension = path.extname(trimmed).replace(/^\./, '').toLowerCase()
  const inferredExtension = existingExtension || inferExtensionFromMimeType(mimeType)
  const normalizedFilename = existingExtension || !inferredExtension
    ? trimmed
    : `${trimmed}.${inferredExtension}`
  return {
    filename: normalizedFilename,
    extension: inferredExtension,
  }
}

function buildSaveDialogOptions(title: string, filename: string, mimeType?: string | null): Electron.SaveDialogOptions {
  const resolved = resolveDownloadFilename(filename, mimeType)
  const options: Electron.SaveDialogOptions = {
    title,
    defaultPath: resolved.filename,
  }
  if (resolved.extension) {
    options.filters = [
      {
        name: resolved.extension.toUpperCase(),
        extensions: [resolved.extension],
      },
      {
        name: 'All files',
        extensions: ['*'],
      },
    ]
  }
  return options
}

function ensureFilePathExtension(filePath: string, mimeType?: string | null) {
  if (path.extname(String(filePath || '').trim())) {
    return filePath
  }
  const extension = inferExtensionFromMimeType(mimeType)
  if (!extension) {
    return filePath
  }
  return `${filePath}.${extension}`
}

async function pickGatewayDocuments() {
  if (!win) {
    throw new Error('Main window is not available.')
  }
  const result = await dialog.showOpenDialog(win, {
    title: 'Attach files',
    properties: ['openFile', 'multiSelections'],
    filters: [
      {
        name: 'Documents',
        extensions: ['pdf', 'docx', 'pptx'],
      },
      {
        name: 'Images',
        extensions: ['jpg', 'jpeg', 'png', 'gif', 'webp'],
      },
    ],
  })
  if (result.canceled || result.filePaths.length === 0) {
    return []
  }

  let pickedImageCount = 0
  const picked: PickedGatewayDocument[] = []
  const oversized: Array<{ filename: string; sizeBytes: number }> = []
  for (const filePath of result.filePaths) {
    const filename = path.basename(filePath)
    const stats = await fs.stat(filePath)
    if (!stats.isFile()) {
      continue
    }
    if (stats.size > GATEWAY_DOCUMENT_UPLOAD_MAX_FILE_BYTES) {
      oversized.push({ filename, sizeBytes: stats.size })
      continue
    }
    const mimeType = inferDesktopAttachmentMimeType(filename)
    if (isImageMimeType(mimeType)) {
      pickedImageCount += 1
    }
    picked.push({
      filePath,
      filename,
      mimeType,
      sizeBytes: stats.size,
    })
  }
  if (oversized.length > 0) {
    throw new Error(buildDocumentSizeLimitError(oversized))
  }
  if (pickedImageCount > GATEWAY_MAX_IMAGE_ATTACHMENTS_PER_MESSAGE) {
    throw new Error(buildImageAttachmentLimitError(pickedImageCount))
  }
  return picked
}

async function readClipboardImageAsGatewayDocument() {
  const image = clipboard.readImage()
  if (image.isEmpty()) {
    return null
  }
  const pngBytes = image.toPNG()
  if (!pngBytes.length) {
    return null
  }
  if (pngBytes.length > GATEWAY_DOCUMENT_UPLOAD_MAX_FILE_BYTES) {
    throw new Error(buildDocumentSizeLimitError([{ filename: 'pasted image', sizeBytes: pngBytes.length }]))
  }

  const dir = path.join(app.getPath('temp'), 'cosmic-os', 'clipboard-images')
  await fs.mkdir(dir, { recursive: true })
  const stamp = new Date().toISOString().replace(/[:.]/g, '-')
  const filename = `pasted-image-${stamp}.png`
  const filePath = path.join(dir, filename)
  await fs.writeFile(filePath, pngBytes)
  return {
    filePath,
    filename,
    mimeType: 'image/png',
    sizeBytes: pngBytes.length,
  } satisfies PickedGatewayDocument
}

async function uploadDesktopDocumentsToGateway(
  config: PersistentGatewayConnectionConfig,
  requestId: string,
  sessionId: string,
  attachments: PendingGatewayDocumentUpload[],
) {
  if (!requestId.trim()) {
    throw new Error('requestId is required before uploading attachments.')
  }
  if (!sessionId.trim()) {
    throw new Error('Desktop session is not ready yet. Reconnect to the VM and retry.')
  }
  const uploadUrl = new URL('/channels/desktop/uploads', `${config.baseUrl.replace(/\/$/, '')}/`).toString()
  const formData = new FormData()
  formData.set('request_id', requestId)
  formData.set('session_id', sessionId)
  formData.set('device_id', config.deviceId)

  const imageCount = countImageAttachments(attachments)
  if (imageCount > GATEWAY_MAX_IMAGE_ATTACHMENTS_PER_MESSAGE) {
    throw new Error(buildImageAttachmentLimitError(imageCount))
  }

  for (const attachment of attachments) {
    const filePath = String(attachment?.filePath || '').trim()
    const filename = String(attachment?.filename || '').trim() || path.basename(filePath)
    if (!filePath || !filename) {
      continue
    }
    const sizeBytes = Number(attachment?.sizeBytes || 0)
    if (Number.isFinite(sizeBytes) && sizeBytes > GATEWAY_DOCUMENT_UPLOAD_MAX_FILE_BYTES) {
      throw new Error(buildDocumentSizeLimitError([{ filename, sizeBytes }]))
    }
    const fileBytes = await fs.readFile(filePath)
    if (fileBytes.length > GATEWAY_DOCUMENT_UPLOAD_MAX_FILE_BYTES) {
      throw new Error(buildDocumentSizeLimitError([{ filename, sizeBytes: fileBytes.length }]))
    }
    const mimeType = String(attachment?.mimeType || '').trim() || inferDesktopAttachmentMimeType(filename)
    formData.append('files', new Blob([fileBytes], { type: mimeType }), filename)
  }

  const response = await fetch(uploadUrl, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${config.apiToken}`,
    },
    body: formData,
  })
  if (!response.ok) {
    let detail = `Attachment upload failed (${response.status})`
    try {
      const body = await response.json()
      const remoteDetail = typeof body?.detail === 'string' ? body.detail.trim() : ''
      if (remoteDetail) {
        detail = remoteDetail
      }
    } catch {
      // ignore parse failure and fall back to generic status text
    }
    throw new Error(detail)
  }

  const payload = await response.json()
  return Array.isArray(payload?.attachments) ? payload.attachments : []
}

function syncGatewaySettingsFromPayload(settings: any) {
  if (!settings || typeof settings !== 'object') {
    return
  }

  const gatewayBaseUrl = String(settings.gatewayBaseUrl || '').trim()
  const gatewayApiToken = String(settings.gatewayApiToken || '').trim()
  const desktopDeviceId = String(settings.desktopDeviceId || '').trim()
  const cosmicAuthRaw = settings.cosmicAuth

  store.set('gatewayBaseUrl', gatewayBaseUrl)
  store.set('gatewayApiToken', gatewayApiToken)
  if (desktopDeviceId) {
    store.set('desktopDeviceId', desktopDeviceId)
  }

  if (typeof cosmicAuthRaw === 'string' && cosmicAuthRaw.trim()) {
    try {
      store.set('cosmicAuth', JSON.parse(cosmicAuthRaw))
    } catch {
      store.set('cosmicAuth', cosmicAuthRaw)
    }
  } else if (cosmicAuthRaw && typeof cosmicAuthRaw === 'object') {
    store.set('cosmicAuth', cosmicAuthRaw)
  } else if (!cosmicAuthRaw) {
    store.set('cosmicAuth', null)
  }

  configureGatewayConnection()

  if ('cosmicMailBaseUrl' in settings) {
    store.set('cosmicMailBaseUrl', String(settings.cosmicMailBaseUrl ?? '').trim())
  }
  if ('cosmicMailApiToken' in settings) {
    store.set('cosmicMailApiToken', String(settings.cosmicMailApiToken ?? '').trim())
  }
}

const GATEWAY_SYSTEM_METRIC_PRIMARY_PATH = '/desktop/system-metrics'
const GATEWAY_SYSTEM_METRIC_FALLBACK_PATHS = [
  '/health/ready',
  '/health',
]
const GATEWAY_SYSTEM_METRICS_CACHE_TTL_MS = 15_000

export type GatewayUsagePeriodOptions = {
  usage_days?: number
  usage_hours?: number
  usage_start?: string
  usage_end?: string
}

let gatewaySystemMetricsCache:
  | { cacheKey: string; fetchedAt: number; payload: any }
  | null = null
let gatewaySystemMetricsInFlight:
  | { cacheKey: string; promise: Promise<any> }
  | null = null

function appendSystemMetricsUsageParams(params: URLSearchParams, usage?: GatewayUsagePeriodOptions) {
  if (!usage) return
  if (usage.usage_days != null && Number.isFinite(usage.usage_days)) {
    params.set('usage_days', String(Math.trunc(Number(usage.usage_days))))
  }
  if (usage.usage_hours != null && Number.isFinite(usage.usage_hours)) {
    params.set('usage_hours', String(Math.trunc(Number(usage.usage_hours))))
  }
  const start = usage.usage_start != null ? String(usage.usage_start).trim() : ''
  if (start) params.set('usage_start', start)
  const end = usage.usage_end != null ? String(usage.usage_end).trim() : ''
  if (end) params.set('usage_end', end)
}

function gatewaySystemMetricsCacheKey(config: GatewayConnectionConfig, usage?: GatewayUsagePeriodOptions) {
  const base = `${normalizeGatewayBaseUrl(config?.baseUrl || '')}|${String(config?.apiToken || '').trim()}`
  if (!usage) return base
  const params = new URLSearchParams()
  appendSystemMetricsUsageParams(params, usage)
  const qs = params.toString()
  return qs ? `${base}|${qs}` : base
}

async function fetchGatewaySystemMetrics(
  config: GatewayConnectionConfig,
  forceRefresh = false,
  usage?: GatewayUsagePeriodOptions,
) {
  const params = new URLSearchParams()
  if (forceRefresh) params.set('force_refresh', '1')
  appendSystemMetricsUsageParams(params, usage)
  const qs = params.toString()
  const primaryPath =
    qs.length > 0 ? `${GATEWAY_SYSTEM_METRIC_PRIMARY_PATH}?${qs}` : GATEWAY_SYSTEM_METRIC_PRIMARY_PATH

  let lastError: unknown = null
  try {
    const payload = await callGatewayJson(config, primaryPath, { timeoutMs: 5000 })
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
      return {
        sourceEndpoint: GATEWAY_SYSTEM_METRIC_PRIMARY_PATH,
        source: 'gateway-system-metrics',
        fetchedAt: Date.now(),
        value: payload,
      }
    }
    return {
      ...payload,
      sourceEndpoint: payload.sourceEndpoint || GATEWAY_SYSTEM_METRIC_PRIMARY_PATH,
      source: payload.source || 'gateway-system-metrics',
      fetchedAt: payload.fetchedAt || Date.now(),
    }
  } catch (error: unknown) {
    lastError = error
  }

  for (const pathName of GATEWAY_SYSTEM_METRIC_FALLBACK_PATHS) {
    try {
      const payload = await callGatewayJson(config, pathName, { timeoutMs: 4000 })
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
        return {
          sourceEndpoint: pathName,
          source: 'gateway-system-metrics',
          fetchedAt: Date.now(),
          value: payload,
        }
      }
      return {
        ...payload,
        sourceEndpoint: pathName,
        source: 'gateway-system-metrics',
        fetchedAt: Date.now(),
      }
    } catch (error: unknown) {
      lastError = error
    }
  }

  if (lastError instanceof Error) {
    throw lastError
  }
  throw new Error('Gateway system metrics endpoint is unavailable.')
}

async function getGatewaySystemMetrics(
  config: GatewayConnectionConfig,
  forceRefresh = false,
  usage?: GatewayUsagePeriodOptions,
) {
  const cacheKey = gatewaySystemMetricsCacheKey(config, usage)
  const now = Date.now()
  if (
    !forceRefresh &&
    gatewaySystemMetricsCache &&
    gatewaySystemMetricsCache.cacheKey === cacheKey &&
    now - gatewaySystemMetricsCache.fetchedAt < GATEWAY_SYSTEM_METRICS_CACHE_TTL_MS
  ) {
    return gatewaySystemMetricsCache.payload
  }
  if (
    gatewaySystemMetricsInFlight &&
    gatewaySystemMetricsInFlight.cacheKey === cacheKey
  ) {
    return gatewaySystemMetricsInFlight.promise
  }

  const request = fetchGatewaySystemMetrics(config, forceRefresh, usage).then((payload) => {
    gatewaySystemMetricsCache = {
      cacheKey,
      fetchedAt: Date.now(),
      payload,
    }
    return payload
  }).finally(() => {
    if (gatewaySystemMetricsInFlight?.promise === request) {
      gatewaySystemMetricsInFlight = null
    }
  })

  gatewaySystemMetricsInFlight = {
    cacheKey,
    promise: request,
  }
  return request
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

function formatTransportError(serviceLabel: string, baseUrl: string, error: any) {
  if (error?.name === 'AbortError') {
    return `${serviceLabel} request timed out.`
  }

  const causeCode = String(error?.cause?.code || error?.code || '').trim().toUpperCase()
  const causeMessage = String(error?.cause?.message || error?.message || '').trim()

  if (causeCode === 'ECONNREFUSED') {
    return `${serviceLabel} is not reachable at ${baseUrl}. The server is not accepting connections on that address or port.`
  }
  if (causeCode === 'ENOTFOUND') {
    return `${serviceLabel} host could not be resolved for ${baseUrl}. Check the URL and hostname.`
  }
  if (causeCode === 'ECONNRESET') {
    return `${serviceLabel} connection was reset by ${baseUrl}. Check whether the server or a proxy closed the connection.`
  }
  if (
    causeCode === 'CERT_HAS_EXPIRED' ||
    causeCode === 'DEPTH_ZERO_SELF_SIGNED_CERT' ||
    causeCode === 'ERR_TLS_CERT_ALTNAME_INVALID'
  ) {
    return `${serviceLabel} TLS validation failed for ${baseUrl}. Check whether the URL should use http instead of https, or whether the certificate is valid.`
  }
  if (causeMessage && causeMessage !== 'fetch failed') {
    return `${serviceLabel} request failed: ${causeMessage}`
  }
  return `${serviceLabel} could not be reached at ${baseUrl}. Check whether the server is running and whether the URL uses the correct http/https scheme.`
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
        ...(config?.deviceId ? { 'X-Device-Id': String(config.deviceId).trim() } : {}),
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
    throw new Error(formatTransportError('Gateway', baseUrl, error))
  } finally {
    clearTimeout(timeout)
  }
}

const MEETING_USAGE_QUEUE_STORE_KEY = 'meetingUsageQueueV1'
const MEETING_USAGE_QUEUE_LIMIT = 1000
let meetingUsageFlushTimer: ReturnType<typeof setTimeout> | null = null
let meetingUsageFlushBusy = false

function readMeetingUsageQueue(): any[] {
  const raw = store.get(MEETING_USAGE_QUEUE_STORE_KEY)
  return Array.isArray(raw) ? raw.filter((item) => item && typeof item === 'object') : []
}

function writeMeetingUsageQueue(items: any[]) {
  store.set(MEETING_USAGE_QUEUE_STORE_KEY, items.slice(-MEETING_USAGE_QUEUE_LIMIT))
}

function scheduleMeetingUsageFlush(delayMs = 750) {
  if (meetingUsageFlushTimer) return
  meetingUsageFlushTimer = setTimeout(() => {
    meetingUsageFlushTimer = null
    flushMeetingUsageQueue().catch((error) => {
      console.error('[meeting usage] flush failed', error)
    })
  }, Math.max(0, delayMs))
}

function enqueueMeetingUsageEvent(payload: any) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return
  }
  const queue = readMeetingUsageQueue()
  queue.push(payload)
  writeMeetingUsageQueue(queue)
  scheduleMeetingUsageFlush()
}

async function flushMeetingUsageQueue() {
  if (meetingUsageFlushBusy) return
  const config = getStoredGatewayTransportConfig()
  if (!config) return

  meetingUsageFlushBusy = true
  try {
    let queue = readMeetingUsageQueue()
    while (queue.length > 0) {
      const event = queue[0]
      try {
        await callGatewayJson(config, '/desktop/usage/log', {
          method: 'POST',
          body: event,
          timeoutMs: 8000,
        })
        queue = queue.slice(1)
        writeMeetingUsageQueue(queue)
        gatewaySystemMetricsCache = null
      } catch (error) {
        console.error('[meeting usage] queued event could not be posted', error)
        break
      }
    }
  } finally {
    meetingUsageFlushBusy = false
  }
}

async function callCosmicMailJson(
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
    throw new Error('Cosmic Mail API token is required.')
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
        `Cosmic Mail request failed (${response.status})`
      throw new Error(detail)
    }

    return payload
  } catch (error: any) {
    throw new Error(formatTransportError('Cosmic Mail', baseUrl, error))
  } finally {
    clearTimeout(timeout)
  }
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
        else if (tag === 'MEETING_USAGE') enqueueMeetingUsageEvent(json)
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

type CosmicMailDbPending = {
  resolve: (value: { ok: boolean; result?: unknown; error?: string | null }) => void
  reject: (reason?: unknown) => void
  timer: ReturnType<typeof setTimeout>
}
const cosmicMailDbPending = new Map<string, CosmicMailDbPending>()
let cosmicMailDbRequestSeq = 0

function cosmicMailDbRequest(payload: Record<string, unknown>): Promise<{ ok: boolean; result?: unknown; error?: string | null }> {
  return new Promise((resolve, reject) => {
    if (!settingsProcess?.stdin) {
      reject(new Error('Settings bridge is not ready.'))
      return
    }
    const requestId = `cm_${Date.now()}_${++cosmicMailDbRequestSeq}`
    const line = `COSMIC_MAIL_DB:${JSON.stringify({ ...payload, requestId })}\n`
    const timer = setTimeout(() => {
      const pending = cosmicMailDbPending.get(requestId)
      if (!pending) return
      cosmicMailDbPending.delete(requestId)
      pending.reject(new Error('Cosmic Mail DB request timed out.'))
    }, 12_000)
    cosmicMailDbPending.set(requestId, { resolve, reject, timer })
    try {
      settingsProcess.stdin.write(line)
    } catch (err) {
      clearTimeout(timer)
      cosmicMailDbPending.delete(requestId)
      reject(err)
    }
  })
}

let cosmicMailPollInterval: ReturnType<typeof setInterval> | null = null
let cosmicMailPollBusy = false

function normalizeCosmicMailListResponse(payload: unknown): any[] {
  if (Array.isArray(payload)) return payload
  if (payload && typeof payload === 'object') {
    const obj = payload as Record<string, unknown>
    if (Array.isArray(obj.items)) return obj.items
    if (Array.isArray(obj.data)) return obj.data
  }
  return []
}

function pickPreferredCosmicMailOrganization(authContext: any, organizations: any[]): any | null {
  const orgs = Array.isArray(organizations) ? organizations : []
  const prefId = authContext?.organization_id
  if (prefId) {
    const byId = orgs.find((o: any) => o?.id === prefId)
    if (byId) return byId
  }
  const cosmic = orgs.find(
    (o: any) =>
      String(o?.slug || '')
        .toLowerCase()
        .trim() === 'cosmic' ||
      String(o?.name || '')
        .toLowerCase()
        .trim() === 'cosmic',
  )
  return cosmic || orgs[0] || null
}

function formatCosmicMailBatchFromSummary(
  items: { fromName: string; fromAddress: string }[],
): string {
  const labels = items
    .slice(0, 4)
    .map((f) => String(f.fromName || f.fromAddress || '').trim())
    .filter(Boolean)
  const unique = [...new Set(labels)]
  if (unique.length === 0) return 'Multiple senders'
  if (unique.length === 1) return unique[0]
  if (items.length <= 3) return unique.join(' · ')
  return `${unique[0]} · +${items.length - 1} more`
}

const COSMIC_MAIL_APPROVAL_NOTIFY_STORE_KEY = 'cosmicMailApprovalNotifyV1'

type CosmicMailStoredApprovalNotify = {
  orgId: string
  knownIds: string[]
}

async function recordCosmicMailGatewayNotification(payload: any) {
  const kind = String(payload?.kind || '').trim()
  if (kind === 'inbound') {
    const mailboxId = String(payload?.mailbox_id || payload?.mailboxId || '').trim()
    const messageId = String(payload?.message_id || payload?.messageId || '').trim()
    if (mailboxId && messageId) {
      try {
        await cosmicMailDbRequest({ op: 'try_mark_seen', mailboxId, messageId })
      } catch {
        // Best-effort de-dupe for the legacy direct poller.
      }
    }
  }

  if (kind === 'approval') {
    const approvalId = String(payload?.approval_id || payload?.approvalId || '').trim()
    const orgId = String(payload?.organization_id || payload?.organizationId || '').trim()
    if (approvalId && orgId) {
      const prev = store.get(COSMIC_MAIL_APPROVAL_NOTIFY_STORE_KEY) as CosmicMailStoredApprovalNotify | undefined
      const knownIds =
        prev && prev.orgId === orgId && Array.isArray(prev.knownIds)
          ? prev.knownIds
          : []
      store.set(COSMIC_MAIL_APPROVAL_NOTIFY_STORE_KEY, {
        orgId,
        knownIds: [...new Set([...knownIds, approvalId])].slice(-400),
      })
    }
  }

  return { ok: true }
}

function clipCosmicMailIslandText(value: string, max = 168) {
  const normalized = String(value || '')
    .replace(/\s+/g, ' ')
    .trim()
  if (!normalized) return ''
  return normalized.length > max ? `${normalized.slice(0, max - 1).trimEnd()}\u2026` : normalized
}

function formatCosmicMailApprovalBatchAgentSummary(
  items: { agentName: string }[],
): string {
  const labels = items
    .map((item) => String(item.agentName || '').trim())
    .filter(Boolean)
  const unique = [...new Set(labels)]
  if (unique.length === 0) return 'Agents'
  if (unique.length === 1) return unique[0]
  if (items.length <= 3) return unique.slice(0, 3).join(' · ')
  return `${unique[0]} · +${items.length - 1} more`
}

async function notifyCosmicMailPendingApprovals(
  cfg: GatewayConnectionConfig,
  orgId: string,
  targetWin: BrowserWindow,
) {
  if (!orgId || !targetWin || targetWin.isDestroyed()) return

  let approvalsRaw: any[] = []
  try {
    approvalsRaw = normalizeCosmicMailListResponse(
      await callCosmicMailJson(cfg, '/v1/approvals', { timeoutMs: 20_000 }),
    )
  } catch {
    return
  }

  type PendingSnap = {
    id: string
    subject: string
    agentName: string
    mailboxAddress: string
    recipients: string
    snippet: string
    createdAt: number
  }

  const pending: PendingSnap[] = []
  for (const a of approvalsRaw) {
    if (String(a?.organization_id || '') !== orgId) continue
    if (String(a?.status || '').toLowerCase() !== 'pending') continue
    const id = String(a?.id || '').trim()
    if (!id) continue
    const draft = a?.draft && typeof a.draft === 'object' ? a.draft : null
    const subject = String(draft?.subject || '').trim() || '(No subject)'
    const agentName = String(a?.agent_name || '').trim() || 'Agent'
    const mailboxAddress = String(a?.mailbox_address || '').trim() || 'Inbox'
    const createdMs = new Date(a?.created_at || 0).getTime()
    const createdAt = Number.isFinite(createdMs) ? createdMs : Date.now()
    let recipients = '—'
    if (Array.isArray(draft?.to_recipients) && draft.to_recipients.length) {
      const emails = draft.to_recipients
        .map((r: any) => String(r?.email || '').trim())
        .filter(Boolean)
      if (emails.length) recipients = emails.join(', ')
    }
    const plain = String(draft?.text_body || '').trim()
    const fromHtml = String(draft?.html_body || '')
      .replace(/<[^>]+>/g, ' ')
      .replace(/\s+/g, ' ')
      .trim()
    const snippet =
      clipCosmicMailIslandText(plain || fromHtml, 200) || 'Awaiting your review'
    pending.push({
      id,
      subject,
      agentName,
      mailboxAddress,
      recipients,
      snippet,
      createdAt,
    })
  }

  pending.sort((a, b) => b.createdAt - a.createdAt)

  const prev = store.get(COSMIC_MAIL_APPROVAL_NOTIFY_STORE_KEY) as CosmicMailStoredApprovalNotify | undefined
  let knownIds =
    prev && prev.orgId === orgId && Array.isArray(prev.knownIds) ? [...prev.knownIds] : null

  if (!knownIds) {
    store.set(COSMIC_MAIL_APPROVAL_NOTIFY_STORE_KEY, {
      orgId,
      knownIds: pending.map((p) => p.id).slice(-400),
    })
    return
  }

  const newOnes = pending.filter((p) => !knownIds!.includes(p.id))
  if (!newOnes.length) return

  knownIds = [...new Set([...knownIds, ...newOnes.map((p) => p.id)])].slice(-400)
  store.set(COSMIC_MAIL_APPROVAL_NOTIFY_STORE_KEY, { orgId, knownIds })

  const payload =
    newOnes.length === 1
      ? {
          kind: 'single' as const,
          approvalId: newOnes[0].id,
          subject: newOnes[0].subject,
          agentName: newOnes[0].agentName,
          mailboxAddress: newOnes[0].mailboxAddress,
          recipients: newOnes[0].recipients,
          snippet: newOnes[0].snippet,
          createdAt: newOnes[0].createdAt,
        }
      : {
          kind: 'batch' as const,
          count: newOnes.length,
          subject: newOnes[0].subject,
          agentSummary: formatCosmicMailApprovalBatchAgentSummary(newOnes),
          snippet: newOnes[0].snippet,
          latestCreatedAt: newOnes[0].createdAt,
          mailboxAddress: newOnes[0].mailboxAddress,
        }

  targetWin.webContents.send('cosmic-mail:new-approval', payload)
}

async function runCosmicMailPollTick() {
  if (cosmicMailPollBusy) return
  const w = win
  if (!w || w.isDestroyed()) return

  const baseUrl = String(store.get('cosmicMailBaseUrl') || '').trim()
  const apiToken = String(store.get('cosmicMailApiToken') || '').trim()
  if (!baseUrl || !apiToken) return

  cosmicMailPollBusy = true
  const cfg: GatewayConnectionConfig = { baseUrl, apiToken }

  type FreshInbound = {
    mailboxId: string
    mailboxAddress: string
    threadId: string
    messageId: string
    receivedAt: number
    subject: string
    fromName: string
    fromAddress: string
    snippet: string
  }

  const fresh: FreshInbound[] = []

  try {
    const authContext = await callCosmicMailJson(cfg, '/v1/system/auth-context', { timeoutMs: 15_000 })
    const organizations = normalizeCosmicMailListResponse(
      await callCosmicMailJson(cfg, '/v1/organizations', { timeoutMs: 15_000 }),
    )
    const preferred = pickPreferredCosmicMailOrganization(authContext, organizations)
    const preferredOrgId = preferred?.id ? String(preferred.id) : ''

    if (preferredOrgId) {
    const mailboxesRaw = normalizeCosmicMailListResponse(
      await callCosmicMailJson(cfg, '/v1/mailboxes', { timeoutMs: 20_000 }),
    )
    const mailboxes = mailboxesRaw.filter((m: any) => m?.organization_id === preferredOrgId)

    for (const mailbox of mailboxes) {
      const mailboxId = String(mailbox?.id || '').trim()
      const mailboxAddress = String(mailbox?.address || mailboxId).trim() || mailboxId
      if (!mailboxId) continue

      let baselineRes: { ok: boolean; result?: unknown; error?: string | null }
      try {
        baselineRes = await cosmicMailDbRequest({ op: 'is_baseline_done', mailboxId })
      } catch {
        continue
      }
      if (baselineRes.error) continue
      const baselineDone = !!baselineRes.result

      let threadsRaw: any[] = []
      try {
        const qs = new URLSearchParams({ mailbox_id: mailboxId })
        threadsRaw = normalizeCosmicMailListResponse(
          await callCosmicMailJson(cfg, `/v1/threads?${qs.toString()}`, { timeoutMs: 20_000 }),
        )
      } catch {
        continue
      }

      const threads = [...threadsRaw]
        .sort(
          (a, b) =>
            new Date(b?.last_message_at || 0).getTime() - new Date(a?.last_message_at || 0).getTime(),
        )
        .slice(0, 18)

      type InboundSnap = {
        id: string
        receivedAt: number
        threadId: string
        subject: string
        fromName: string
        fromAddress: string
        snippet: string
      }
      const inboundSnapshots: InboundSnap[] = []

      for (const thread of threads) {
        const threadId = String(thread?.id || '').trim()
        if (!threadId) continue
        let messages: any[] = []
        try {
          messages = normalizeCosmicMailListResponse(
            await callCosmicMailJson(cfg, `/v1/threads/${threadId}/messages`, { timeoutMs: 20_000 }),
          )
        } catch {
          continue
        }
        const sorted = [...messages].sort(
          (a, b) =>
            new Date(a?.received_at || a?.sent_at || a?.created_at || 0).getTime() -
            new Date(b?.received_at || b?.sent_at || b?.created_at || 0).getTime(),
        )
        const recent = sorted.slice(-30)
        const subject = String(thread?.subject || '(No subject)')
        for (const msg of recent) {
          if (String(msg?.direction || '') !== 'inbound') continue
          const messageId = String(msg?.id || '').trim()
          if (!messageId) continue
          const receivedAt = new Date(
            msg?.received_at || msg?.sent_at || msg?.created_at || 0,
          ).getTime()
          const snippet = String(
            msg?.preview_text || msg?.body_plain || thread?.snippet || '',
          )
            .trim()
            .slice(0, 180)
          inboundSnapshots.push({
            id: messageId,
            receivedAt,
            threadId,
            subject,
            fromName: String(msg?.from_name || '').trim(),
            fromAddress: String(msg?.from_address || '').trim(),
            snippet: snippet || 'New message',
          })
        }
      }

      if (!baselineDone) {
        const ids = inboundSnapshots.map((s) => s.id)
        try {
          await cosmicMailDbRequest({ op: 'seed_seen', mailboxId, messageIds: ids })
          await cosmicMailDbRequest({ op: 'set_baseline_done', mailboxId })
        } catch {
          // ignore; next poll retries
        }
        continue
      }

      const ordered = [...inboundSnapshots].sort((a, b) => b.receivedAt - a.receivedAt)
      for (const snap of ordered) {
        let mark: { ok: boolean; result?: unknown; error?: string | null }
        try {
          mark = await cosmicMailDbRequest({ op: 'try_mark_seen', mailboxId, messageId: snap.id })
        } catch {
          break
        }
        if (mark.error) break
        if (mark.result === true) {
          fresh.push({
            mailboxId,
            mailboxAddress,
            threadId: snap.threadId,
            messageId: snap.id,
            receivedAt: snap.receivedAt,
            subject: snap.subject,
            fromName: snap.fromName,
            fromAddress: snap.fromAddress,
            snippet: snap.snippet,
          })
        }
      }
    }

    if (fresh.length && win && !win.isDestroyed()) {
    fresh.sort((a, b) => b.receivedAt - a.receivedAt)

    const payload =
      fresh.length === 1
        ? {
            kind: 'single' as const,
            mailboxId: fresh[0].mailboxId,
            mailboxAddress: fresh[0].mailboxAddress,
            threadId: fresh[0].threadId,
            messageId: fresh[0].messageId,
            subject: fresh[0].subject,
            fromName: fresh[0].fromName,
            fromAddress: fresh[0].fromAddress,
            snippet: fresh[0].snippet,
            receivedAt: fresh[0].receivedAt,
          }
        : {
            kind: 'batch' as const,
            count: fresh.length,
            mailboxId: fresh[0].mailboxId,
            mailboxAddress: fresh[0].mailboxAddress,
            subject: fresh[0].subject,
            fromSummary: formatCosmicMailBatchFromSummary(fresh),
            snippet: fresh[0].snippet,
            latestReceivedAt: fresh[0].receivedAt,
          }

    win.webContents.send('cosmic-mail:new-inbound', payload)
    }

    await notifyCosmicMailPendingApprovals(cfg, preferredOrgId, w)
    }
  } catch (err) {
    console.error('[cosmic-mail poll]', err)
  } finally {
    cosmicMailPollBusy = false
  }
}

function startCosmicMailPollScheduler() {
  if (cosmicMailPollInterval) return
  cosmicMailPollInterval = setInterval(runCosmicMailPollTick, 30_000)
  setTimeout(runCosmicMailPollTick, 6000)
}

function stopCosmicMailPollScheduler() {
  if (cosmicMailPollInterval) {
    clearInterval(cosmicMailPollInterval)
    cosmicMailPollInterval = null
  }
}

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
        if (tag === 'SETTINGS') {
          syncGatewaySettingsFromPayload(json)
          window.webContents.send('settings:all', json)
        }
        else if (tag === 'INTEGRATIONS') window.webContents.send('integrations:all', json)
        else if (tag === 'CALENDAR_AGENDA') window.webContents.send('calendar:agenda', json)
        else if (tag === 'INTEGRATION_EVENT') window.webContents.send('integration:event', json)
        else if (tag === 'KEY_STATUS') window.webContents.send('key-status', json)
        else if (tag === 'COSMIC_MAIL_DB_REPLY') {
          const rid = typeof json?.requestId === 'string' ? json.requestId : ''
          const pending = rid ? cosmicMailDbPending.get(rid) : undefined
          if (pending) {
            clearTimeout(pending.timer)
            cosmicMailDbPending.delete(rid)
            if (json?.error) {
              pending.reject(new Error(String(json.error)))
            } else {
              pending.resolve({
                ok: !!json?.ok,
                result: json?.result,
                error: json?.error ?? null,
              })
            }
          }
        }
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
    try {
      voiceProcess.stdin.write(command + '\n')
      return true
    } catch (error) {
      console.error('[VOICE] Failed to send command:', error)
    }
  }
  return false
}

function toggleVoiceTyping() {
  const nextActive = !voiceActive
  const sent = sendToVoiceBridge(nextActive ? 'START' : 'STOP')
  if (sent) {
    voiceActive = nextActive
  }
  return { ok: sent, active: voiceActive }
}

function getPreferredDisplayId() {
  const preferredId = store.get('preferredDisplayId')
  return typeof preferredId === 'number' && Number.isFinite(preferredId) ? preferredId : null
}

// Get the preferred display or fallback to cursor display
function getTargetDisplay() {
  const preferredId = getPreferredDisplayId()
  const displays = screen.getAllDisplays()

  // Try to use preferred display
  if (preferredId !== null) {
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

type SizeToDisplayReason =
  | 'startup'
  | 'show'
  | 'meeting-show'
  | 'preferred-display-change'
  | 'display-added'
  | 'display-removed'
  | 'display-metrics-changed'

function applyDisplayBounds(
  target: Electron.Display,
  reason: SizeToDisplayReason,
  onSettled?: () => void,
) {
  if (!win || win.isDestroyed()) {
    onSettled?.()
    return
  }
  const { x, y, width, height } = target.workArea
  const prevId = lastAppliedDisplayId
  const prevScale = lastAppliedScaleFactor
  const crossingDisplay = prevId !== null && prevId !== target.id
  const scaleChanged = prevScale !== null && prevScale !== target.scaleFactor

  lastAppliedDisplayId = target.id
  lastAppliedScaleFactor = target.scaleFactor
  console.log(`📺 Applying display target (${reason}): ${target.id} @${target.scaleFactor}x`)

  const settle = () => {
    if (!win || win.isDestroyed()) {
      onSettled?.()
      return
    }
    win.webContents.send('cosmic:display-changed', {
      displayId: target.id,
      scaleFactor: target.scaleFactor,
      workArea: target.workArea,
      reason,
    })
    onSettled?.()
  }

  // On Windows, a single atomic setBounds across displays with different DPI scale factors
  // gets sized in the *source* display's DPI context — the new bounds arrive mis-scaled.
  // Reposition first so Windows fires WM_DPICHANGED, then size at the new DPI.
  const needsTwoStep =
    process.platform === 'win32' && (crossingDisplay || scaleChanged)

  if (needsTwoStep) {
    // Drive window opacity to 0 at the OS level so the user never sees the window
    // at an intermediate size during the multi-tick bounds transition. Without this,
    // the transparent shell picks up DWM shadow/repaint artifacts mid-move — that's
    // the "shaky" effect. We restore opacity only after bounds are fully settled.
    const priorOpacity = win.getOpacity()
    win.setOpacity(0)
    win.setPosition(x, y, false)
    setImmediate(() => {
      if (!win || win.isDestroyed()) { onSettled?.(); return }
      win.setBounds({ x, y, width, height }, false)
      // Second pass on the next tick guarantees the final bounds are computed
      // in the destination DPI context even if the first setBounds raced WM_DPICHANGED.
      setImmediate(() => {
        if (!win || win.isDestroyed()) { onSettled?.(); return }
        win.setBounds({ x, y, width, height }, false)
        // One more tick to let the compositor commit the final bounds before we
        // restore opacity — eliminates the last trace of visible motion.
        setImmediate(() => {
          if (!win || win.isDestroyed()) { onSettled?.(); return }
          win.setOpacity(priorOpacity)
          settle()
        })
      })
    })
  } else {
    win.setBounds({ x, y, width, height }, false)
    settle()
  }
}

function sizeToDisplay(reason: SizeToDisplayReason = 'show', onSettled?: () => void) {
  if (!win) { onSettled?.(); return }
  applyDisplayBounds(getTargetDisplay(), reason, onSettled)
}

function loadMainWindow() {
  if (!win) return
  if (VITE_DEV_SERVER_URL) win.loadURL(VITE_DEV_SERVER_URL)
  else win.loadFile(path.join(RENDERER_DIST, 'index.html'))
}

function toggleSearch() {
  if (!win) return
  if (searchVisible) {
    searchVisible = false
    win.webContents.send('cosmic:hiding')
    win.setIgnoreMouseEvents(true, { forward: true })
  } else {
    searchVisible = true
    // Defer the reveal until bounds have fully settled on the target display.
    // On cross-display/cross-DPI toggles the bounds apply across multiple ticks;
    // firing `cosmic:shown` too early makes the fade-in animate at intermediate sizes.
    sizeToDisplay('show', () => {
      if (!win || win.isDestroyed() || !searchVisible) return
      win.setIgnoreMouseEvents(false)
      win.webContents.send('cosmic:shown')
      win.focus()
    })
  }
}

function invokeMeetingMode() {
  if (!win) return

  if (!searchVisible) {
    searchVisible = true
    sizeToDisplay('meeting-show', () => {
      if (!win || win.isDestroyed() || !searchVisible) return
      win.setIgnoreMouseEvents(false)
      win.webContents.send('cosmic:shown')
      win.webContents.send('meeting:invoke')
      win.focus()
    })
  } else {
    win.setIgnoreMouseEvents(false)
    win.webContents.send('meeting:invoke')
    win.focus()
  }
}

function createWindow() {
  const iconPath = resolveAppIconPath()
  const browserIcon = iconPath ? loadBrowserWindowIcon(iconPath) : undefined
  const initialDisplay = getTargetDisplay()
  const initialBounds = initialDisplay.workArea
  win = new BrowserWindow({
    x: initialBounds.x,
    y: initialBounds.y,
    width: initialBounds.width,
    height: initialBounds.height,
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    alwaysOnTop: true,
    skipTaskbar: false,
    hasShadow: false,
    // Windows tool windows (`toolbar`) are excluded from the taskbar; keep that type only on macOS.
    ...(process.platform === 'darwin' ? { type: 'toolbar' as const } : {}),
    ...(browserIcon ? { icon: browserIcon } : {}),
    webPreferences: {
      preload: path.join(__dirname, 'preload.mjs'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  })

  win.webContents.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown' || input.key !== 'Escape') {
      return
    }
    if (!searchVisible) {
      return
    }
    event.preventDefault()
    toggleSearch()
  })

  win.setIgnoreMouseEvents(true, { forward: true })
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
  kill(weatherProcess); weatherProcess = null

  stopCosmicMailPollScheduler()
  for (const [, pending] of cosmicMailDbPending) {
    clearTimeout(pending.timer)
    pending.reject(new Error('Settings bridge stopped.'))
  }
  cosmicMailDbPending.clear()

  kill(settingsProcess); settingsProcess = null
  kill(voiceProcess); voiceProcess = null
  kill(meetingProcess); meetingProcess = null
  gatewayConnectionManager?.stop()
  gatewayConnectionManager = null
}

// Monitor change detection
function handleDisplayAdded(_event: any, display: Electron.Display) {
  console.log('📺 Display added:', display.id)
  if (!store.get('autoRepositionOnChange') || searchVisible) {
    return
  }

  if (getPreferredDisplayId() === display.id) {
    sizeToDisplay('display-added')
  }
}

function handleDisplayRemoved(_event: any, display: Electron.Display) {
  console.log('📺 Display removed:', display.id)
  if (!store.get('autoRepositionOnChange')) {
    return
  }

  const preferredId = getPreferredDisplayId()
  if (preferredId === display.id || lastAppliedDisplayId === display.id) {
    // Active display vanished — clear so the next apply treats it as a fresh placement.
    if (lastAppliedDisplayId === display.id) {
      lastAppliedDisplayId = null
      lastAppliedScaleFactor = null
    }
    sizeToDisplay('display-removed')
  }
}

function handleDisplayMetricsChanged(
  _event: any,
  display: Electron.Display,
  changedMetrics: string[],
) {
  // Re-apply bounds if the display we're currently on had its scale factor or work area change
  // (e.g., user changed Windows display scaling, taskbar resized, rotation).
  if (display.id !== lastAppliedDisplayId) return
  const relevant = changedMetrics.some(m =>
    m === 'scaleFactor' || m === 'bounds' || m === 'workArea' || m === 'rotation',
  )
  if (!relevant) return
  console.log(`📺 Display metrics changed on active display ${display.id}: ${changedMetrics.join(', ')}`)
  applyDisplayBounds(display, 'display-metrics-changed')
}

app.on('before-quit', () => {
  cleanupProcesses()
})

// Compositor tuning for a full-workArea transparent window with backdrop-filter surfaces.
// Default tile memory budget (~128MB) overflows at high DPI + blurred full-screen layers,
// producing `tile_manager.cc` warnings and per-keystroke input latency. These switches
// must be applied before app.whenReady().
app.commandLine.appendSwitch('force-gpu-mem-available-mb', '1024')
app.commandLine.appendSwitch('enable-gpu-rasterization')
app.commandLine.appendSwitch('enable-zero-copy')

app.whenReady().then(() => {
  // Register Google login before createWindow() so the channel exists as soon as the
  // renderer can call it (avoids races with fast loads / stale partial main bundles).
  ipcMain.handle('auth:loginWithGoogle', async () => {
    return loginWithGoogleOAuth()
  })

  if (process.platform === 'win32') {
    app.setAppUserModelId('com.cosmic.spotlight')
  }
  createWindow()
  if (win) {
    let initialWindowShown = false
    const revealStartupWindow = () => {
      if (!win || win.isDestroyed() || initialWindowShown) return
      initialWindowShown = true
      sizeToDisplay('startup')
      win.show()
    }

    win.once('ready-to-show', revealStartupWindow)
    win.webContents.once('did-finish-load', () => {
      sizeToDisplay('startup')
      revealStartupWindow()
    })
    loadMainWindow()

    startMediaBridge(win)
    startWindowBridge(win)
    startWeatherBridge(win)
    startMeetingBridge(win)

    startSettingsBridge(win)
    startCosmicMailPollScheduler()
    settingsProcess?.stdin.write('GET_ALL_SETTINGS\n')
    settingsProcess?.stdin.write('GET_KEY_STATUS\n')
    startVoiceBridge(win)
    gatewayConnectionManager = new GatewayConnectionManager(win)
    getDesktopDeviceId()
    configureGatewayConnection()
    sizeToDisplay('startup')
  }

  // Listen for display changes
  screen.on('display-added', handleDisplayAdded)
  screen.on('display-removed', handleDisplayRemoved)
  screen.on('display-metrics-changed', handleDisplayMetricsChanged)

  ipcMain.on('cosmic:hide', () => { if (searchVisible) toggleSearch() })
  ipcMain.on('cosmic:toggle', toggleSearch)

  ipcMain.on('set-ignore-mouse-events', (event, ignore, options) => {
    const w = BrowserWindow.fromWebContents(event.sender)
    if (w) w.setIgnoreMouseEvents(ignore, options)
  })

  ipcMain.on('app:quit', () => { app.quit() })
  ipcMain.on('app:minimize', (event) => {
    const w = BrowserWindow.fromWebContents(event.sender)
    if (w && !w.isDestroyed()) w.minimize()
  })
  ipcMain.on('app:restore', (event) => {
    const w = BrowserWindow.fromWebContents(event.sender)
    if (!w || w.isDestroyed()) return
    if (w.isMinimized()) w.restore()
    w.show()
    w.focus()
  })
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

  ipcMain.on('settings:get-key-status', () => {
    settingsProcess?.stdin.write('GET_KEY_STATUS\n')
  })

  ipcMain.on('settings:save-api-keys', (_, payload) => {
    settingsProcess?.stdin.write(`SAVE_API_KEYS:${JSON.stringify(payload || {})}\n`)
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
  ipcMain.handle('voice:toggle-typing', () => toggleVoiceTyping())

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

  ipcMain.handle('gateway:get-state', () => {
    return gatewayConnectionManager?.getState() || null
  })

  ipcMain.handle('gateway:pick-documents', async () => {
    return { documents: await pickGatewayDocuments() }
  })

  ipcMain.handle('gateway:read-clipboard-image', async () => {
    const document = await readClipboardImageAsGatewayDocument()
    return { documents: document ? [document] : [] }
  })

  ipcMain.handle('gateway:send-query', async (_, payload: {
    content: string
    conversationContext?: any[]
    requestId?: string
    routeOverride?: string
    attachments?: PendingGatewayDocumentUpload[]
  }) => {
    if (!gatewayConnectionManager) {
      throw new Error('Gateway connection manager is unavailable.')
    }
    const effectiveRequestId = String(payload?.requestId || '').trim() || `req_${crypto.randomUUID()}`
    let stagedAttachments: any[] = []
    const requestedAttachments = Array.isArray(payload?.attachments) ? payload.attachments : []
    if (requestedAttachments.length > 0) {
      const transportConfig = getStoredGatewayTransportConfig()
      if (!transportConfig) {
        throw new Error('Gateway transport is not configured.')
      }
      const gatewayState = gatewayConnectionManager.getState()
      const sessionId = String(gatewayState?.sessionId || '').trim()
      stagedAttachments = await uploadDesktopDocumentsToGateway(
        transportConfig,
        effectiveRequestId,
        sessionId,
        requestedAttachments,
      )
    }
    return {
      requestId: gatewayConnectionManager.sendQuery(
        String(payload?.content || ''),
        Array.isArray(payload?.conversationContext) ? payload.conversationContext : [],
        effectiveRequestId,
        String(payload?.routeOverride || ''),
        stagedAttachments,
      ),
    }
  })

  ipcMain.handle('gateway:cancel-response', async (_, payload: { requestId?: string; taskId?: string }) => {
    if (!gatewayConnectionManager) {
      throw new Error('Gateway connection manager is unavailable.')
    }
    return gatewayConnectionManager.cancelResponse(
      String(payload?.requestId || ''),
      String(payload?.taskId || ''),
    )
  })

  ipcMain.handle('gateway:submit-task-input-reply', async (_, payload: { inputRequestId?: string; taskId?: string; content?: string }) => {
    if (!gatewayConnectionManager) {
      throw new Error('Gateway connection manager is unavailable.')
    }
    return gatewayConnectionManager.submitTaskInputReply(
      String(payload?.inputRequestId || ''),
      String(payload?.taskId || ''),
      String(payload?.content || ''),
    )
  })

  ipcMain.handle('gateway:background-request', async (_, payload: { requestId?: string }) => {
    if (!gatewayConnectionManager) {
      throw new Error('Gateway connection manager is unavailable.')
    }
    return gatewayConnectionManager.backgroundRequest(
      String(payload?.requestId || ''),
    )
  })

  ipcMain.handle('gateway:foreground-request', async (_, payload: { requestId?: string }) => {
    if (!gatewayConnectionManager) {
      throw new Error('Gateway connection manager is unavailable.')
    }
    return gatewayConnectionManager.foregroundRequest(
      String(payload?.requestId || ''),
    )
  })

  ipcMain.handle('gateway:request-resume', () => {
    gatewayConnectionManager?.requestResume()
    return { ok: true }
  })

  ipcMain.handle('gateway:list-sessions', async () => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      return { sessions: [] }
    }
    return callGatewayJson(config, '/sessions')
  })

  ipcMain.handle('gateway:get-session-history', async (_, sessionId: string) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, `/sessions/${encodeURIComponent(sessionId)}`)
  })

  ipcMain.handle('gateway:get-request-traces', async (_, sessionId: string) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, `/sessions/${encodeURIComponent(sessionId)}/request-traces`)
  })

  ipcMain.handle('gateway:list-mobile-devices', async () => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      return { devices: [] }
    }
    return callGatewayJson(config, '/channels/mobile/devices')
  })

  ipcMain.handle('gateway:authorize-mobile-device', async (_, deviceId: string) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/channels/mobile/devices/authorize', {
      method: 'POST',
      body: { device_id: String(deviceId || '').trim() },
    })
  })

  ipcMain.handle('gateway:revoke-mobile-device', async (_, deviceId: string) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, `/channels/mobile/devices/${encodeURIComponent(String(deviceId || '').trim())}`, {
      method: 'DELETE',
    })
  })

  ipcMain.handle('gateway:revoke-all-mobile-devices', async () => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/channels/mobile/devices/revoke-all', {
      method: 'POST',
    })
  })

  ipcMain.handle(
    'gateway:get-system-metrics',
    async (
      _,
      payload?: boolean | { forceRefresh?: boolean; usage?: GatewayUsagePeriodOptions },
    ) => {
      const config = getStoredGatewayTransportConfig()
      if (!config) {
        throw new Error('Gateway connection is not configured.')
      }
      const gatewayState = gatewayConnectionManager?.getState()?.status
      if (gatewayState && !gatewayState.connected) {
        throw new Error(String(gatewayState.detail || 'The desktop app is not connected to your VM yet.'))
      }
      let forceRefresh = false
      let usage: GatewayUsagePeriodOptions | undefined
      if (typeof payload === 'boolean') {
        forceRefresh = payload
      } else if (payload && typeof payload === 'object') {
        forceRefresh = Boolean(payload.forceRefresh)
        usage = payload.usage
      }
      try {
        return await getGatewaySystemMetrics(config, forceRefresh, usage)
      } catch (error: unknown) {
        if (error instanceof Error && error.message === 'Gateway request timed out.' && gatewayState?.detail) {
          throw new Error(gatewayState.detail)
        }
        throw error
      }
    },
  )

  ipcMain.handle('gateway:get-registry-agents', async () => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    const gatewayState = gatewayConnectionManager?.getState()?.status
    if (gatewayState && !gatewayState.connected) {
      throw new Error(String(gatewayState.detail || 'The desktop app is not connected to your VM yet.'))
    }
    return callGatewayJson(config, '/desktop/registry-agents', { timeoutMs: 25000 })
  })

  ipcMain.handle('gateway:list-tool-opportunities', async () => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/desktop/tool-opportunities', { timeoutMs: 20000 })
  })

  ipcMain.handle('gateway:update-tool-opportunity', async (_, payload: { opportunityId?: string; changes?: Record<string, unknown> }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    const opportunityId = String(payload?.opportunityId || '').trim()
    if (!opportunityId) {
      throw new Error('Tool opportunity id is required.')
    }
    return callGatewayJson(config, `/desktop/tool-opportunities/${encodeURIComponent(opportunityId)}`, {
      method: 'PATCH',
      body: payload?.changes && typeof payload.changes === 'object' ? payload.changes : {},
      timeoutMs: 20000,
    })
  })

  ipcMain.handle('gateway:build-tool-opportunity', async (_, opportunityId: string) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    const normalized = String(opportunityId || '').trim()
    if (!normalized) {
      throw new Error('Tool opportunity id is required.')
    }
    return callGatewayJson(config, `/desktop/tool-opportunities/${encodeURIComponent(normalized)}/build`, {
      method: 'POST',
      timeoutMs: 20000,
    })
  })

  ipcMain.handle('gateway:get-preferences', async () => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/desktop/preferences', { timeoutMs: 15000 })
  })

  ipcMain.handle('gateway:save-preferences', async (_, payload: {
    visualResponseEnhancementEnabled?: boolean
    cosmicOrchestratorProvider?: string
    cosmicOrchestratorModel?: string
    cosmicHeartbeatEnabled?: boolean
  }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    const body: Record<string, any> = {}
    if (typeof payload?.visualResponseEnhancementEnabled === 'boolean') {
      body.visual_response_enhancement_enabled = payload.visualResponseEnhancementEnabled
    }
    if (typeof payload?.cosmicOrchestratorProvider === 'string') {
      body.cosmic_orchestrator_provider = payload.cosmicOrchestratorProvider
    }
    if (typeof payload?.cosmicOrchestratorModel === 'string') {
      body.cosmic_orchestrator_model = payload.cosmicOrchestratorModel
    }
    if (typeof payload?.cosmicHeartbeatEnabled === 'boolean') {
      body.cosmic_heartbeat_enabled = payload.cosmicHeartbeatEnabled
    }
    return callGatewayJson(config, '/desktop/preferences', {
      method: 'PATCH',
      body,
      timeoutMs: 15000,
    })
  })

  ipcMain.handle('gateway:get-scheduler-overview', async () => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    const gatewayState = gatewayConnectionManager?.getState()?.status
    if (gatewayState && !gatewayState.connected) {
      throw new Error(String(gatewayState.detail || 'The desktop app is not connected to your VM yet.'))
    }
    return callGatewayJson(config, '/scheduler/overview', { timeoutMs: 15000 })
  })

  ipcMain.handle('gateway:pause-scheduler-cron', async (_, payload: { cronId?: string; reason?: string }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    const cronId = String(payload?.cronId || '').trim()
    if (!cronId) {
      throw new Error('Cron id is required.')
    }
    return callGatewayJson(config, `/scheduler/crons/${encodeURIComponent(cronId)}/pause`, {
      method: 'POST',
      body: {
        reason: String(payload?.reason || '').trim() || 'Paused from Spaces Autopilot.',
      },
      timeoutMs: 15000,
    })
  })

  ipcMain.handle('gateway:resume-scheduler-cron', async (_, payload: { cronId?: string }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    const cronId = String(payload?.cronId || '').trim()
    if (!cronId) {
      throw new Error('Cron id is required.')
    }
    return callGatewayJson(config, `/scheduler/crons/${encodeURIComponent(cronId)}/resume`, {
      method: 'POST',
      timeoutMs: 15000,
    })
  })

  ipcMain.handle('gateway:delete-scheduler-cron', async (_, payload: { cronId?: string }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    const cronId = String(payload?.cronId || '').trim()
    if (!cronId) {
      throw new Error('Cron id is required.')
    }
    return callGatewayJson(config, `/scheduler/crons/${encodeURIComponent(cronId)}`, {
      method: 'DELETE',
      timeoutMs: 15000,
    })
  })

  ipcMain.handle('gateway:pause-scheduler-heartbeat', async (_, payload: { reason?: string }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/scheduler/heartbeat/pause', {
      method: 'POST',
      body: {
        reason: String(payload?.reason || '').trim() || 'Paused from Spaces Autopilot.',
      },
      timeoutMs: 15000,
    })
  })

  ipcMain.handle('gateway:resume-scheduler-heartbeat', async () => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/scheduler/heartbeat/resume', {
      method: 'POST',
      timeoutMs: 15000,
    })
  })

  ipcMain.handle('gateway:record-heartbeat-consumption', async (_, payload: {
    sessionId?: string
    messageId?: string
    requestId?: string
    sourceId?: string
    channel?: string
    platform?: string
    deviceId?: string
    consumedVia?: string
    metadata?: Record<string, unknown>
  }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/channels/heartbeat/consume', {
      method: 'POST',
      body: {
        session_id: String(payload?.sessionId || '').trim() || null,
        message_id: String(payload?.messageId || '').trim() || null,
        request_id: String(payload?.requestId || '').trim() || null,
        source_id: String(payload?.sourceId || '').trim() || null,
        channel: String(payload?.channel || '').trim() || null,
        platform: String(payload?.platform || '').trim() || 'desktop',
        device_id: String(payload?.deviceId || config.deviceId || '').trim() || null,
        consumed_via: String(payload?.consumedVia || '').trim() || 'desktop',
        metadata: payload?.metadata && typeof payload.metadata === 'object' ? payload.metadata : {},
      },
      timeoutMs: 10000,
    })
  })

  ipcMain.handle('gateway:list-heartbeat-consumptions', async (_, sessionId?: string) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    const normalizedSessionId = String(sessionId || '').trim()
    const suffix = normalizedSessionId
      ? `?session_id=${encodeURIComponent(normalizedSessionId)}&limit=500`
      : '?limit=500'
    return callGatewayJson(config, `/channels/heartbeat/consumptions${suffix}`, {
      timeoutMs: 10000,
    })
  })

  ipcMain.handle('gateway:get-codex-status', async () => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/desktop/agents/codex/status', { timeoutMs: 20000 })
  })

  ipcMain.handle('gateway:save-codex-config', async (_, payload: {
    authMode?: string
    apiKey?: string
    preferredModel?: string
    reasoningEffort?: string
    approvalMode?: string
    vmSyncEnabled?: boolean
  }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/desktop/agents/codex/config', {
      method: 'POST',
      body: {
        auth_mode: payload?.authMode,
        api_key: payload?.apiKey,
        preferred_model: payload?.preferredModel,
        reasoning_effort: payload?.reasoningEffort,
        approval_mode: payload?.approvalMode,
        vm_sync_enabled: payload?.vmSyncEnabled,
      },
      timeoutMs: 45000,
    })
  })

  ipcMain.handle('gateway:start-codex-login', async () => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/desktop/agents/codex/login/start', {
      method: 'POST',
      timeoutMs: 30000,
    })
  })

  ipcMain.handle('gateway:logout-codex', async () => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/desktop/agents/codex/auth', {
      method: 'DELETE',
      timeoutMs: 25000,
    })
  })

  ipcMain.handle('gateway:get-cursor-status', async () => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/desktop/agents/cursor/status', { timeoutMs: 20000 })
  })

  ipcMain.handle('gateway:save-cursor-config', async (_, payload: {
    preferredModel?: string
    approvalMode?: string
    vmSyncEnabled?: boolean
  }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/desktop/agents/cursor/config', {
      method: 'POST',
      body: {
        preferred_model: payload?.preferredModel,
        approval_mode: payload?.approvalMode,
        vm_sync_enabled: payload?.vmSyncEnabled,
      },
      timeoutMs: 45000,
    })
  })

  ipcMain.handle('gateway:start-cursor-login', async () => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/desktop/agents/cursor/login/start', {
      method: 'POST',
      timeoutMs: 30000,
    })
  })

  ipcMain.handle('gateway:logout-cursor', async () => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/desktop/agents/cursor/auth', {
      method: 'DELETE',
      timeoutMs: 25000,
    })
  })

  ipcMain.handle('gateway:get-alpha-agent-config', async () => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/desktop/agents/alpha/config', { timeoutMs: 15000 })
  })

  ipcMain.handle('gateway:save-alpha-agent-config', async (_, payload: {
    preferredHarness?: string
  }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/desktop/agents/alpha/config', {
      method: 'POST',
      body: {
        preferred_harness: payload?.preferredHarness,
      },
      timeoutMs: 15000,
    })
  })

  ipcMain.handle('gateway:download-output-artifact', async (_, payload: {
    messageId?: string
    artifactId?: string
    suggestedFilename?: string
    mimeType?: string
    timeoutMs?: number
  }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    const messageId = String(payload?.messageId || '').trim()
    const artifactId = String(payload?.artifactId || '').trim()
    if (!messageId || !artifactId) {
      throw new Error('messageId and artifactId are required.')
    }

    const requestUrl = new URL(
      `/desktop/messages/${encodeURIComponent(messageId)}/artifacts/${encodeURIComponent(artifactId)}/download`,
      `${normalizeGatewayBaseUrl(config.baseUrl)}/`,
    ).toString()

    const controller = new AbortController()
    const timeoutMs = Math.max(5000, Number(payload?.timeoutMs ?? 120000))
    const timeout = setTimeout(() => controller.abort(), timeoutMs)

    try {
      const response = await fetch(requestUrl, {
        method: 'GET',
        headers: {
          Authorization: `Bearer ${config.apiToken}`,
        },
        signal: controller.signal,
      })
      if (!response.ok) {
        const responseText = await response.text()
        let parsed: any = null
        if (responseText) {
          try {
            parsed = JSON.parse(responseText)
          } catch {
            parsed = { raw: responseText }
          }
        }
        const detail =
          (typeof parsed?.detail === 'string' && parsed.detail) ||
          (typeof parsed?.error === 'string' && parsed.error) ||
          response.statusText ||
          `Download failed (${response.status})`
        throw new Error(detail)
      }

      const cd = response.headers.get('content-disposition')
      const responseMimeType = String(response.headers.get('content-type') || '').trim().split(';', 1)[0]
      let filename = (payload?.suggestedFilename && String(payload.suggestedFilename).trim()) || 'artifact'
      if (cd) {
        const m = /filename\*=UTF-8''([^;]+)|filename=\"([^\"]+)\"/i.exec(cd)
        const raw = m ? (m[1] || m[2]) : null
        if (raw) {
          try {
            filename = decodeURIComponent(raw)
          } catch {
            filename = raw
          }
        }
      }
      const effectiveMimeType =
        (payload?.mimeType && String(payload.mimeType).trim()) ||
        responseMimeType ||
        inferDesktopAttachmentMimeType(filename)
      const saveDialogOpts = buildSaveDialogOptions('Save file', filename, effectiveMimeType)

      const bytes = Buffer.from(await response.arrayBuffer())

      const saveTarget = win
        ? await dialog.showSaveDialog(win, saveDialogOpts)
        : await dialog.showSaveDialog(saveDialogOpts)
      if (saveTarget.canceled || !saveTarget.filePath) {
        return { cancelled: true }
      }

      const outputFilePath = ensureFilePathExtension(saveTarget.filePath, effectiveMimeType)
      await fs.mkdir(path.dirname(outputFilePath), { recursive: true })
      await fs.writeFile(outputFilePath, bytes)
      return {
        cancelled: false,
        filePath: outputFilePath,
        filename: path.basename(outputFilePath),
      }
    } catch (error: any) {
      throw new Error(formatTransportError('Gateway artifact download', normalizeGatewayBaseUrl(config.baseUrl), error))
    } finally {
      clearTimeout(timeout)
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

  ipcMain.handle('telegram:get-status', async (_, payload: GatewayConnectionConfig) => {
    return callGatewayJson(payload, '/channels/telegram/status')
  })

  ipcMain.handle('telegram:sync-webhook', async (_, payload: GatewayConnectionConfig) => {
    return callGatewayJson(payload, '/channels/telegram/webhook/sync', {
      method: 'POST',
    })
  })

  ipcMain.handle('telegram:clear-webhook', async (_, payload: GatewayConnectionConfig & { dropPendingUpdates?: boolean }) => {
    const dropPendingUpdates = payload?.dropPendingUpdates ? 'true' : 'false'
    return callGatewayJson(payload, `/channels/telegram/webhook?drop_pending_updates=${dropPendingUpdates}`, {
      method: 'DELETE',
    })
  })

  ipcMain.handle('telegram:send-test', async (_, payload: GatewayConnectionConfig & { chatId: number; message: string }) => {
    return callGatewayJson(payload, '/channels/telegram/send', {
      method: 'POST',
      body: {
        chat_id: payload.chatId,
        message: payload.message,
      },
    })
  })

  ipcMain.handle('gateway:get-agent-email-status', async () => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/channels/agent-email/status', { timeoutMs: 20000 })
  })

  ipcMain.handle('gateway:save-agent-email-config', async (_, payload: {
    baseUrl?: string
    apiToken?: string
    primaryMailboxAddress?: string | null
  }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/channels/agent-email/config', {
      method: 'POST',
      body: {
        base_url: String(payload?.baseUrl || '').trim(),
        api_token: String(payload?.apiToken || '').trim(),
        primary_mailbox_address: payload?.primaryMailboxAddress ?? null,
      },
      timeoutMs: 30000,
    })
  })

  ipcMain.handle('gateway:clear-agent-email-config', async () => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/channels/agent-email/config', {
      method: 'DELETE',
      timeoutMs: 20000,
    })
  })

  ipcMain.handle('gateway:get-agent-email-desktop-config', async () => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/channels/agent-email/desktop-config', {
      method: 'GET',
      timeoutMs: 20000,
    })
  })

  ipcMain.handle('gateway:save-agent-email-trusted-senders', async (_, payload: {
    trustedSenders?: string[]
  }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    const trustedSenders = Array.isArray(payload?.trustedSenders)
      ? payload.trustedSenders.map((item) => String(item || ''))
      : []
    return callGatewayJson(config, '/channels/agent-email/trusted-senders', {
      method: 'POST',
      body: {
        trusted_senders: trustedSenders,
      },
      timeoutMs: 20000,
    })
  })

  ipcMain.handle('gateway:get-gmail-approvals', async () => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    return callGatewayJson(config, '/channels/gmail/approvals', {
      method: 'GET',
      timeoutMs: 20000,
    })
  })

  ipcMain.handle('gateway:approve-gmail-approval', async (_, payload: { approvalId?: string }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    const approvalId = String(payload?.approvalId || '').trim()
    if (!approvalId) {
      throw new Error('Gmail approval id is required.')
    }
    return callGatewayJson(config, `/channels/gmail/approvals/${encodeURIComponent(approvalId)}/approve`, {
      method: 'POST',
      timeoutMs: 45000,
    })
  })

  ipcMain.handle('gateway:reject-gmail-approval', async (_, payload: { approvalId?: string; note?: string }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    const approvalId = String(payload?.approvalId || '').trim()
    if (!approvalId) {
      throw new Error('Gmail approval id is required.')
    }
    return callGatewayJson(config, `/channels/gmail/approvals/${encodeURIComponent(approvalId)}/reject`, {
      method: 'POST',
      body: { note: String(payload?.note || '').trim() || null },
      timeoutMs: 30000,
    })
  })

  ipcMain.handle('gateway:update-gmail-approval-draft', async (_, payload: { approvalId?: string; subject?: string; bodyText?: string }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    const approvalId = String(payload?.approvalId || '').trim()
    if (!approvalId) {
      throw new Error('Gmail approval id is required.')
    }
    return callGatewayJson(config, `/channels/gmail/approvals/${encodeURIComponent(approvalId)}/draft`, {
      method: 'PATCH',
      body: {
        subject: String(payload?.subject || '').trim(),
        body_text: String(payload?.bodyText || ''),
      },
      timeoutMs: 90000,
    })
  })

  ipcMain.handle('gateway:approve-agent-email-approval', async (_, payload: { approvalId?: string }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    const approvalId = String(payload?.approvalId || '').trim()
    if (!approvalId) {
      throw new Error('Agent Email approval id is required.')
    }
    return callGatewayJson(config, `/channels/agent-email/approvals/${encodeURIComponent(approvalId)}/approve`, {
      method: 'POST',
      timeoutMs: 45000,
    })
  })

  ipcMain.handle('gateway:update-agent-email-approval-draft', async (_, payload: { approvalId?: string; subject?: string; bodyText?: string }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    const approvalId = String(payload?.approvalId || '').trim()
    if (!approvalId) {
      throw new Error('Agent Email approval id is required.')
    }
    return callGatewayJson(config, `/channels/agent-email/approvals/${encodeURIComponent(approvalId)}/draft`, {
      method: 'PATCH',
      body: {
        subject: String(payload?.subject || '').trim(),
        body_text: String(payload?.bodyText || ''),
      },
      timeoutMs: 45000,
    })
  })

  ipcMain.handle('gateway:update-calendar-event', async (_, payload: {
    eventId?: string
    accountId?: string | null
    calendarId?: string | null
    summary?: string
    start?: string
    end?: string
    location?: string | null
    description?: string | null
    isAllDay?: boolean
    timezone?: string | null
  }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    const eventId = String(payload?.eventId || '').trim()
    if (!eventId) {
      throw new Error('Calendar event id is required.')
    }
    return callGatewayJson(config, `/channels/calendar/events/${encodeURIComponent(eventId)}`, {
      method: 'PATCH',
      body: {
        account_id: String(payload?.accountId || '').trim() || null,
        calendar_id: String(payload?.calendarId || '').trim() || 'primary',
        summary: String(payload?.summary || '').trim(),
        start: String(payload?.start || '').trim(),
        end: String(payload?.end || '').trim(),
        location: String(payload?.location || '').trim() || null,
        description: String(payload?.description || ''),
        is_all_day: Boolean(payload?.isAllDay),
        timezone: String(payload?.timezone || '').trim() || null,
      },
      timeoutMs: 90000,
    })
  })

  ipcMain.handle('gateway:respond-calendar-invite', async (_, payload: {
    eventId?: string
    accountId?: string | null
    calendarId?: string | null
    responseStatus?: string
  }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    const eventId = String(payload?.eventId || '').trim()
    const responseStatus = String(payload?.responseStatus || '').trim()
    if (!eventId) {
      throw new Error('Calendar event id is required.')
    }
    if (!['accepted', 'declined', 'tentative', 'needsAction'].includes(responseStatus)) {
      throw new Error('A valid Calendar invitation response is required.')
    }
    return callGatewayJson(config, `/channels/calendar/events/${encodeURIComponent(eventId)}/respond`, {
      method: 'POST',
      body: {
        account_id: String(payload?.accountId || '').trim() || null,
        calendar_id: String(payload?.calendarId || '').trim() || 'primary',
        response_status: responseStatus,
      },
      timeoutMs: 90000,
    })
  })

  ipcMain.handle('gateway:reject-agent-email-approval', async (_, payload: { approvalId?: string; note?: string }) => {
    const config = getStoredGatewayTransportConfig()
    if (!config) {
      throw new Error('Gateway connection is not configured.')
    }
    const approvalId = String(payload?.approvalId || '').trim()
    if (!approvalId) {
      throw new Error('Agent Email approval id is required.')
    }
    return callGatewayJson(config, `/channels/agent-email/approvals/${encodeURIComponent(approvalId)}/reject`, {
      method: 'POST',
      body: { note: String(payload?.note || '').trim() || null },
      timeoutMs: 30000,
    })
  })

  ipcMain.handle('cosmic-mail:request', async (_, payload: GatewayConnectionConfig & {
    path: string
    method?: string
    body?: unknown
    timeoutMs?: number
  }) => {
    return callCosmicMailJson(payload, payload.path, {
      method: payload.method,
      body: payload.body,
      timeoutMs: payload.timeoutMs,
    })
  })

  ipcMain.handle('cosmic-mail:record-gateway-notification', async (_, payload: any) => {
    return recordCosmicMailGatewayNotification(payload)
  })

  ipcMain.handle('cosmic-mail:upload-draft-attachment', async (_, payload: GatewayConnectionConfig & {
    draftId: string
    filePath: string
    filename?: string
    timeoutMs?: number
  }) => {
    const apiToken = String(payload?.apiToken || '').trim()
    if (!apiToken) {
      throw new Error('Cosmic Mail API token is required.')
    }
    const baseUrl = normalizeGatewayBaseUrl(payload?.baseUrl || '')
    const draftId = String(payload.draftId || '').trim()
    const filePath = String(payload.filePath || '').trim()
    if (!draftId || !filePath) {
      throw new Error('Draft id and file path are required.')
    }

    const requestUrl = new URL(
      `/v1/attachments/drafts/${encodeURIComponent(draftId)}`,
      `${baseUrl}/`,
    ).toString()

    const controller = new AbortController()
    const timeoutMs = Math.max(5000, payload.timeoutMs ?? 120_000)
    const timeout = setTimeout(() => controller.abort(), timeoutMs)

    try {
      const buf = await fs.readFile(filePath)
      const blob = new Blob([buf])
      const form = new FormData()
      const name = (payload.filename && String(payload.filename).trim()) || path.basename(filePath)
      form.append('file', blob, name)

      const response = await fetch(requestUrl, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${apiToken}`,
          Accept: 'application/json',
        },
        body: form,
        signal: controller.signal,
      })

      const responseText = await response.text()
      let parsed: any = null
      if (responseText) {
        try {
          parsed = JSON.parse(responseText)
        } catch {
          parsed = { raw: responseText }
        }
      }

      if (!response.ok) {
        const detail =
          (typeof parsed?.detail === 'string' && parsed.detail) ||
          (typeof parsed?.error === 'string' && parsed.error) ||
          response.statusText ||
          `Upload failed (${response.status})`
        throw new Error(detail)
      }

      return parsed
    } catch (error: any) {
      throw new Error(formatTransportError('Cosmic Mail', baseUrl, error))
    } finally {
      clearTimeout(timeout)
    }
  })

  ipcMain.handle('cosmic-mail:download-attachment', async (event, payload: GatewayConnectionConfig & {
    attachmentId: string
    suggestedFilename?: string
    timeoutMs?: number
  }) => {
    const apiToken = String(payload?.apiToken || '').trim()
    if (!apiToken) {
      throw new Error('Cosmic Mail API token is required.')
    }
    const baseUrl = normalizeGatewayBaseUrl(payload?.baseUrl || '')
    const attachmentId = String(payload.attachmentId || '').trim()
    if (!attachmentId) {
      throw new Error('Attachment id is required.')
    }

    const requestUrl = new URL(
      `/v1/attachments/${encodeURIComponent(attachmentId)}/download`,
      `${baseUrl}/`,
    ).toString()

    const controller = new AbortController()
    const timeoutMs = Math.max(5000, payload.timeoutMs ?? 120_000)
    const timeout = setTimeout(() => controller.abort(), timeoutMs)

    try {
      const response = await fetch(requestUrl, {
        method: 'GET',
        headers: {
          Authorization: `Bearer ${apiToken}`,
        },
        signal: controller.signal,
      })

      if (!response.ok) {
        const responseText = await response.text()
        let parsed: any = null
        if (responseText) {
          try {
            parsed = JSON.parse(responseText)
          } catch {
            parsed = { raw: responseText }
          }
        }
        const detail =
          (typeof parsed?.detail === 'string' && parsed.detail) ||
          (typeof parsed?.error === 'string' && parsed.error) ||
          response.statusText ||
          `Download failed (${response.status})`
        throw new Error(detail)
      }

      const cd = response.headers.get('content-disposition')
      const responseMimeType = String(response.headers.get('content-type') || '').trim().split(';', 1)[0]
      let filename = (payload.suggestedFilename && String(payload.suggestedFilename).trim()) || 'attachment'
      if (cd) {
        const m = /filename\*=UTF-8''([^;]+)|filename="([^"]+)"/i.exec(cd)
        const raw = m ? (m[1] || m[2]) : null
        if (raw) {
          try {
            filename = decodeURIComponent(raw.trim())
          } catch {
            filename = raw.trim()
          }
        }
      }
      const effectiveMimeType = responseMimeType || inferDesktopAttachmentMimeType(filename)

      const arrayBuffer = await response.arrayBuffer()
      const buffer = Buffer.from(arrayBuffer)

      const parentWindow = (win && !win.isDestroyed() ? win : null) ?? BrowserWindow.fromWebContents(event.sender)
      const saveDialogOpts = buildSaveDialogOptions('Save attachment', filename, effectiveMimeType)
      const { canceled, filePath } = parentWindow
        ? await dialog.showSaveDialog(parentWindow, saveDialogOpts)
        : await dialog.showSaveDialog(saveDialogOpts)
      if (canceled || !filePath) {
        return { cancelled: true as const }
      }

      const outputFilePath = ensureFilePathExtension(filePath, effectiveMimeType)
      await fs.writeFile(outputFilePath, buffer)
      return { cancelled: false as const, path: outputFilePath }
    } catch (error: any) {
      throw new Error(formatTransportError('Cosmic Mail', baseUrl, error))
    } finally {
      clearTimeout(timeout)
    }
  })


  /**
   * Google sign-in for desktop: opens the **system default browser** for Supabase Google OAuth,
   * then `authenticate_with_google_identity` RPC (same VM payload as API-key login).
   * A short-lived localhost server receives the redirect: the browser POSTs `location.href`
   * (including hash or query) back so tokens work with an external browser.
   * Add loopback redirect patterns in Supabase (same project as mobile). API-key login is unchanged.
   */
  async function loginWithGoogleOAuth(): Promise<
    | {
        success: true
        apiKey: string
        userId: string
        fullName: string
        isPrivileged: boolean
        gatewayUrl: string
        gatewayApiToken: string
        vmIp: string
        vmDns: string
        authenticatedAt: number
      }
    | { success: false; error: string; message?: string }
  > {
    type OAuthOutcome = { type: 'href'; href: string } | { type: 'err'; error: Error }

    let settled = false
    let timeoutId: ReturnType<typeof setTimeout> | undefined
    let resolveOutcome!: (v: OAuthOutcome) => void
    const outcomePromise = new Promise<OAuthOutcome>((resolve) => {
      resolveOutcome = resolve
    })

    const finish = (v: OAuthOutcome) => {
      if (settled) return
      settled = true
      if (timeoutId !== undefined) clearTimeout(timeoutId)
      resolveOutcome(v)
    }

    const server = createServer((req, res) => {
      const addr = server.address() as AddressInfo | null
      const listenPort = addr?.port ?? 0
      const raw = req.url ?? '/'
      const q = raw.indexOf('?')
      const pathOnly = q >= 0 ? raw.slice(0, q) : raw

      if (req.method === 'GET' && pathOnly === '/cosmic-oauth') {
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
        res.end(`<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cosmic</title>
<style>
  html, body { height: 100%; margin: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: #fff;
    color: #0f172a;
    -webkit-font-smoothing: antialiased;
  }
  #oauth-root {
    min-height: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 32px 24px;
    box-sizing: border-box;
    text-align: center;
  }
  /* Matches Spaces header wordmark: .spaces-header-brand (spaces-control.css) */
  .cosmic-brand {
    font-family: "Bahnschrift", "Avenir Next Condensed", "Segoe UI Variable Display", "Helvetica Neue", Arial, sans-serif;
    font-size: clamp(20px, 5vw, 28px);
    font-weight: 600;
    letter-spacing: 0.56em;
    text-transform: uppercase;
    margin: 0 0 22px 0;
    padding-left: 0.56em;
    color: #0b1220;
    line-height: 1.2;
    text-shadow: 0 1px 12px rgba(15, 23, 42, 0.06);
  }
  .oauth-lede {
    font-size: 15px;
    font-weight: 500;
    color: #334155;
    margin: 0 0 8px 0;
    letter-spacing: -0.01em;
  }
  .oauth-sub {
    font-size: 13px;
    line-height: 1.55;
    color: #64748b;
    max-width: 22rem;
    margin: 0;
  }
  .oauth-dots {
    display: inline-flex;
    gap: 6px;
    margin-top: 22px;
    align-items: center;
    justify-content: center;
  }
  .oauth-dots span {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: #cbd5e1;
    animation: oauthPulse 1.2s ease-in-out infinite;
  }
  .oauth-dots span:nth-child(2) { animation-delay: 0.15s; }
  .oauth-dots span:nth-child(3) { animation-delay: 0.3s; }
  @keyframes oauthPulse {
    0%, 80%, 100% { opacity: 0.35; transform: scale(0.92); }
    40% { opacity: 1; transform: scale(1); }
  }
</style>
</head>
<body>
<main id="oauth-root">
  <p class="cosmic-brand" aria-label="COSMIC">COSMIC</p>
  <p class="oauth-lede">Finishing sign-in</p>
  <p class="oauth-sub">Confirming your session with Cosmic. This only takes a moment.</p>
  <div class="oauth-dots" aria-hidden="true"><span></span><span></span><span></span></div>
</main>
<script>
(function(){
  function esc(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  fetch("/cosmic-oauth/done", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ href: String(location.href || "") }),
  })
    .then(function () {
      var el = document.getElementById("oauth-root");
      if (!el) return;
      el.innerHTML =
        '<p class="cosmic-brand" aria-label="COSMIC">COSMIC</p>' +
        '<p class="oauth-lede">You are signed in</p>' +
        '<p class="oauth-sub">Close this tab and return to the Cosmic desktop app.</p>';
    })
    .catch(function (e) {
      var el = document.getElementById("oauth-root");
      var msg = (e && e.message) || e || "Unknown error";
      if (el) {
        el.innerHTML =
          '<p class="cosmic-brand" aria-label="COSMIC">COSMIC</p>' +
          '<p class="oauth-lede">Sign-in could not finish</p>' +
          '<p class="oauth-sub">' +
          esc(msg) +
          "</p>";
      }
    });
})();
</script>
</body>
</html>`)
        return
      }

      if (req.method === 'POST' && pathOnly === '/cosmic-oauth/done') {
        const chunks: Buffer[] = []
        let total = 0
        let tooLarge = false
        req.on('data', (chunk: Buffer) => {
          total += chunk.length
          if (total > 65536) {
            tooLarge = true
            req.destroy()
            return
          }
          chunks.push(chunk)
        })
        req.on('end', () => {
          if (tooLarge) {
            res.writeHead(413).end()
            return
          }
          let parsed: { href?: string } = {}
          try {
            parsed = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}') as { href?: string }
          } catch {
            res.writeHead(400).end()
            return
          }
          const href = typeof parsed.href === 'string' ? parsed.href : ''
          if (!listenPort || !href.startsWith(`http://127.0.0.1:${listenPort}/cosmic-oauth`)) {
            res.writeHead(400).end('invalid href')
            return
          }
          res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' })
          res.end('OK')
          finish({ type: 'href', href })
        })
        req.on('error', () => {
          try {
            res.destroy()
          } catch {
            /* ignore */
          }
        })
        return
      }

      res.writeHead(404).end()
    })

    try {
      await new Promise<void>((resolveListen, rejectListen) => {
        server.once('error', rejectListen)
        server.listen(0, '127.0.0.1', () => resolveListen())
      })
    } catch (e: any) {
      try {
        server.close()
      } catch {
        /* ignore */
      }
      return {
        success: false,
        error: 'oauth_server_failed',
        message: e?.message || 'Could not start local OAuth receiver.',
      }
    }

    const addr = server.address() as AddressInfo | string | null
    const port = typeof addr === 'object' && addr?.port ? addr.port : 0
    if (!port) {
      try {
        server.close()
      } catch {
        /* ignore */
      }
      return { success: false, error: 'oauth_server_failed', message: 'No free localhost port for OAuth.' }
    }

    const redirectTo = `http://127.0.0.1:${port}/cosmic-oauth`
    const oauthSupabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
      auth: {
        persistSession: false,
        autoRefreshToken: false,
        detectSessionInUrl: false,
      },
    })

    const { data: oauthPayload, error: oauthInitError } = await oauthSupabase.auth.signInWithOAuth({
      provider: 'google',
      options: {
        redirectTo,
        skipBrowserRedirect: true,
        queryParams: { prompt: 'select_account' },
      },
    })

    if (oauthInitError || !oauthPayload?.url) {
      await new Promise<void>((r) => server.close(() => r()))
      return {
        success: false,
        error: 'oauth_init_failed',
        message: oauthInitError?.message || 'Could not start Google sign-in.',
      }
    }

    timeoutId = setTimeout(() => {
      finish({ type: 'err', error: new Error('Google sign-in timed out.') })
    }, 240_000)

    try {
      await shell.openExternal(oauthPayload.url)
    } catch (e) {
      finish({
        type: 'err',
        error: e instanceof Error ? e : new Error('Could not open the system browser.'),
      })
    }

    const outcome = await outcomePromise

    await new Promise<void>((r) => {
      server.close(() => r())
    })

    if (outcome.type === 'err') {
      const msg = outcome.error.message || 'Google sign-in failed.'
      if (msg.includes('timed out')) {
        return { success: false, error: 'oauth_timeout', message: msg }
      }
      if (msg.includes('system browser')) {
        return { success: false, error: 'oauth_open_failed', message: msg }
      }
      return { success: false, error: 'network_error', message: msg }
    }

    const finalUrl = outcome.href

    try {
      const callbackUrl = new URL(finalUrl)
      if (callbackUrl.searchParams.has('code')) {
        const { error: exchangeError } = await oauthSupabase.auth.exchangeCodeForSession(finalUrl)
        if (exchangeError) {
          return {
            success: false,
            error: 'oauth_session_failed',
            message: exchangeError.message,
          }
        }
      } else {
        const hash = new URL(finalUrl).hash.replace(/^#/, '')
        const params = new URLSearchParams(hash)
        const oauthErr = params.get('error')
        if (oauthErr) {
          return {
            success: false,
            error: oauthErr,
            message: params.get('error_description') || oauthErr,
          }
        }
        const accessToken = params.get('access_token')
        const refreshToken = params.get('refresh_token')
        if (!accessToken || !refreshToken) {
          return {
            success: false,
            error: 'oauth_no_tokens',
            message: 'Google sign-in did not return a session.',
          }
        }
        const { error: setErr } = await oauthSupabase.auth.setSession({
          access_token: accessToken,
          refresh_token: refreshToken,
        })
        if (setErr) {
          return {
            success: false,
            error: 'oauth_session_failed',
            message: setErr.message,
          }
        }
      }

      const { data: rpcData, error: rpcError } = await oauthSupabase.rpc('authenticate_with_google_identity')

      try {
        await oauthSupabase.auth.signOut({ scope: 'local' })
      } catch {
        /* best-effort */
      }

      if (rpcError) {
        return {
          success: false,
          error: 'rpc_error',
          message: rpcError.message || 'Could not look up your Cosmic account.',
        }
      }

      const result = rpcData as {
        success?: boolean
        error?: string
        message?: string
        api_key?: string
        user?: { id: string; full_name: string; is_privileged: boolean }
        vm?: { gateway_url: string; api_token: string; vm_ip: string; vm_dns: string }
      }

      if (!result || typeof result !== 'object' || result.success !== true || !result.user || !result.vm) {
        return {
          success: false,
          error: result?.error || 'rpc_invalid',
          message: result?.message || 'Unexpected response from authentication server.',
        }
      }

      const apiKey = String(result.api_key ?? '').trim()
      if (!apiKey) {
        return {
          success: false,
          error: 'no_api_key',
          message: 'No API key on file for this account.',
        }
      }

      const authData = {
        apiKey,
        userId: result.user.id,
        fullName: result.user.full_name,
        isPrivileged: result.user.is_privileged,
        gatewayUrl: result.vm.gateway_url,
        gatewayApiToken: result.vm.api_token,
        vmIp: result.vm.vm_ip,
        vmDns: result.vm.vm_dns,
        authenticatedAt: Date.now(),
      }
      const deviceId = getDesktopDeviceId()

      const authJson = JSON.stringify(authData)
      settingsProcess?.stdin.write(`SAVE_SETTING:cosmicAuth:${authJson}\n`)
      settingsProcess?.stdin.write(`SAVE_SETTING:gatewayBaseUrl:${result.vm.gateway_url}\n`)
      settingsProcess?.stdin.write(`SAVE_SETTING:gatewayApiToken:${result.vm.api_token}\n`)
      settingsProcess?.stdin.write(`SAVE_SETTING:desktopDeviceId:${deviceId}\n`)
      store.set('cosmicAuth', authData)
      store.set('gatewayBaseUrl', result.vm.gateway_url)
      store.set('gatewayApiToken', result.vm.api_token)
      store.set('desktopDeviceId', deviceId)
      configureGatewayConnection()
      gatewayConnectionManager?.requestResume()

      return { success: true, ...authData }
    } catch (e: any) {
      return {
        success: false,
        error: 'network_error',
        message: e?.message || 'Google sign-in failed.',
      }
    }
  }

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
      const deviceId = getDesktopDeviceId()

      // Persist auth to SQLite via settings bridge
      const authJson = JSON.stringify(authData)
      settingsProcess?.stdin.write(`SAVE_SETTING:cosmicAuth:${authJson}\n`)
      settingsProcess?.stdin.write(`SAVE_SETTING:gatewayBaseUrl:${result.vm.gateway_url}\n`)
      settingsProcess?.stdin.write(`SAVE_SETTING:gatewayApiToken:${result.vm.api_token}\n`)
      settingsProcess?.stdin.write(`SAVE_SETTING:desktopDeviceId:${deviceId}\n`)
      store.set('cosmicAuth', authData)
      store.set('gatewayBaseUrl', result.vm.gateway_url)
      store.set('gatewayApiToken', result.vm.api_token)
      store.set('desktopDeviceId', deviceId)
      configureGatewayConnection()
      gatewayConnectionManager?.requestResume()

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
    gatewayConnectionManager?.stop()
    settingsProcess?.stdin.write('SAVE_SETTING:cosmicAuth:\n')
    settingsProcess?.stdin.write('SAVE_SETTING:gatewayBaseUrl:\n')
    settingsProcess?.stdin.write('SAVE_SETTING:gatewayApiToken:\n')
    store.set('cosmicAuth', null)
    store.set('gatewayBaseUrl', '')
    store.set('gatewayApiToken', '')
    configureGatewayConnection()
    return {
      success: true,
      scopes: {
        clearedAuth: true,
        clearedGatewayTransport: true,
        websocketClosed: true,
        sessionCacheCleared: true,
        reconnectStopped: true,
        deviceIdRetained: true,
      },
    }
  })

  // Multi-monitor IPC handlers
  ipcMain.handle('get-all-displays', () => {
    const displays = screen.getAllDisplays()
    const primary = screen.getPrimaryDisplay()
    const preferredId = getPreferredDisplayId()

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

  ipcMain.on('set-preferred-display', (event, displayId: number | null) => {
    const normalizedDisplayId =
      typeof displayId === 'number' && Number.isFinite(displayId) ? displayId : null
    console.log(`📺 Setting preferred display to: ${normalizedDisplayId ?? 'auto'}`)
    store.set('preferredDisplayId', normalizedDisplayId)
    sizeToDisplay('preferred-display-change')
    event.sender.send('display-preferences-updated', normalizedDisplayId)
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
    toggleVoiceTyping()
  })
})

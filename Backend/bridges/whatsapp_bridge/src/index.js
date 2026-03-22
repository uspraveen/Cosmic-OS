import fsSync from 'node:fs';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import axios from 'axios';
import express from 'express';
import makeWASocket, {
  Browsers,
  DisconnectReason,
  downloadContentFromMessage,
  fetchLatestBaileysVersion,
  getContentType,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys';

import {
  normalizePhoneFromJid,
  normalizePhoneValue,
  resolveInboundSenderIdentity,
} from './whatsapp_identity.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const BRIDGE_ROOT = path.resolve(__dirname, '..');

function loadEnvFile(filePath) {
  if (!fsSync.existsSync(filePath)) {
    return;
  }

  const raw = fsSync.readFileSync(filePath, 'utf8');
  for (const line of raw.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) {
      continue;
    }

    const eqIndex = trimmed.indexOf('=');
    if (eqIndex <= 0) {
      continue;
    }

    const key = trimmed.slice(0, eqIndex).trim();
    if (!key || Object.prototype.hasOwnProperty.call(process.env, key)) {
      continue;
    }

    let value = trimmed.slice(eqIndex + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    process.env[key] = value;
  }
}

loadEnvFile(path.resolve(BRIDGE_ROOT, '.env'));

const originalConsoleLog = console.log.bind(console);
const originalConsoleWarn = console.warn.bind(console);
const originalConsoleError = console.error.bind(console);

function envInt(name, fallback) {
  const raw = process.env[name];
  if (!raw) return fallback;
  const parsed = Number.parseInt(raw, 10);
  return Number.isNaN(parsed) ? fallback : parsed;
}

function envBool(name, fallback) {
  const raw = process.env[name];
  if (!raw) return fallback;
  return ['1', 'true', 'yes', 'on'].includes(raw.trim().toLowerCase());
}

function resolveBridgePath(rawPath, fallbackSegments) {
  if (rawPath) {
    return path.isAbsolute(rawPath)
      ? rawPath
      : path.resolve(BRIDGE_ROOT, rawPath);
  }
  return path.resolve(BRIDGE_ROOT, ...fallbackSegments);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const config = {
  gatewayUrl:
    process.env.GATEWAY_INTERNAL_URL ??
    process.env.COSMIC_URL ??
    'http://127.0.0.1:8080/internal/channels/whatsapp/incoming',
  host: process.env.WHATSAPP_BRIDGE_HOST ?? '127.0.0.1',
  port: envInt('WHATSAPP_BRIDGE_PORT', 3000),
  authDir: resolveBridgePath(process.env.WHATSAPP_AUTH_DIR, ['store', 'auth']),
  configStorePath: resolveBridgePath(
    process.env.WHATSAPP_CONFIG_PATH,
    ['store', 'bridge-config.json'],
  ),
  bridgeToken: process.env.WHATSAPP_BRIDGE_TOKEN ?? '',
  gatewayInternalToken: process.env.GATEWAY_INTERNAL_TOKEN ?? '',
};

const sessionHealthConfig = {
  faultWindowMs: Math.max(1000, envInt('WHATSAPP_SESSION_FAULT_WINDOW_MS', 45000)),
  selfHealThreshold: Math.max(1, envInt('WHATSAPP_SESSION_SELF_HEAL_THRESHOLD', 5)),
  minSelfHealIntervalMs: Math.max(
    1000,
    envInt('WHATSAPP_SESSION_SELF_HEAL_INTERVAL_MS', 120000),
  ),
  reconnectDelayMs: Math.max(
    0,
    envInt('WHATSAPP_SESSION_SELF_HEAL_RECONNECT_DELAY_MS', 1500),
  ),
};

const app = express();
app.use(express.json({ limit: '2mb' }));
const inboundMediaStore = new Map();
const INBOUND_MEDIA_TTL_MS = Math.max(
  60 * 1000,
  envInt('WHATSAPP_INBOUND_MEDIA_TTL_MS', 30 * 60 * 1000),
);

let sock = null;
let connectPromise = null;
let latestQr = null;
let qrUpdatedAt = null;
let lastError = null;
let connectionState = {
  connected: false,
  pairingState: 'idle',
  lastDisconnectCode: null,
  authDir: config.authDir,
  connectedJid: null,
};
let bridgeMetrics = {
  sessionFaultsTotal: 0,
  badMacErrorsTotal: 0,
  decryptFailuresTotal: 0,
  retryReceiptsTotal: 0,
  prekeyBundleResetsTotal: 0,
  sessionFaultWindowCount: 0,
  lastSessionFaultAt: null,
  lastSessionFaultKind: null,
  lastSessionFaultMessage: null,
  selfHealTriggeredTotal: 0,
  selfHealCompletedTotal: 0,
  selfHealFailedTotal: 0,
  selfHealInFlight: false,
  lastSelfHealAt: null,
  lastSelfHealReason: null,
  lastSelfHealRecoveredAt: null,
  lastSelfHealRecoveredBy: null,
  lastInboundForwardAt: null,
  lastInboundForwardMs: null,
  lastOutboundSendAt: null,
  lastOutboundSendMs: null,
};
let severeSessionFaultTimestamps = [];
let selfHealPromise = null;
let selfHealPendingOpen = false;
let bridgeConfig = {
  allowedPhone: normalizePhoneValue(process.env.WHATSAPP_ALLOWED_PHONE ?? ''),
  selfChatOnly: envBool('WHATSAPP_SELF_CHAT_ONLY', false),
};

function pruneInboundMediaStore(now = Date.now()) {
  for (const [mediaRef, entry] of inboundMediaStore.entries()) {
    if (!entry?.createdAt || now - entry.createdAt > INBOUND_MEDIA_TTL_MS) {
      inboundMediaStore.delete(mediaRef);
    }
  }
}

function rememberInboundMedia(mediaRef, payload) {
  if (!mediaRef || !payload?.source) {
    return;
  }
  pruneInboundMediaStore();
  inboundMediaStore.set(mediaRef, {
    ...payload,
    createdAt: Date.now(),
  });
}

function mediaDownloadTypeForKind(kind) {
  switch (String(kind || '').toLowerCase()) {
    case 'image':
      return 'image';
    case 'video':
      return 'video';
    case 'audio':
    case 'voice':
      return 'audio';
    case 'document':
      return 'document';
    case 'sticker':
      return 'sticker';
    default:
      return null;
  }
}

async function readInboundMediaBuffer(mediaRef) {
  pruneInboundMediaStore();
  const entry = inboundMediaStore.get(mediaRef);
  if (!entry?.source) {
    const error = new Error('Media reference not found');
    error.statusCode = 404;
    throw error;
  }
  const downloadType = mediaDownloadTypeForKind(entry.kind);
  if (!downloadType) {
    const error = new Error('Unsupported media kind');
    error.statusCode = 400;
    throw error;
  }
  const stream = await downloadContentFromMessage(entry.source, downloadType);
  const chunks = [];
  for await (const chunk of stream) {
    chunks.push(Buffer.from(chunk));
  }
  return {
    buffer: Buffer.concat(chunks),
    mimeType: entry.mime_type ?? 'application/octet-stream',
    filename: entry.filename ?? null,
  };
}

function isoNow() {
  return new Date().toISOString();
}

function pruneSessionFaultWindow(now = Date.now()) {
  severeSessionFaultTimestamps = severeSessionFaultTimestamps.filter(
    (timestamp) => now - timestamp <= sessionHealthConfig.faultWindowMs,
  );
  bridgeMetrics.sessionFaultWindowCount = severeSessionFaultTimestamps.length;
}

function resetSessionFaultWindow() {
  severeSessionFaultTimestamps = [];
  bridgeMetrics.sessionFaultWindowCount = 0;
}

function markSelfHealRecovered(source) {
  if (!selfHealPendingOpen && !bridgeMetrics.selfHealInFlight) {
    return;
  }
  bridgeMetrics.selfHealCompletedTotal += 1;
  bridgeMetrics.selfHealInFlight = false;
  bridgeMetrics.lastSelfHealRecoveredAt = isoNow();
  bridgeMetrics.lastSelfHealRecoveredBy = source;
  selfHealPendingOpen = false;
  resetSessionFaultWindow();
}

function noteInboundForwardLatency(elapsedMs) {
  bridgeMetrics.lastInboundForwardAt = isoNow();
  bridgeMetrics.lastInboundForwardMs = elapsedMs;
  markSelfHealRecovered('inbound_forward');
}

function noteOutboundSendLatency(elapsedMs) {
  bridgeMetrics.lastOutboundSendAt = isoNow();
  bridgeMetrics.lastOutboundSendMs = elapsedMs;
  markSelfHealRecovered('outbound_send');
}

async function maybeTriggerSessionSelfHeal(reason) {
  const now = Date.now();
  pruneSessionFaultWindow(now);
  if (severeSessionFaultTimestamps.length < sessionHealthConfig.selfHealThreshold) {
    return;
  }
  if (selfHealPromise || bridgeMetrics.selfHealInFlight) {
    return;
  }
  if (
    bridgeMetrics.lastSelfHealAt &&
    now - Date.parse(bridgeMetrics.lastSelfHealAt) < sessionHealthConfig.minSelfHealIntervalMs
  ) {
    return;
  }
  if (connectionState.pairingState === 'logged_out' || connectionState.pairingState === 'qr_ready') {
    return;
  }

  const authExists = await hasExistingAuthState().catch(() => false);
  if (!authExists) {
    return;
  }

  bridgeMetrics.selfHealTriggeredTotal += 1;
  bridgeMetrics.selfHealInFlight = true;
  bridgeMetrics.lastSelfHealAt = isoNow();
  bridgeMetrics.lastSelfHealReason = reason;
  selfHealPendingOpen = true;

  originalConsoleWarn(
    `[bridge] Triggering WhatsApp session self-heal after ${severeSessionFaultTimestamps.length} severe faults in ${sessionHealthConfig.faultWindowMs}ms (${reason})`,
  );

  selfHealPromise = (async () => {
    if (sessionHealthConfig.reconnectDelayMs > 0) {
      await sleep(sessionHealthConfig.reconnectDelayMs);
    }
    await ensureSocketConnected({ refresh: true });
  })()
    .catch((error) => {
      bridgeMetrics.selfHealFailedTotal += 1;
      bridgeMetrics.selfHealInFlight = false;
      selfHealPendingOpen = false;
      lastError = error?.message ?? String(error);
      originalConsoleError(
        `[bridge] WhatsApp session self-heal failed: ${error?.message ?? error}`,
      );
    })
    .finally(() => {
      selfHealPromise = null;
    });
}

function recordSessionFault(kind, message) {
  const now = Date.now();
  if (kind !== 'retry_receipt') {
    severeSessionFaultTimestamps.push(now);
  }
  pruneSessionFaultWindow(now);

  bridgeMetrics.sessionFaultsTotal += 1;
  bridgeMetrics.lastSessionFaultAt = isoNow();
  bridgeMetrics.lastSessionFaultKind = kind;
  bridgeMetrics.lastSessionFaultMessage = message ?? null;

  if (kind === 'bad_mac') {
    bridgeMetrics.badMacErrorsTotal += 1;
  } else if (kind === 'decrypt_failure') {
    bridgeMetrics.decryptFailuresTotal += 1;
  } else if (kind === 'retry_receipt') {
    bridgeMetrics.retryReceiptsTotal += 1;
  } else if (kind === 'prekey_bundle_reset') {
    bridgeMetrics.prekeyBundleResetsTotal += 1;
  }

  void maybeTriggerSessionSelfHeal(kind);
}

function sanitizeLogValue(value, seen = new WeakSet()) {
  if (value instanceof Error) {
    return {
      type: value.name,
      message: value.message,
      stack: value.stack,
    };
  }
  if (!value || typeof value !== 'object') {
    return value;
  }
  if (Buffer.isBuffer(value)) {
    return `<Buffer length=${value.length}>`;
  }
  if (seen.has(value)) {
    return '[Circular]';
  }
  seen.add(value);
  if (Array.isArray(value)) {
    return value.map((item) => sanitizeLogValue(item, seen));
  }
  const output = {};
  for (const [key, entry] of Object.entries(value)) {
    output[key] = sanitizeLogValue(entry, seen);
  }
  return output;
}

function normalizeLoggerArgs(args) {
  if (!args.length) {
    return { fields: {}, message: '' };
  }
  if (typeof args[0] === 'string') {
    return { fields: {}, message: args[0] };
  }
  const fields = args[0] && typeof args[0] === 'object' ? sanitizeLogValue(args[0]) : {};
  const message =
    typeof args[1] === 'string'
      ? args[1]
      : typeof args[0] === 'string'
        ? args[0]
        : '';
  return { fields, message };
}

function observeBridgeLog(message, fields) {
  if (message === 'failed to decrypt message') {
    const errMessage = fields?.err?.message ?? message;
    recordSessionFault(
      String(errMessage).includes('Bad MAC') ? 'bad_mac' : 'decrypt_failure',
      String(errMessage),
    );
    return;
  }

  if (message === 'sent retry receipt') {
    recordSessionFault('retry_receipt', message);
    return;
  }

  if (message === 'Closing open session in favor of incoming prekey bundle') {
    recordSessionFault('prekey_bundle_reset', message);
  }
}

function writeBridgeLog(level, bindings, args) {
  const { fields, message } = normalizeLoggerArgs(args);
  const payload = {
    level,
    time: isoNow(),
    ...bindings,
    ...fields,
    ...(message ? { msg: message } : {}),
  };
  observeBridgeLog(message, payload);
  const text = JSON.stringify(payload);
  if (level === 'error') {
    originalConsoleError(text);
  } else if (level === 'warn') {
    originalConsoleWarn(text);
  } else {
    originalConsoleLog(text);
  }
}

function createBridgeLogger(bindings = {}) {
  return {
    level: process.env.WHATSAPP_BRIDGE_LOG_LEVEL ?? 'info',
    child(childBindings = {}) {
      return createBridgeLogger({ ...bindings, ...childBindings });
    },
    trace(...args) {
      writeBridgeLog('trace', bindings, args);
    },
    debug(...args) {
      writeBridgeLog('debug', bindings, args);
    },
    info(...args) {
      writeBridgeLog('info', bindings, args);
    },
    warn(...args) {
      writeBridgeLog('warn', bindings, args);
    },
    error(...args) {
      writeBridgeLog('error', bindings, args);
    },
  };
}

console.error = (...args) => {
  try {
    const message = args
      .map((value) => {
        if (value instanceof Error) {
          return `${value.name}: ${value.message}`;
        }
        return String(value);
      })
      .join(' ');
    if (message.includes('Session error:')) {
      recordSessionFault(
        message.includes('Bad MAC') ? 'bad_mac' : 'decrypt_failure',
        message,
      );
    }
  } catch {}
  originalConsoleError(...args);
};

async function ensureAuthDir() {
  await fs.mkdir(config.authDir, { recursive: true });
}

async function ensureConfigStoreDir() {
  await fs.mkdir(path.dirname(config.configStorePath), { recursive: true });
}

async function hasExistingAuthState() {
  try {
    await fs.access(path.join(config.authDir, 'creds.json'));
    return true;
  } catch {
    return false;
  }
}

function setConnectionState(patch) {
  connectionState = { ...connectionState, ...patch };
}

function clearQrState() {
  latestQr = null;
  qrUpdatedAt = null;
}

function normalizeBridgeRecipient(rawValue) {
  const text = String(rawValue ?? '').trim();
  if (!text) {
    return '';
  }
  if (text.endsWith('@s.whatsapp.net')) {
    return normalizePhoneValue(text.split('@', 1)[0]);
  }
  return normalizePhoneValue(text);
}

async function loadBridgeConfig() {
  await ensureConfigStoreDir();

  try {
    const raw = await fs.readFile(config.configStorePath, 'utf8');
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') {
      return;
    }

    bridgeConfig = {
      allowedPhone: normalizePhoneValue(parsed.allowed_phone ?? bridgeConfig.allowedPhone),
      selfChatOnly: Boolean(parsed.self_chat_only ?? bridgeConfig.selfChatOnly),
    };
  } catch (error) {
    if (error?.code !== 'ENOENT') {
      console.warn('Failed to load WhatsApp bridge config:', error?.message ?? error);
    }
  }
}

async function saveBridgeConfig() {
  await ensureConfigStoreDir();
  await fs.writeFile(
    config.configStorePath,
    JSON.stringify(
      {
        allowed_phone: bridgeConfig.allowedPhone || null,
        self_chat_only: bridgeConfig.selfChatOnly,
      },
      null,
      2,
    ),
    'utf8',
  );
}

function buildBridgeConfigPayload() {
  return {
    allowed_phone: bridgeConfig.allowedPhone || null,
    self_chat_only: bridgeConfig.selfChatOnly,
  };
}

function getSelfChatPhone() {
  return normalizePhoneFromJid(connectionState.connectedJid);
}

function getEffectiveAllowedPhone() {
  if (bridgeConfig.allowedPhone) {
    return bridgeConfig.allowedPhone;
  }
  if (bridgeConfig.selfChatOnly) {
    return getSelfChatPhone();
  }
  return '';
}

function shouldAcceptInboundPayload(payload) {
  const effectiveAllowedPhone = getEffectiveAllowedPhone();
  if (!effectiveAllowedPhone) {
    return true;
  }

  if (payload?.chat?.type !== 'dm') {
    return false;
  }

  const senderPhone = normalizePhoneValue(payload?.sender?.phone ?? payload?.sender?.jid ?? '');
  return senderPhone === effectiveAllowedPhone;
}

function describeDisconnect(statusCode, fallbackMessage = 'Connection failure') {
  if (statusCode === null || statusCode === undefined) {
    return fallbackMessage;
  }
  return `${fallbackMessage} (${statusCode})`;
}

async function buildStatusPayload() {
  return {
    status: 'ok',
    connected: connectionState.connected,
    pairing_state: connectionState.pairingState,
    last_disconnect_code: connectionState.lastDisconnectCode,
    auth_dir: connectionState.authDir,
    has_auth_state: await hasExistingAuthState(),
    qr: latestQr,
    qr_updated_at: qrUpdatedAt,
    connected_jid: connectionState.connectedJid,
    last_error: lastError,
    bridge_config: buildBridgeConfigPayload(),
    bridge_metrics: {
      session_faults_total: bridgeMetrics.sessionFaultsTotal,
      bad_mac_errors_total: bridgeMetrics.badMacErrorsTotal,
      decrypt_failures_total: bridgeMetrics.decryptFailuresTotal,
      retry_receipts_total: bridgeMetrics.retryReceiptsTotal,
      prekey_bundle_resets_total: bridgeMetrics.prekeyBundleResetsTotal,
      session_fault_window_count: bridgeMetrics.sessionFaultWindowCount,
      last_session_fault_at: bridgeMetrics.lastSessionFaultAt,
      last_session_fault_kind: bridgeMetrics.lastSessionFaultKind,
      last_session_fault_message: bridgeMetrics.lastSessionFaultMessage,
      self_heal_triggered_total: bridgeMetrics.selfHealTriggeredTotal,
      self_heal_completed_total: bridgeMetrics.selfHealCompletedTotal,
      self_heal_failed_total: bridgeMetrics.selfHealFailedTotal,
      self_heal_in_flight: bridgeMetrics.selfHealInFlight,
      last_self_heal_at: bridgeMetrics.lastSelfHealAt,
      last_self_heal_reason: bridgeMetrics.lastSelfHealReason,
      last_self_heal_recovered_at: bridgeMetrics.lastSelfHealRecoveredAt,
      last_self_heal_recovered_by: bridgeMetrics.lastSelfHealRecoveredBy,
      last_inbound_forward_at: bridgeMetrics.lastInboundForwardAt,
      last_inbound_forward_ms: bridgeMetrics.lastInboundForwardMs,
      last_outbound_send_at: bridgeMetrics.lastOutboundSendAt,
      last_outbound_send_ms: bridgeMetrics.lastOutboundSendMs,
    },
  };
}

function verifyBridgeToken(req, res, next) {
  if (!config.bridgeToken) {
    next();
    return;
  }

  const token = req.header('X-Bridge-Token') ?? '';
  if (token !== config.bridgeToken) {
    res.status(403).json({ error: 'Forbidden' });
    return;
  }

  next();
}

function extractMessageText(message) {
  return (
    message?.conversation ??
    message?.extendedTextMessage?.text ??
    message?.imageMessage?.caption ??
    message?.videoMessage?.caption ??
    null
  );
}

function coerceNumber(value) {
  if (value === null || value === undefined || value === '') {
    return null;
  }

  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function unwrapMessageContent(message) {
  let current = message;
  while (current) {
    if (current.ephemeralMessage?.message) {
      current = current.ephemeralMessage.message;
      continue;
    }
    if (current.viewOnceMessage?.message) {
      current = current.viewOnceMessage.message;
      continue;
    }
    if (current.viewOnceMessageV2?.message) {
      current = current.viewOnceMessageV2.message;
      continue;
    }
    if (current.viewOnceMessageV2Extension?.message) {
      current = current.viewOnceMessageV2Extension.message;
      continue;
    }
    if (current.documentWithCaptionMessage?.message) {
      current = current.documentWithCaptionMessage.message;
      continue;
    }
    break;
  }
  return current;
}

function buildAttachment(kind, source) {
  return {
    kind,
    mime_type: source?.mimetype ?? source?.mimeType ?? null,
    filename: source?.fileName ?? null,
    caption: source?.caption ?? null,
    size_bytes: coerceNumber(source?.fileLength),
    width: coerceNumber(source?.width),
    height: coerceNumber(source?.height),
    duration_ms: source?.seconds ? coerceNumber(source.seconds) * 1000 : null,
    sha256: source?.fileSha256 ? Buffer.from(source.fileSha256).toString('base64') : null,
    _source: source ?? null,
  };
}

function extractMessageDetails(message) {
  const normalizedMessage = unwrapMessageContent(message);
  const contentType = getContentType(normalizedMessage) ?? 'unknown';

  switch (contentType) {
    case 'conversation':
      return {
        type: 'text',
        text: normalizedMessage?.conversation ?? null,
        caption: null,
        quoted_message_id: null,
        mentions: [],
        attachments: [],
      };
    case 'extendedTextMessage':
      return {
        type: 'text',
        text: normalizedMessage?.extendedTextMessage?.text ?? null,
        caption: null,
        quoted_message_id: normalizedMessage?.extendedTextMessage?.contextInfo?.stanzaId ?? null,
        mentions: normalizedMessage?.extendedTextMessage?.contextInfo?.mentionedJid ?? [],
        attachments: [],
      };
    case 'imageMessage': {
      const image = normalizedMessage?.imageMessage;
      return {
        type: 'image',
        text: null,
        caption: image?.caption ?? null,
        quoted_message_id: image?.contextInfo?.stanzaId ?? null,
        mentions: image?.contextInfo?.mentionedJid ?? [],
        attachments: [buildAttachment('image', image)],
      };
    }
    case 'videoMessage': {
      const video = normalizedMessage?.videoMessage;
      return {
        type: 'video',
        text: null,
        caption: video?.caption ?? null,
        quoted_message_id: video?.contextInfo?.stanzaId ?? null,
        mentions: video?.contextInfo?.mentionedJid ?? [],
        attachments: [buildAttachment('video', video)],
      };
    }
    case 'audioMessage': {
      const audio = normalizedMessage?.audioMessage;
      return {
        type: audio?.ptt ? 'voice' : 'audio',
        text: null,
        caption: null,
        quoted_message_id: audio?.contextInfo?.stanzaId ?? null,
        mentions: audio?.contextInfo?.mentionedJid ?? [],
        attachments: [buildAttachment(audio?.ptt ? 'voice' : 'audio', audio)],
      };
    }
    case 'documentMessage': {
      const document = normalizedMessage?.documentMessage;
      return {
        type: 'document',
        text: null,
        caption: document?.caption ?? null,
        quoted_message_id: document?.contextInfo?.stanzaId ?? null,
        mentions: document?.contextInfo?.mentionedJid ?? [],
        attachments: [buildAttachment('document', document)],
      };
    }
    case 'stickerMessage':
      return {
        type: 'sticker',
        text: null,
        caption: null,
        quoted_message_id: normalizedMessage?.stickerMessage?.contextInfo?.stanzaId ?? null,
        mentions: normalizedMessage?.stickerMessage?.contextInfo?.mentionedJid ?? [],
        attachments: [buildAttachment('sticker', normalizedMessage?.stickerMessage)],
      };
    case 'locationMessage': {
      const location = normalizedMessage?.locationMessage;
      return {
        type: 'location',
        text: location?.name ?? location?.address ?? null,
        caption: null,
        quoted_message_id: location?.contextInfo?.stanzaId ?? null,
        mentions: location?.contextInfo?.mentionedJid ?? [],
        attachments: [
          {
            kind: 'location',
            mime_type: null,
            filename: null,
            caption: null,
            size_bytes: null,
            width: null,
            height: null,
            duration_ms: null,
            sha256: null,
            latitude: location?.degreesLatitude ?? null,
            longitude: location?.degreesLongitude ?? null,
            address: location?.address ?? null,
            name: location?.name ?? null,
          },
        ],
      };
    }
    case 'contactMessage': {
      const contact = normalizedMessage?.contactMessage;
      return {
        type: 'contact',
        text: contact?.displayName ?? null,
        caption: null,
        quoted_message_id: contact?.contextInfo?.stanzaId ?? null,
        mentions: contact?.contextInfo?.mentionedJid ?? [],
        attachments: [
          {
            kind: 'contact',
            mime_type: 'text/vcard',
            filename: null,
            caption: null,
            size_bytes: null,
            width: null,
            height: null,
            duration_ms: null,
            sha256: null,
            display_name: contact?.displayName ?? null,
            vcard: contact?.vcard ?? null,
          },
        ],
      };
    }
    case 'reactionMessage': {
      const reaction = normalizedMessage?.reactionMessage;
      return {
        type: 'reaction',
        text: reaction?.text ?? null,
        caption: null,
        quoted_message_id: reaction?.key?.id ?? null,
        mentions: [],
        attachments: [
          {
            kind: 'reaction',
            mime_type: null,
            filename: null,
            caption: null,
            size_bytes: null,
            width: null,
            height: null,
            duration_ms: null,
            sha256: null,
            emoji: reaction?.text ?? null,
            target_message_id: reaction?.key?.id ?? null,
          },
        ],
      };
    }
    case 'buttonsResponseMessage': {
      const response = normalizedMessage?.buttonsResponseMessage;
      return {
        type: 'button_reply',
        text: response?.selectedDisplayText ?? response?.selectedButtonId ?? null,
        caption: null,
        quoted_message_id: null,
        mentions: [],
        attachments: [],
      };
    }
    case 'listResponseMessage': {
      const response = normalizedMessage?.listResponseMessage;
      return {
        type: 'list_reply',
        text: response?.title ?? response?.singleSelectReply?.selectedRowId ?? null,
        caption: null,
        quoted_message_id: null,
        mentions: [],
        attachments: [],
      };
    }
    default:
      return {
        type: 'unknown',
        text: extractMessageText(normalizedMessage),
        caption: null,
        quoted_message_id: null,
        mentions: [],
        attachments: [],
      };
  }
}

function buildInboundPayload(msg) {
  const messageId = msg?.key?.id ?? `msg_${Date.now()}`;
  const identity = resolveInboundSenderIdentity(msg);
  const senderJid = identity.senderJid;
  const chatJid = msg?.key?.remoteJid ?? senderJid;
  const senderPhone = identity.senderPhone;
  const details = extractMessageDetails(msg?.message);
  const attachments = details.attachments.map((attachment, index) => {
    const attachmentId = `att_${index + 1}`;
    const mediaRef = `${messageId}:${attachmentId}`;
    const { _source, ...publicAttachment } = attachment;
    if (_source) {
      rememberInboundMedia(mediaRef, {
        kind: attachment.kind,
        source: _source,
        mime_type: attachment.mime_type,
        filename: attachment.filename,
      });
    }
    return {
      id: attachmentId,
      bridge_media_ref: mediaRef,
      download_url: `/media/${messageId}/${attachmentId}`,
      ...publicAttachment,
    };
  });

  return {
    schema_version: 1,
    event: 'message.inbound',
    event_id: `evt_${messageId}`,
    platform: 'whatsapp',
    channel: `whatsapp:${senderPhone ?? senderJid ?? chatJid ?? 'unknown'}`,
    sender: {
      jid: senderJid,
      phone: senderPhone,
      push_name: msg?.pushName ?? null,
    },
    chat: {
      jid: chatJid,
      type: chatJid?.endsWith('@g.us') ? 'group' : 'dm',
    },
    message: {
      id: messageId,
      type: details.type,
      text: details.text,
      caption: details.caption,
      timestamp_unix_ms: msg?.messageTimestamp ? coerceNumber(msg.messageTimestamp) * 1000 : Date.now(),
      quoted_message_id: details.quoted_message_id,
      mentions: details.mentions,
      attachments,
    },
    text: details.text ?? details.caption ?? null,
  };
}

async function forwardIncomingMessage(payload) {
  const headers = {};
  if (config.gatewayInternalToken) {
    headers['X-Internal-Token'] = config.gatewayInternalToken;
  }

  const startedAt = Date.now();
  const response = await axios.post(config.gatewayUrl, payload, { headers });
  const elapsedMs = Date.now() - startedAt;
  noteInboundForwardLatency(elapsedMs);
  const messageId = payload?.message?.id ?? 'unknown';
  const type = payload?.message?.type ?? 'unknown';
  console.log(`Forwarded inbound WhatsApp message ${messageId} type=${type} to Gateway in ${elapsedMs}ms`);
  return response;
}

async function closeSocket({ logout = false } = {}) {
  const currentSock = sock;
  sock = null;
  connectPromise = null;

  if (!currentSock) {
    return;
  }

  try {
    if (typeof currentSock.ev?.removeAllListeners === 'function') {
      currentSock.ev.removeAllListeners();
    }
  } catch {}

  if (logout && typeof currentSock.logout === 'function') {
    try {
      await currentSock.logout();
    } catch {}
  }

  try {
    currentSock.end?.(new Error('socket restart'));
  } catch {}

  try {
    currentSock.ws?.close?.();
  } catch {}
}

async function clearAuthState() {
  // Delete contents of authDir rather than the directory itself.
  // The parent dir may be owned by root (e.g. /var/lib/cosmic/whatsapp/)
  // so removing + recreating authDir would fail with EACCES.
  const entries = await fs.readdir(config.authDir).catch(() => []);
  await Promise.all(
    entries.map((entry) =>
      fs.rm(path.join(config.authDir, entry), { recursive: true, force: true })
    )
  );
}

async function resetForFreshPairing() {
  await closeSocket({ logout: false });
  await clearAuthState();
  clearQrState();
  lastError = null;
  setConnectionState({
    connected: false,
    pairingState: 'idle',
    lastDisconnectCode: null,
    connectedJid: null,
  });
}

function bindSocketEvents(currentSock, saveCreds) {
  currentSock.ev.on('creds.update', saveCreds);

  currentSock.ev.on('connection.update', async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      latestQr = qr;
      qrUpdatedAt = new Date().toISOString();
      lastError = null;
      setConnectionState({
        connected: false,
        pairingState: 'qr_ready',
        lastDisconnectCode: null,
        connectedJid: null,
      });
    }

    if (connection === 'connecting') {
      setConnectionState({
        connected: false,
        pairingState: 'connecting',
      });
      return;
    }

    if (connection === 'open') {
      clearQrState();
      lastError = null;
      markSelfHealRecovered('connection_open');
      setConnectionState({
        connected: true,
        pairingState: 'connected',
        lastDisconnectCode: null,
        connectedJid: currentSock.user?.id ?? null,
      });
      console.log('WhatsApp bridge connected.');
      return;
    }

    if (connection !== 'close') {
      return;
    }

    const statusCode = lastDisconnect?.error?.output?.statusCode ?? null;
    const authExists = await hasExistingAuthState();
    const loggedOut = statusCode === DisconnectReason.loggedOut;
    const shouldReconnect = !loggedOut && authExists;
    const errorMessage = lastDisconnect?.error?.message ?? null;

    if (sock === currentSock) {
      sock = null;
    }

    if (loggedOut) {
      clearQrState();
      bridgeMetrics.selfHealInFlight = false;
      selfHealPendingOpen = false;
      bridgeMetrics.lastSelfHealRecoveredBy = null;
    }

    if (loggedOut) {
      lastError = describeDisconnect(statusCode, 'WhatsApp session logged out');
    } else if (!authExists && statusCode !== null) {
      lastError = errorMessage || describeDisconnect(statusCode);
    }

    setConnectionState({
      connected: false,
      pairingState:
        loggedOut ? 'logged_out' : (!authExists && statusCode !== null) ? 'error' : authExists ? 'disconnected' : 'idle',
      lastDisconnectCode: statusCode,
      connectedJid: null,
    });

    if (shouldReconnect) {
      void ensureSocketConnected({ refresh: false });
    }
  });

  currentSock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;

    for (const msg of messages) {
      if (!msg.message || msg.key.fromMe) continue;

      const payload = buildInboundPayload(msg);
      const sender = payload.sender?.jid ?? payload.chat?.jid ?? 'unknown';
      const summary = payload.message?.text ?? payload.message?.caption ?? `[${payload.message?.type ?? 'unknown'}]`;

      if (!shouldAcceptInboundPayload(payload)) {
        console.log(`Ignoring WhatsApp message outside configured user scope: ${sender}`);
        continue;
      }

      console.log(`Incoming WhatsApp message from ${sender}: ${summary}`);

      try {
        await forwardIncomingMessage(payload);
      } catch (error) {
        const message = error?.response?.data ?? error?.message ?? error;
        console.error('Failed to forward incoming WhatsApp message:', message);
      }
    }
  });
}

async function ensureSocketConnected({ refresh = false } = {}) {
  if (connectionState.connected && sock) {
    return sock;
  }

  if (refresh) {
    await closeSocket({ logout: false });
    clearQrState();
  } else if (connectPromise) {
    return connectPromise;
  }

  connectPromise = (async () => {
    await ensureAuthDir();
    lastError = null;
    setConnectionState({
      connected: false,
      pairingState: 'connecting',
      connectedJid: null,
    });

    let waVersion;
    try {
      const { version } = await fetchLatestBaileysVersion();
      waVersion = version;
      console.log(`Fetched latest WhatsApp Web version: ${version.join('.')}`);
    } catch (versionError) {
      console.warn('Failed to fetch latest WhatsApp Web version, using Baileys default:', versionError?.message);
    }

    const { state, saveCreds } = await useMultiFileAuthState(config.authDir);
    const currentSock = makeWASocket({
      auth: state,
      ...(waVersion ? { version: waVersion } : {}),
      browser: Browsers.macOS('Google Chrome'),
      logger: createBridgeLogger({ class: 'baileys' }),
      markOnlineOnConnect: false,
      printQRInTerminal: false,
      connectTimeoutMs: 60000,
      defaultQueryTimeoutMs: 0,
      shouldSyncHistoryMessage: () => false,
    });

    sock = currentSock;
    bindSocketEvents(currentSock, saveCreds);
    return currentSock;
  })()
    .catch((error) => {
      lastError = error?.message ?? String(error);
      setConnectionState({
        connected: false,
        pairingState: 'error',
        connectedJid: null,
      });
      throw error;
    })
    .finally(() => {
      connectPromise = null;
    });

  return connectPromise;
}

async function waitForPairingArtifact(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (
      connectionState.connected ||
      latestQr ||
      connectionState.pairingState === 'error' ||
      connectionState.lastDisconnectCode !== null
    ) {
      return buildStatusPayload();
    }
    await sleep(250);
  }
  return buildStatusPayload();
}

app.get('/health', async (_req, res) => {
  res.json(await buildStatusPayload());
});

app.get('/status', verifyBridgeToken, async (_req, res) => {
  res.json(await buildStatusPayload());
});

app.get('/config', verifyBridgeToken, async (_req, res) => {
  res.json({
    status: 'ok',
    config: buildBridgeConfigPayload(),
  });
});

app.get('/media/:messageId/:attachmentId', verifyBridgeToken, async (req, res) => {
  try {
    const messageId = String(req.params?.messageId ?? '').trim();
    const attachmentId = String(req.params?.attachmentId ?? '').trim();
    if (!messageId || !attachmentId) {
      res.status(400).json({ error: 'messageId and attachmentId are required' });
      return;
    }
    const mediaRef = `${messageId}:${attachmentId}`;
    const payload = await readInboundMediaBuffer(mediaRef);
    if (payload.filename) {
      const safeFilename = String(payload.filename).replace(/"/g, '');
      res.setHeader('Content-Disposition', `inline; filename="${safeFilename}"`);
    }
    res.setHeader('Cache-Control', 'private, max-age=300');
    res.type(payload.mimeType || 'application/octet-stream');
    res.send(payload.buffer);
  } catch (error) {
    const statusCode = Number(error?.statusCode) || 500;
    res.status(statusCode).json({ error: error?.message ?? 'Failed to load media' });
  }
});

app.post('/config', verifyBridgeToken, async (req, res) => {
  const nextAllowedPhone = normalizePhoneValue(req.body?.allowed_phone ?? '');
  const nextSelfChatOnly = Boolean(req.body?.self_chat_only ?? false);

  bridgeConfig = {
    allowedPhone: nextAllowedPhone,
    selfChatOnly: nextSelfChatOnly,
  };

  try {
    await saveBridgeConfig();
    res.json({
      status: 'ok',
      config: buildBridgeConfigPayload(),
    });
  } catch (error) {
    const message = error?.message ?? String(error);
    res.status(500).json({
      status: 'error',
      error: message,
    });
  }
});

app.post('/pairing/qr', verifyBridgeToken, async (req, res) => {
  const refresh = req.body?.refresh !== false;
  const waitTimeoutMs = Math.max(1000, Math.min(envInt('WHATSAPP_QR_WAIT_TIMEOUT_MS', 15000), 60000));
  const requestTimeoutMs = Math.max(
    1000,
    Math.min(coerceNumber(req.body?.wait_timeout_ms) ?? waitTimeoutMs, 60000),
  );

  try {
    const authExists = await hasExistingAuthState().catch(() => false);
    if (
      authExists &&
      !connectionState.connected &&
      (
        connectionState.pairingState === 'logged_out' ||
        connectionState.lastDisconnectCode === DisconnectReason.loggedOut
      )
    ) {
      await resetForFreshPairing();
    }

    if (!connectionState.connected) {
      await ensureSocketConnected({ refresh });
    }

    const payload = await waitForPairingArtifact(requestTimeoutMs);
    if (!payload.connected && !payload.qr && (payload.last_error || payload.last_disconnect_code !== null)) {
      const disconnectMessage = payload.last_error || describeDisconnect(payload.last_disconnect_code);
      res.status(502).json({
        status: 'error',
        error: disconnectMessage,
        bridge_status: payload,
      });
      return;
    }

    const statusCode = payload.connected || payload.qr ? 200 : 202;
    res.status(statusCode).json(payload);
  } catch (error) {
    const message = error?.message ?? String(error);
    lastError = message;
    setConnectionState({
      connected: false,
      pairingState: 'error',
      connectedJid: null,
    });
    res.status(500).json({
      status: 'error',
      error: message,
    });
  }
});

app.delete('/session', verifyBridgeToken, async (_req, res) => {
  try {
    await closeSocket({ logout: true });
    await clearAuthState();
    clearQrState();
    lastError = null;
    setConnectionState({
      connected: false,
      pairingState: 'idle',
      lastDisconnectCode: null,
      connectedJid: null,
    });
    res.json(await buildStatusPayload());
  } catch (error) {
    const message = error?.message ?? String(error);
    lastError = message;
    res.status(500).json({
      status: 'error',
      error: message,
    });
  }
});

async function handleSend(req, res) {
  const { number, message } = req.body ?? {};

  if (!sock || !connectionState.connected) {
    res.status(500).json({ error: 'WhatsApp not connected' });
    return;
  }

  if (!number || !message) {
    res.status(400).json({ error: 'number and message are required' });
    return;
  }

  const normalizedPhone = normalizeBridgeRecipient(number);
  if (!normalizedPhone) {
    res.status(400).json({ error: 'number must be a valid WhatsApp destination' });
    return;
  }

  const effectiveAllowedPhone = getEffectiveAllowedPhone();
  if (effectiveAllowedPhone && normalizedPhone !== effectiveAllowedPhone) {
    res.status(403).json({
      error: `Outbound WhatsApp is restricted to ${effectiveAllowedPhone}`,
    });
    return;
  }

  const jid = `${normalizedPhone.replace(/\D/g, '')}@s.whatsapp.net`;

  try {
    const startedAt = Date.now();
    await sock.sendMessage(jid, { text: message });
    const elapsedMs = Date.now() - startedAt;
    noteOutboundSendLatency(elapsedMs);
    console.log(`Outgoing WhatsApp message to ${normalizedPhone} in ${elapsedMs}ms: ${message}`);
    res.json({ status: 'success' });
  } catch (error) {
    console.error('Failed to send WhatsApp message:', error?.message ?? error);
    res.status(500).json({ error: 'Failed to send message' });
  }
}

app.post('/send-message', verifyBridgeToken, handleSend);
app.post('/send', verifyBridgeToken, handleSend);

app.listen(config.port, config.host, async () => {
  await loadBridgeConfig();
  console.log(`WhatsApp bridge listening on http://${config.host}:${config.port}`);
  console.log(`Gateway intake URL: ${config.gatewayUrl}`);
  console.log(`Baileys auth directory: ${config.authDir}`);

  if (await hasExistingAuthState()) {
    console.log('Existing WhatsApp auth state detected. Attempting reconnect.');
    void ensureSocketConnected({ refresh: false });
  } else {
    console.log('No existing WhatsApp auth state detected. Waiting for pairing QR request.');
  }
});

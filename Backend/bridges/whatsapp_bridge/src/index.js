import fsSync from 'node:fs';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import axios from 'axios';
import express from 'express';
import makeWASocket, {
  DisconnectReason,
  getContentType,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys';

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

function envInt(name, fallback) {
  const raw = process.env[name];
  if (!raw) return fallback;
  const parsed = Number.parseInt(raw, 10);
  return Number.isNaN(parsed) ? fallback : parsed;
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
  bridgeToken: process.env.WHATSAPP_BRIDGE_TOKEN ?? '',
  gatewayInternalToken: process.env.GATEWAY_INTERNAL_TOKEN ?? '',
};

const app = express();
app.use(express.json({ limit: '2mb' }));

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

async function ensureAuthDir() {
  await fs.mkdir(config.authDir, { recursive: true });
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

function normalizePhoneFromJid(jid) {
  if (!jid || jid.endsWith('@g.us')) {
    return null;
  }

  const local = jid.split('@')[0] ?? '';
  const digits = local.replace(/\D/g, '');
  return digits ? `+${digits}` : null;
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
  const senderJid = msg?.key?.participant ?? msg?.key?.remoteJid ?? null;
  const chatJid = msg?.key?.remoteJid ?? senderJid;
  const senderPhone = normalizePhoneFromJid(senderJid);
  const details = extractMessageDetails(msg?.message);
  const attachments = details.attachments.map((attachment, index) => ({
    id: `att_${index + 1}`,
    bridge_media_ref: `${messageId}:att_${index + 1}`,
    download_url: null,
    ...attachment,
  }));

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

  return axios.post(config.gatewayUrl, payload, { headers });
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
  await fs.rm(config.authDir, { recursive: true, force: true });
  await ensureAuthDir();
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

    if (sock === currentSock) {
      sock = null;
    }

    if (loggedOut) {
      clearQrState();
    }

    setConnectionState({
      connected: false,
      pairingState: loggedOut ? 'logged_out' : authExists ? 'disconnected' : 'idle',
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

    const { state, saveCreds } = await useMultiFileAuthState(config.authDir);
    const currentSock = makeWASocket({
      auth: state,
      printQRInTerminal: false,
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
    if (connectionState.connected || latestQr || connectionState.pairingState === 'error') {
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

app.post('/pairing/qr', verifyBridgeToken, async (req, res) => {
  const refresh = req.body?.refresh !== false;
  const waitTimeoutMs = Math.max(1000, Math.min(envInt('WHATSAPP_QR_WAIT_TIMEOUT_MS', 15000), 60000));
  const requestTimeoutMs = Math.max(
    1000,
    Math.min(coerceNumber(req.body?.wait_timeout_ms) ?? waitTimeoutMs, 60000),
  );

  try {
    if (!connectionState.connected) {
      await ensureSocketConnected({ refresh });
    }

    const payload = await waitForPairingArtifact(requestTimeoutMs);
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

  const jid = number.includes('@')
    ? number
    : `${number.replace(/\D/g, '')}@s.whatsapp.net`;

  try {
    await sock.sendMessage(jid, { text: message });
    console.log(`Outgoing WhatsApp message to ${number}: ${message}`);
    res.json({ status: 'success' });
  } catch (error) {
    console.error('Failed to send WhatsApp message:', error?.message ?? error);
    res.status(500).json({ error: 'Failed to send message' });
  }
}

app.post('/send-message', verifyBridgeToken, handleSend);
app.post('/send', verifyBridgeToken, handleSend);

app.listen(config.port, config.host, async () => {
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

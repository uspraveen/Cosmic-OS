import path from 'node:path';
import { fileURLToPath } from 'node:url';

import axios from 'axios';
import express from 'express';
import makeWASocket, {
  DisconnectReason,
  getContentType,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys';
import qrcode from 'qrcode-terminal';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const BRIDGE_ROOT = path.resolve(__dirname, '..');

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

const config = {
  gatewayUrl:
    process.env.GATEWAY_INTERNAL_URL ??
    process.env.COSMIC_URL ??
    'http://127.0.0.1:5000/webhook',
  host: process.env.WHATSAPP_BRIDGE_HOST ?? '127.0.0.1',
  port: envInt('WHATSAPP_BRIDGE_PORT', 3000),
  authDir: resolveBridgePath(process.env.WHATSAPP_AUTH_DIR, ['store', 'auth']),
  bridgeToken: process.env.WHATSAPP_BRIDGE_TOKEN ?? '',
  gatewayInternalToken: process.env.GATEWAY_INTERNAL_TOKEN ?? '',
};

const app = express();
app.use(express.json());

let sock = null;
let connectionState = {
  connected: false,
  lastDisconnectCode: null,
  authDir: config.authDir,
};

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

async function connectToWhatsApp() {
  const { state, saveCreds } = await useMultiFileAuthState(config.authDir);

  sock = makeWASocket({
    auth: state,
    printQRInTerminal: true,
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      qrcode.generate(qr, { small: true });
    }

    if (connection === 'close') {
      const statusCode = lastDisconnect?.error?.output?.statusCode ?? null;
      connectionState = {
        ...connectionState,
        connected: false,
        lastDisconnectCode: statusCode,
      };
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
      if (shouldReconnect) {
        void connectToWhatsApp();
      }
    } else if (connection === 'open') {
      connectionState = {
        ...connectionState,
        connected: true,
        lastDisconnectCode: null,
      };
      console.log('WhatsApp bridge connected.');
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;

    for (const msg of messages) {
      if (!msg.message || msg.key.fromMe) continue;

      const payload = buildInboundPayload(msg);
      const sender = payload.sender?.jid ?? payload.chat?.jid ?? 'unknown';
      const summary = payload.message?.text ?? payload.message?.caption ?? `[${payload.message?.type ?? 'unknown'}]`;

      console.log(`Incoming WhatsApp message from ${sender}: ${summary}`);

      try {
        const response = await forwardIncomingMessage(payload);
        if (response.data?.reply) {
          await sock.sendMessage(sender, { text: response.data.reply });
        }
      } catch (error) {
        console.error('Failed to forward incoming WhatsApp message:', error?.message ?? error);
      }
    }
  });
}

app.get('/health', (_req, res) => {
  res.json({
    status: 'ok',
    connected: connectionState.connected,
    auth_dir: connectionState.authDir,
  });
});

app.get('/status', verifyBridgeToken, (_req, res) => {
  res.json(connectionState);
});

async function handleSend(req, res) {
  const { number, message } = req.body ?? {};

  if (!sock) {
    res.status(500).json({ error: 'WhatsApp not connected' });
    return;
  }

  if (!number || !message) {
    res.status(400).json({ error: 'number and message are required' });
    return;
  }

  const jid = number.includes('@s.whatsapp.net')
    ? number
    : `${number}@s.whatsapp.net`;

  try {
    await sock.sendMessage(jid, { text: message });
    console.log(`Outgoing WhatsApp message to ${number}: ${message}`);
    res.json({ status: 'success' });
  } catch (error) {
    console.error('Failed to send WhatsApp message:', error);
    res.status(500).json({ error: 'Failed to send message' });
  }
}

app.post('/send-message', verifyBridgeToken, handleSend);
app.post('/send', verifyBridgeToken, handleSend);

app.listen(config.port, config.host, () => {
  console.log(`WhatsApp bridge listening on http://${config.host}:${config.port}`);
  console.log(`Baileys auth directory: ${config.authDir}`);
  void connectToWhatsApp();
});

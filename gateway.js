import makeWASocket, { useMultiFileAuthState, DisconnectReason } from '@whiskeysockets/baileys';
import axios from 'axios';
import qrcode from 'qrcode-terminal';
import express from 'express';

// Configuration
const COSMIC_URL = 'http://127.0.0.1:5000/webhook'; // Python Brain
const PORT = 3000; // Node Gateway Port

const app = express();
app.use(express.json());

let sock; // Global socket variable

async function connectToWhatsApp() {
    const { state, saveCreds } = await useMultiFileAuthState('auth_info_baileys');

    // FIX: Removed .default here.
    sock = makeWASocket({
        auth: state,
        printQRInTerminal: true
    });

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', (update) => {
        const { connection, lastDisconnect, qr } = update;

        if (qr) qrcode.generate(qr, { small: true });

        if (connection === 'close') {
            const shouldReconnect = (lastDisconnect?.error)?.output?.statusCode !== DisconnectReason.loggedOut;
            if (shouldReconnect) connectToWhatsApp();
        } else if (connection === 'open') {
            console.log('✅ Gateway Connected to WhatsApp!');
        }
    });

    // LISTENER: Forward incoming messages to Python
    sock.ev.on('messages.upsert', async ({ messages, type }) => {
        if (type !== 'notify') return;
        for (const msg of messages) {
            if (!msg.message || msg.key.fromMe) continue;

            const text = msg.message.conversation || msg.message.extendedTextMessage?.text;
            const sender = msg.key.remoteJid;

            if (!text) continue;
            console.log(`📨 Incoming: ${text}`);

            try {
                const response = await axios.post(COSMIC_URL, { sender, text });
                if (response.data && response.data.reply) {
                    await sock.sendMessage(sender, { text: response.data.reply });
                }
            } catch (error) {
                console.error("❌ Python Brain unreachable");
            }
        }
    });
}

// NEW: Endpoint to send messages FROM Python
app.post('/send-message', async (req, res) => {
    const { number, message } = req.body;

    if (!sock) return res.status(500).json({ error: "WhatsApp not connected" });

    // Format number for WhatsApp
    const jid = number.includes('@s.whatsapp.net') ? number : `${number}@s.whatsapp.net`;

    try {
        await sock.sendMessage(jid, { text: message });
        console.log(`📤 Proactive Send to ${number}: ${message}`);
        res.json({ status: "success" });
    } catch (error) {
        console.error("Failed to send:", error);
        res.status(500).json({ error: "Failed to send message" });
    }
});

// Start the Node Server
app.listen(PORT, () => {
    console.log(`🔌 Gateway API running on port ${PORT}`);
    connectToWhatsApp();
});
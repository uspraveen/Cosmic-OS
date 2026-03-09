import test from "node:test";
import assert from "node:assert/strict";

import {
  normalizePhoneFromJid,
  normalizePhoneValue,
  resolveInboundSenderIdentity,
} from "./whatsapp_identity.js";

test("normalizePhoneFromJid strips device suffix from phone-backed jids", () => {
  assert.equal(
    normalizePhoneFromJid("919677001106:10@s.whatsapp.net"),
    "+919677001106",
  );
});

test("normalizePhoneFromJid ignores lid identities", () => {
  assert.equal(normalizePhoneFromJid("117368571359350@lid"), null);
});

test("normalizePhoneValue keeps plain phone filters canonical", () => {
  assert.equal(normalizePhoneValue("+1 (215) 307-9021"), "+12153079021");
});

test("resolveInboundSenderIdentity prefers nested phone-backed jid over lid sender", () => {
  const identity = resolveInboundSenderIdentity({
    key: {
      participant: "117368571359350@lid",
      remoteJid: "117368571359350@lid",
    },
    message: {
      messageContextInfo: {
        deviceListMetadata: {
          senderKeyHash: "ignored",
        },
        participantPn: "12153079021@s.whatsapp.net",
      },
    },
  });

  assert.equal(identity.senderJid, "12153079021@s.whatsapp.net");
  assert.equal(identity.senderPhone, "+12153079021");
  assert.equal(identity.lidJid, "117368571359350@lid");
});

test("resolveInboundSenderIdentity finds nested pnJid mappings", () => {
  const identity = resolveInboundSenderIdentity({
    key: {
      participant: "117368571359350@lid",
    },
    message: {
      contactAction: {
        lidJid: "117368571359350@lid",
        pnJid: "12153079021@s.whatsapp.net",
      },
    },
  });

  assert.equal(identity.senderJid, "12153079021@s.whatsapp.net");
  assert.equal(identity.senderPhone, "+12153079021");
});

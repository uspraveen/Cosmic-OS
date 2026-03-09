const PHONE_JID_SUFFIXES = ["@s.whatsapp.net", "@c.us"];
const GROUP_JID_SUFFIX = "@g.us";
const LID_JID_SUFFIX = "@lid";

function normalizePhoneValue(rawValue) {
  const text = String(rawValue ?? "").trim();
  if (!text) {
    return "";
  }
  const digits = text.replace(/\D/g, "");
  return digits ? `+${digits}` : "";
}

function normalizePhoneFromJid(jid) {
  const text = String(jid ?? "").trim();
  if (!text) {
    return null;
  }
  if (text.endsWith(GROUP_JID_SUFFIX) || text.endsWith(LID_JID_SUFFIX)) {
    return null;
  }

  const local = (text.split("@", 1)[0] ?? "").split(":", 1)[0] ?? "";
  const digits = local.replace(/\D/g, "");
  return digits ? `+${digits}` : null;
}

function isPhoneBackedJid(value) {
  if (typeof value !== "string") {
    return false;
  }
  return PHONE_JID_SUFFIXES.some((suffix) => value.endsWith(suffix));
}

function isLidJid(value) {
  return typeof value === "string" && value.endsWith(LID_JID_SUFFIX);
}

function findFirstStringByKeys(node, keys, predicate = () => true, seen = new Set()) {
  if (!node || typeof node !== "object") {
    return null;
  }
  if (seen.has(node)) {
    return null;
  }
  seen.add(node);

  if (Array.isArray(node)) {
    for (const item of node) {
      const match = findFirstStringByKeys(item, keys, predicate, seen);
      if (match) {
        return match;
      }
    }
    return null;
  }

  for (const key of keys) {
    const value = node[key];
    if (typeof value === "string" && predicate(value)) {
      return value;
    }
  }

  for (const value of Object.values(node)) {
    const match = findFirstStringByKeys(value, keys, predicate, seen);
    if (match) {
      return match;
    }
  }
  return null;
}

function firstNonEmpty(...values) {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) {
      return value;
    }
  }
  return null;
}

function resolveInboundSenderIdentity(msg) {
  const participant = typeof msg?.key?.participant === "string" ? msg.key.participant : null;
  const remoteJid = typeof msg?.key?.remoteJid === "string" ? msg.key.remoteJid : null;

  const phoneBackedJid = firstNonEmpty(
    typeof msg?.key?.participantPn === "string" && isPhoneBackedJid(msg.key.participantPn)
      ? msg.key.participantPn
      : null,
    isPhoneBackedJid(participant) ? participant : null,
    isPhoneBackedJid(remoteJid) ? remoteJid : null,
    findFirstStringByKeys(
      msg,
      ["participantPn", "senderPn", "pnJid"],
      isPhoneBackedJid,
    ),
  );

  const lidJid = firstNonEmpty(
    isLidJid(participant) ? participant : null,
    isLidJid(remoteJid) ? remoteJid : null,
    findFirstStringByKeys(
      msg,
      ["participantLid", "senderLid", "lidJid"],
      isLidJid,
    ),
  );

  const senderJid = firstNonEmpty(phoneBackedJid, participant, remoteJid, lidJid);
  const senderPhone = normalizePhoneFromJid(phoneBackedJid ?? senderJid);

  return {
    senderJid,
    senderPhone,
    phoneBackedJid,
    lidJid,
    rawParticipantJid: participant,
    rawRemoteJid: remoteJid,
  };
}

export {
  normalizePhoneValue,
  normalizePhoneFromJid,
  resolveInboundSenderIdentity,
};

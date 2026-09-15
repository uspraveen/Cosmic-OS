/**
 * AskUser questions from the browser agent are free text. The model often
 * pastes a briefing, a signup URL, and a policy dump into a single string.
 * The card cannot render that as a title — extract a short ask, the site,
 * and an optional account, and keep the rest behind a fold.
 */

export type BrowserInterruptKind = 'password' | 'verification_code' | 'confirm' | 'blocked' | 'generic'

export interface BrowserInterruptPresentation {
  kicker: string
  title: string
  site: string | null
  username: string | null
  summary: string | null
  leftover: string | null
  fieldLabel: string
  primaryAction: string
  placeholder: string
}

const URL_RE = /https?:\/\/[^\s<>"')\]]+/gi
const EMAIL_RE = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi

const KICKER: Record<BrowserInterruptKind, string> = {
  password: 'Password requested',
  verification_code: 'Verification code needed',
  confirm: 'Waiting on you',
  blocked: 'Stuck — needs help',
  generic: 'Needs your input',
}

const TITLE: Record<BrowserInterruptKind, string> = {
  password: 'Sign in',
  verification_code: 'Verification code',
  confirm: 'Your turn',
  blocked: 'This page is stuck',
  generic: 'The browser needs an answer',
}

const clip = (value: string, limit: number) => {
  const text = value.replace(/\s+/g, ' ').trim()
  if (text.length <= limit) return text
  return `${text.slice(0, Math.max(0, limit - 1)).trimEnd()}…`
}

const hostnameOf = (value: string | null | undefined): string | null => {
  const raw = String(value || '').trim()
  if (!raw) return null
  try {
    const host = new URL(raw.includes('://') ? raw : `https://${raw}`).hostname.toLowerCase()
    if (!host) return null
    return host.replace(/^www\./, '')
  } catch {
    return null
  }
}

const cleanProse = (value: string): string => (
  value
    .replace(URL_RE, ' ')
    .replace(/\(\s*\)/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
)

const firstSentence = (value: string): string => {
  const text = value.replace(/\s+/g, ' ').trim()
  if (!text) return ''
  const match = text.match(/^(.+?[.!?])(?:\s|$)/)
  return (match?.[1] || text).trim()
}

const usefulSummary = (prose: string, kind: BrowserInterruptKind, title: string): string | null => {
  const sentence = firstSentence(prose)
  if (!sentence) return null
  const normalized = sentence.toLowerCase()
  if (normalized === title.toLowerCase()) return null
  if (kind === 'password' && /password|sign[- ]?in|log[- ]?in/.test(normalized) && sentence.length < 90) {
    return null
  }
  if (kind === 'verification_code' && /code|otp|verify/.test(normalized) && sentence.length < 90) {
    return null
  }
  return clip(sentence, 160)
}

export const presentBrowserInterrupt = (
  question: string,
  kind: BrowserInterruptKind,
  pageUrl?: string | null,
): BrowserInterruptPresentation => {
  const raw = String(question || '').replace(/\u0000/g, '').trim()
  const urls = raw.match(URL_RE) || []
  const site = hostnameOf(pageUrl) || hostnameOf(urls[0]) || null
  const emails = raw.match(EMAIL_RE) || []
  const username = emails[0] || null
  const prose = cleanProse(raw)
  const title = kind === 'generic' || kind === 'blocked'
    ? (clip(firstSentence(prose) || TITLE[kind], 88) || TITLE[kind])
    : TITLE[kind]
  const summary = usefulSummary(prose, kind, title)
  // Password / code / confirm already have a dedicated sheet. The model essay
  // is almost never useful there and was the original title disaster.
  const keepEssay = kind === 'generic' || kind === 'blocked'
  let leftover = keepEssay ? prose : ''
  if (summary && leftover.startsWith(summary.replace(/…$/, ''))) {
    leftover = leftover.slice(summary.replace(/…$/, '').length).replace(/^[.\s]+/, '').trim()
  }
  if (leftover && leftover.toLowerCase() === title.toLowerCase()) leftover = ''
  if (summary && leftover.toLowerCase() === summary.toLowerCase()) leftover = ''
  const compactLeftover = leftover && leftover.length > 48 ? leftover : (leftover && leftover !== summary ? leftover : '')
  return {
    kicker: KICKER[kind],
    title,
    site,
    username,
    summary: keepEssay ? summary : null,
    leftover: compactLeftover || null,
    fieldLabel: kind === 'password' ? 'Password' : kind === 'verification_code' ? 'Code' : 'Your answer',
    primaryAction: kind === 'password' || kind === 'verification_code' || kind === 'confirm' ? 'Continue' : 'Send',
    placeholder: kind === 'password' ? '' : kind === 'verification_code' ? '6-digit code' : 'Your answer',
  }
}

/**
 * Weather alert classification + the ledger that decides when an alert is
 * allowed to take over the Dynamic Island again.
 *
 * The island used to dedupe auto-peeks on the alert's *display string*. That
 * string is not stable for a stable condition: the heat rules alone flip between
 * "Hot conditions" (current temp) and "Hot day ahead" (daily high) as the day
 * warms and cools, and the bridge rounds the current temperature, so a reading
 * hovering near the threshold flaps between the two every refresh. Every flip
 * read as a brand-new alert. Dedupe therefore keys off a coarse condition
 * category instead, and a peek is remembered for a cooldown rather than for a
 * single string value.
 */

export const WEATHER_ALERT_LOG_KEY = 'cosmic.weatherAlertLog.v1'

export type WeatherAlertTier = 'severe' | 'advisory'

/**
 * Condition family. Deliberately coarser than the message: drizzle strengthening
 * into showers is the same standing "it is wet" condition and must not
 * re-announce itself.
 */
export type WeatherAlertCategory = 'thunderstorm' | 'snow' | 'rain' | 'ice' | 'fog' | 'heat'

export interface WeatherAlertInfo {
  tier: WeatherAlertTier | null
  category: WeatherAlertCategory | null
  alertMessage: string
}

/** Open-Meteo / bridge payload is always °C. */
export const HEAT_ADVISORY_CURRENT_C = 30
export const HEAT_ADVISORY_HIGH_C = 32

/** A severe alert that ran its course re-announces while it is still going. */
export const SEVERE_RESHOW_MS = 45 * 60 * 1000
/**
 * An advisory that ran its course stays quiet for half a day. A summer heat
 * advisory can be continuously true for months; being told twice a day is the
 * point at which it is still information rather than noise.
 */
export const ADVISORY_RESHOW_MS = 12 * 60 * 60 * 1000
/** A peek cut short by higher-priority island UI was never really seen. */
export const INTERRUPTED_RETRY_MS = 2 * 60 * 1000
/** A category absent this long ends the episode; its next appearance is new. */
export const EPISODE_GAP_MS = 3 * 60 * 60 * 1000
/** Bounds how often a still-present condition rewrites the persisted ledger. */
export const SEEN_WRITE_THROTTLE_MS = 5 * 60 * 1000

export interface WeatherAlertSample {
  wmo?: number | null
  temp?: number | null
  high?: number | null
}

export interface WeatherAlertRecord {
  /** When the peek for this category was last torn down, for any reason. */
  at: number
  /** Whether that peek ran its full course, as opposed to being cut short. */
  completed: boolean
  /** Last time this category was observed in a weather sample. */
  seenAt: number
}

export type WeatherAlertLog = Record<string, WeatherAlertRecord>

const SEVERE_BY_WMO: Array<[number[], WeatherAlertCategory, string]> = [
  [[95, 96, 99], 'thunderstorm', 'Thunderstorm Alert'],
  [[71, 73, 75, 85, 86], 'snow', 'Heavy Snow Alert'],
]

const ADVISORY_BY_WMO: Array<[number[], WeatherAlertCategory, string]> = [
  [[80, 81, 82], 'rain', 'Shower activity'],
  [[65], 'rain', 'Heavy rain'],
  [[61, 63], 'rain', 'Rain expected'],
  [[53, 55], 'rain', 'Steady drizzle'],
  [[66, 67], 'ice', 'Icy / freezing rain'],
  [[56, 57], 'ice', 'Freezing drizzle'],
  [[45, 48], 'fog', 'Low visibility (fog)'],
]

const NO_ALERT: WeatherAlertInfo = { tier: null, category: null, alertMessage: '' }

export function getWeatherAlertInfo(weather: WeatherAlertSample): WeatherAlertInfo {
  const wmo = weather.wmo ?? 0
  const temp = Number(weather.temp)
  const high = weather.high === undefined || weather.high === null ? Number.NaN : Number(weather.high)

  for (const [codes, category, alertMessage] of SEVERE_BY_WMO) {
    if (codes.includes(wmo)) return { tier: 'severe', category, alertMessage }
  }
  for (const [codes, category, alertMessage] of ADVISORY_BY_WMO) {
    if (codes.includes(wmo)) return { tier: 'advisory', category, alertMessage }
  }

  if (Number.isFinite(temp) && temp >= HEAT_ADVISORY_CURRENT_C) {
    return { tier: 'advisory', category: 'heat', alertMessage: 'Hot conditions' }
  }
  if (Number.isFinite(high) && high >= HEAT_ADVISORY_HIGH_C) {
    return { tier: 'advisory', category: 'heat', alertMessage: 'Hot day ahead' }
  }

  return NO_ALERT
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
}

function asNumber(value: unknown): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

export function loadWeatherAlertLog(): WeatherAlertLog {
  if (typeof window === 'undefined') return {}
  try {
    const raw = window.localStorage.getItem(WEATHER_ALERT_LOG_KEY)
    if (!raw) return {}
    const parsed = asRecord(JSON.parse(raw))
    const log: WeatherAlertLog = {}
    for (const [key, value] of Object.entries(parsed)) {
      const entry = asRecord(value)
      const at = asNumber(entry.at)
      if (!at) continue
      log[key] = {
        at,
        completed: entry.completed === true,
        // Pre-`seenAt` entries fall back to the show time, which only ever
        // makes the episode look older — never newer than it really is.
        seenAt: asNumber(entry.seenAt) || at,
      }
    }
    return log
  } catch {
    return {}
  }
}

export function saveWeatherAlertLog(log: WeatherAlertLog) {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(WEATHER_ALERT_LOG_KEY, JSON.stringify(log))
  } catch {
    // localStorage can be unavailable in constrained test/webview contexts.
  }
}

export function shouldPeekWeatherAlert(
  log: WeatherAlertLog,
  category: WeatherAlertCategory,
  tier: WeatherAlertTier,
  now: number,
): boolean {
  const record = log[category]
  if (!record) return true
  const cooldown = record.completed
    ? tier === 'severe'
      ? SEVERE_RESHOW_MS
      : ADVISORY_RESHOW_MS
    : INTERRUPTED_RETRY_MS
  return now - record.at >= cooldown
}

/**
 * Records that a peek for `category` is over. Every teardown path must call
 * this — a path that forgets leaves the alert looking un-shown, which is what
 * let interrupted peeks fire again on the very next render.
 */
export function recordWeatherAlertShown(
  log: WeatherAlertLog,
  category: WeatherAlertCategory,
  completed: boolean,
  now: number,
): WeatherAlertLog {
  return { ...log, [category]: { at: now, completed, seenAt: now } }
}

/**
 * Keeps the episode window alive while a condition persists, and forgets a
 * category that has been gone long enough that its return is a new event.
 * Returns the same reference when nothing changed, so callers can skip the write.
 */
export function noteWeatherAlertObserved(
  log: WeatherAlertLog,
  category: WeatherAlertCategory,
  now: number,
): WeatherAlertLog {
  const record = log[category]
  if (!record) return log

  if (now - record.seenAt > EPISODE_GAP_MS) {
    const next = { ...log }
    delete next[category]
    return next
  }

  if (now - record.seenAt < SEEN_WRITE_THROTTLE_MS) return log
  return { ...log, [category]: { ...record, seenAt: now } }
}

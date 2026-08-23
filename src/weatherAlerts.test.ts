import { describe, expect, it } from 'vitest'
import {
  ADVISORY_RESHOW_MS,
  EPISODE_GAP_MS,
  INTERRUPTED_RETRY_MS,
  SEEN_WRITE_THROTTLE_MS,
  SEVERE_RESHOW_MS,
  getWeatherAlertInfo,
  noteWeatherAlertObserved,
  recordWeatherAlertShown,
  shouldPeekWeatherAlert,
  type WeatherAlertLog,
} from './weatherAlerts'

const T0 = 1_700_000_000_000

describe('getWeatherAlertInfo', () => {
  it('gives both heat wordings the same category', () => {
    // The bug this whole ledger exists for: on a 34C day the current temp
    // crosses 30C mid-morning and falls back under it in the evening, flipping
    // the message twice a day while the condition never changed.
    const hotNow = getWeatherAlertInfo({ wmo: 0, temp: 31, high: 34 })
    const hotLater = getWeatherAlertInfo({ wmo: 0, temp: 29, high: 34 })

    expect(hotNow.alertMessage).toBe('Hot conditions')
    expect(hotLater.alertMessage).toBe('Hot day ahead')
    expect(hotNow.category).toBe('heat')
    expect(hotLater.category).toBe('heat')
  })

  it('collapses every wet-weather wording onto one category', () => {
    const codes = [80, 81, 82, 65, 61, 63, 53, 55]
    const categories = codes.map((wmo) => getWeatherAlertInfo({ wmo, temp: 10, high: 12 }).category)
    expect(new Set(categories)).toEqual(new Set(['rain']))
  })

  it('keeps severe conditions distinct and ranked above heat', () => {
    const storm = getWeatherAlertInfo({ wmo: 95, temp: 33, high: 35 })
    expect(storm.tier).toBe('severe')
    expect(storm.category).toBe('thunderstorm')

    const snow = getWeatherAlertInfo({ wmo: 75, temp: -2, high: 0 })
    expect(snow.tier).toBe('severe')
    expect(snow.category).toBe('snow')
  })

  it('reports no alert on an ordinary day', () => {
    expect(getWeatherAlertInfo({ wmo: 0, temp: 21, high: 25 })).toEqual({
      tier: null,
      category: null,
      alertMessage: '',
    })
  })

  it('ignores a missing daily high instead of treating it as cold', () => {
    expect(getWeatherAlertInfo({ wmo: 0, temp: 20, high: null }).category).toBeNull()
    expect(getWeatherAlertInfo({ wmo: 0, temp: 20, high: undefined }).category).toBeNull()
  })
})

describe('shouldPeekWeatherAlert', () => {
  it('shows a category that has never been shown', () => {
    expect(shouldPeekWeatherAlert({}, 'heat', 'advisory', T0)).toBe(true)
  })

  it('keeps a completed advisory quiet for half a day', () => {
    const log = recordWeatherAlertShown({}, 'heat', true, T0)
    expect(shouldPeekWeatherAlert(log, 'heat', 'advisory', T0 + ADVISORY_RESHOW_MS - 1)).toBe(false)
    expect(shouldPeekWeatherAlert(log, 'heat', 'advisory', T0 + ADVISORY_RESHOW_MS)).toBe(true)
  })

  it('lets a completed severe alert repeat while it is still going', () => {
    const log = recordWeatherAlertShown({}, 'thunderstorm', true, T0)
    expect(shouldPeekWeatherAlert(log, 'thunderstorm', 'severe', T0 + SEVERE_RESHOW_MS - 1)).toBe(false)
    expect(shouldPeekWeatherAlert(log, 'thunderstorm', 'severe', T0 + SEVERE_RESHOW_MS)).toBe(true)
  })

  it('retries an interrupted peek soon, but not immediately', () => {
    const log = recordWeatherAlertShown({}, 'heat', false, T0)
    // The old code recorded nothing here, so the alert re-fired on the next render.
    expect(shouldPeekWeatherAlert(log, 'heat', 'advisory', T0 + 1)).toBe(false)
    expect(shouldPeekWeatherAlert(log, 'heat', 'advisory', T0 + INTERRUPTED_RETRY_MS)).toBe(true)
  })

  it('tracks categories independently', () => {
    const log = recordWeatherAlertShown({}, 'heat', true, T0)
    expect(shouldPeekWeatherAlert(log, 'heat', 'advisory', T0 + 1000)).toBe(false)
    expect(shouldPeekWeatherAlert(log, 'fog', 'advisory', T0 + 1000)).toBe(true)
  })
})

describe('noteWeatherAlertObserved', () => {
  it('returns the same reference when there is nothing to write', () => {
    const log = recordWeatherAlertShown({}, 'heat', true, T0)
    expect(noteWeatherAlertObserved(log, 'heat', T0 + 1000)).toBe(log)
    expect(noteWeatherAlertObserved(log, 'fog', T0 + 1000)).toBe(log)
  })

  it('refreshes seenAt once past the write throttle', () => {
    const log = recordWeatherAlertShown({}, 'heat', true, T0)
    const next = noteWeatherAlertObserved(log, 'heat', T0 + SEEN_WRITE_THROTTLE_MS)
    expect(next).not.toBe(log)
    expect(next.heat.seenAt).toBe(T0 + SEEN_WRITE_THROTTLE_MS)
    expect(next.heat.at).toBe(T0)
  })

  it('forgets a category that has been gone long enough to be a new episode', () => {
    const log = recordWeatherAlertShown({}, 'rain', true, T0)
    const next = noteWeatherAlertObserved(log, 'rain', T0 + EPISODE_GAP_MS + 1)
    expect(next.rain).toBeUndefined()
    expect(shouldPeekWeatherAlert(next, 'rain', 'advisory', T0 + EPISODE_GAP_MS + 1)).toBe(true)
  })

  it('a continuously present advisory stays deduped across many refreshes', () => {
    // 15-minute weather ticks across a hot day: one announcement, not one per tick.
    let log: WeatherAlertLog = recordWeatherAlertShown({}, 'heat', true, T0)
    let peeks = 0
    for (let tick = 1; tick <= 4 * 12; tick += 1) {
      const now = T0 + tick * 15 * 60 * 1000
      log = noteWeatherAlertObserved(log, 'heat', now)
      if (shouldPeekWeatherAlert(log, 'heat', 'advisory', now)) {
        log = recordWeatherAlertShown(log, 'heat', true, now)
        peeks += 1
      }
    }
    // 12 hours of ticks, so exactly one re-show at the cooldown boundary.
    expect(peeks).toBe(1)
  })

  it('a brief dip below the threshold does not re-arm the alert', () => {
    // Rounded temps flap around the boundary; the condition did not really end.
    let log: WeatherAlertLog = recordWeatherAlertShown({}, 'heat', true, T0)
    const backTenMinutesLater = T0 + 10 * 60 * 1000
    log = noteWeatherAlertObserved(log, 'heat', backTenMinutesLater)
    expect(shouldPeekWeatherAlert(log, 'heat', 'advisory', backTenMinutesLater)).toBe(false)
  })
})

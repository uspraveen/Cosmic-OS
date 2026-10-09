import { describe, expect, it } from 'vitest'
import { messageCarriesLiveWork } from './stoppedMessage'

describe('messageCarriesLiveWork', () => {
  it('keeps a text-less message that is showing a browser run', () => {
    expect(messageCarriesLiveWork({ browserProgress: { taskId: 'tsk_1', phase: 'running' } })).toBe(true)
  })

  it('keeps deck, sheet, console and inline-progress messages', () => {
    expect(messageCarriesLiveWork({ slideProgress: { current: 1 } })).toBe(true)
    expect(messageCarriesLiveWork({ sheetsProgress: { rows: [] } })).toBe(true)
    expect(messageCarriesLiveWork({ alphaTerminalLog: [{ id: 'x' }] })).toBe(true)
    expect(messageCarriesLiveWork({ activityLog: [{ id: 'a' }] })).toBe(true)
    expect(messageCarriesLiveWork({ browserConsoleAnchors: [{ taskId: 't', offset: 0 }] })).toBe(true)
  })

  it('lets a genuinely empty placeholder go', () => {
    expect(messageCarriesLiveWork({})).toBe(false)
    expect(messageCarriesLiveWork({ activityLog: [], alphaTerminalLog: null })).toBe(false)
    expect(messageCarriesLiveWork(null)).toBe(false)
  })
})

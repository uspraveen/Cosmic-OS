import { describe, expect, it } from 'vitest'
import { mergeBrowserRunProgress, normalizeBrowserTrail } from './browserRunTrail'

interface Reading {
  step?: number | null
  description?: string
  screenshot?: { previewUrl: string } | null
  trail?: { step: number | null; text: string }[]
}

const reading = (step: number, description: string, screenshot?: string): Reading => ({
  step,
  description,
  screenshot: screenshot ? { previewUrl: screenshot } : null,
})

const trailOf = (value: Reading | undefined) =>
  (value?.trail || []).map((entry) => [entry.step, entry.text])

describe('mergeBrowserRunProgress', () => {
  it('has nothing to remember on the first reading', () => {
    const merged = mergeBrowserRunProgress<Reading>(undefined, reading(1, 'Opened example.com'))
    expect(merged?.description).toBe('Opened example.com')
    expect(merged?.trail).toBeUndefined()
  })

  it('turns the step that just finished into history', () => {
    let state = mergeBrowserRunProgress<Reading>(undefined, reading(1, 'Opened example.com'))
    state = mergeBrowserRunProgress(state, reading(2, 'Typed into the search box'))
    state = mergeBrowserRunProgress(state, reading(3, 'Pressed Enter'))
    expect(state?.description).toBe('Pressed Enter')
    expect(trailOf(state)).toEqual([
      [1, 'Opened example.com'],
      [2, 'Typed into the search box'],
    ])
  })

  it('does not append twice when the same reading is replayed', () => {
    // The background-task path upserts a reading and then patches with the
    // very same one; a history refresh can re-deliver it again later.
    const first = mergeBrowserRunProgress<Reading>(undefined, reading(1, 'Opened example.com'))
    const second = mergeBrowserRunProgress(first, reading(2, 'Clicked Sign in'))
    const replayed = mergeBrowserRunProgress(second, reading(2, 'Clicked Sign in'))
    const replayedAgain = mergeBrowserRunProgress(replayed, reading(2, 'Clicked Sign in'))
    expect(trailOf(replayedAgain)).toEqual([[1, 'Opened example.com']])
  })

  it('records a repeated description when the step number moved on', () => {
    let state = mergeBrowserRunProgress<Reading>(undefined, reading(4, 'Scrolled down'))
    state = mergeBrowserRunProgress(state, reading(5, 'Scrolled down'))
    state = mergeBrowserRunProgress(state, reading(6, 'Read the results'))
    expect(trailOf(state)).toEqual([
      [4, 'Scrolled down'],
      [5, 'Scrolled down'],
    ])
  })

  it('keeps the last screenshot when a step reports none', () => {
    const first = mergeBrowserRunProgress<Reading>(undefined, reading(1, 'Opened example.com', 'shot-1.png'))
    const second = mergeBrowserRunProgress(first, reading(2, 'Clicked Sign in'))
    expect(second?.screenshot).toEqual({ previewUrl: 'shot-1.png' })
    const third = mergeBrowserRunProgress(second, reading(3, 'Filled the form', 'shot-3.png'))
    expect(third?.screenshot).toEqual({ previewUrl: 'shot-3.png' })
  })

  it('trusts a trail that already came back from a mirror', () => {
    const previous = mergeBrowserRunProgress<Reading>(undefined, reading(1, 'Opened example.com'))
    const mirrored: Reading = { ...reading(9, 'Finished'), trail: [{ step: 8, text: 'Extracted the table' }] }
    expect(trailOf(mergeBrowserRunProgress(previous, mirrored))).toEqual([[8, 'Extracted the table']])
  })

  it('caps the trail instead of growing without bound', () => {
    let state: Reading | undefined
    for (let step = 1; step <= 20; step += 1) {
      state = mergeBrowserRunProgress(state, reading(step, `Step ${step}`))
    }
    expect(state?.trail).toHaveLength(6)
    // Step 20 is the one still running, so history ends at 19.
    expect(trailOf(state)).toEqual([
      [14, 'Step 14'], [15, 'Step 15'], [16, 'Step 16'],
      [17, 'Step 17'], [18, 'Step 18'], [19, 'Step 19'],
    ])
  })

  it('keeps whichever reading exists when the other is missing', () => {
    const existing = reading(2, 'Clicked Sign in')
    expect(mergeBrowserRunProgress<Reading>(existing, undefined)).toBe(existing)
    expect(mergeBrowserRunProgress<Reading>(undefined, undefined)).toBeUndefined()
  })
})

describe('normalizeBrowserTrail', () => {
  it('drops entries with no text and keeps a missing step as null', () => {
    expect(normalizeBrowserTrail([
      { step: 1, text: 'Opened example.com' },
      { step: 2, text: '   ' },
      { text: 'Clicked Sign in' },
      'nonsense',
    ])).toEqual([
      { step: 1, text: 'Opened example.com' },
      { step: null, text: 'Clicked Sign in' },
    ])
  })

  it('returns undefined for anything that is not a populated array', () => {
    expect(normalizeBrowserTrail(undefined)).toBeUndefined()
    expect(normalizeBrowserTrail([])).toBeUndefined()
    expect(normalizeBrowserTrail([{ text: '' }])).toBeUndefined()
    expect(normalizeBrowserTrail('trail')).toBeUndefined()
  })
})

describe('mergeBrowserRunProgress: run identity', () => {
  it('carries the task id through a patch that omits it', () => {
    // Pause, resume and takeover input are all addressed at the task id. A
    // partial progress patch must not leave the card showing a run it can no
    // longer talk to.
    const merged = mergeBrowserRunProgress(
      { taskId: 'task-1', description: 'Opened the page' },
      { description: 'Clicked apply' },
    )
    expect(merged?.taskId).toBe('task-1')
  })

  it('lets a new run replace the id', () => {
    const merged = mergeBrowserRunProgress(
      { taskId: 'task-1', description: 'a' },
      { taskId: 'task-2', description: 'b' },
    )
    expect(merged?.taskId).toBe('task-2')
  })
})

import { describe, expect, it } from 'vitest'
import { createBrowserLiveFrameStore } from './browserLiveFrameStore'

describe('browser live frame store', () => {
  it('coalesces frames to one subscriber update and tracks identical heartbeats', () => {
    const paints: (() => void)[] = []
    let time = 1000
    const store = createBrowserLiveFrameStore((callback) => { paints.push(callback) }, () => time)
    const seen: string[] = []
    store.subscribe('browser-1', () => seen.push(store.getSnapshot('browser-1').frame))
    store.offer('browser-1', 'request-1', 'first')
    time = 1100
    store.offer('browser-1', 'request-1', 'newest')
    expect(paints).toHaveLength(1)
    paints.shift()!()
    expect(seen).toEqual(['newest'])
    expect(store.getSnapshot('request-1').frame).toBe('newest')
    time = 3200
    store.offer('browser-1', 'request-1', 'newest')
    paints.shift()!()
    expect(store.getSnapshot('browser-1')).toEqual({ frame: 'newest', changedAt: 1100, receivedAt: 3200 })
  })

  it('keeps a finished run at its last live frame and refuses late frames', () => {
    const store = createBrowserLiveFrameStore((callback) => { callback() }, () => 1000)
    store.offer('browser-1', 'request-1', 'live')
    store.finish('browser-1')
    store.offer('browser-1', 'request-1', 'late')
    expect(store.getSnapshot('browser-1').frame).toBe('live')
    expect(store.getSnapshot('request-1').frame).toBe('live')
  })

  it('allows a newer browser run to replace its parent request alias', () => {
    const store = createBrowserLiveFrameStore((callback) => { callback() }, () => 1000)
    store.offer('browser-1', 'request-1', 'old')
    store.finish('browser-1')
    store.offer('browser-2', 'request-1', 'new')
    expect(store.getSnapshot('browser-1').frame).toBe('old')
    expect(store.getSnapshot('request-1').frame).toBe('new')
    expect(store.getSnapshot('browser-2').frame).toBe('new')
  })

  it('keeps the last frame when a finished run has no final screenshot', () => {
    const store = createBrowserLiveFrameStore((callback) => { callback() }, () => 1000)
    store.offer('browser-1', 'request-1', 'last live frame')
    store.finish('browser-1')
    store.offer('browser-1', 'request-1', 'late frame')
    expect(store.getSnapshot('browser-1').frame).toBe('last live frame')
  })
})

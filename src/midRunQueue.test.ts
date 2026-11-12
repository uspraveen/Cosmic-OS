import { describe, expect, it } from 'vitest'
import {
  addLocalMidRunItem,
  applyMidRunAck,
  applyMidRunSnapshot,
  describeMidRunItem,
  findMidRunItem,
  midRunModeForKey,
  patchMidRunItem,
  removeMidRunItem,
} from './midRunQueue'

const server = (id: string, state = 'pending_steer', extra: Record<string, unknown> = {}) => ({
  request_id: id,
  content: `text ${id}`,
  mode: 'steer',
  state,
  created_at: '2026-11-11T00:00:00Z',
  attachment_count: 0,
  ...extra,
})

describe('mid-run queue tray', () => {
  it('adds an optimistic local item once', () => {
    let items = addLocalMidRunItem([], { requestId: 'r1', content: 'hi', mode: 'steer' })
    items = addLocalMidRunItem(items, { requestId: 'r1', content: 'hi', mode: 'steer' })
    expect(items).toHaveLength(1)
    expect(items[0]).toMatchObject({ state: 'sending', serverSeen: false })
  })

  it('acks upgrade the local item to the server state', () => {
    let items = addLocalMidRunItem([], { requestId: 'r1', content: 'hi', mode: 'steer' })
    items = applyMidRunAck(items, { type: 'user_message.queued', item: server('r1', 'steering') })
    expect(items[0]).toMatchObject({ state: 'steering', serverSeen: true, content: 'text r1' })
  })

  it('keeps unacked local items when a snapshot races ahead of the ack', () => {
    let items = addLocalMidRunItem([], { requestId: 'r2', content: 'late', mode: 'queue' })
    items = applyMidRunSnapshot(items, [server('r1')])
    expect(items.map((item) => item.requestId)).toEqual(['r1', 'r2'])
    expect(items[1].state).toBe('sending')
  })

  it('drops items that left the server queue (delivered, dispatched, withdrawn)', () => {
    let items = applyMidRunSnapshot([], [server('r1'), server('r2', 'queued')])
    items = applyMidRunSnapshot(items, [server('r2', 'queued')])
    expect(items.map((item) => item.requestId)).toEqual(['r2'])
  })

  it('keeps a starting item until its route_result, except on resume', () => {
    let items = applyMidRunSnapshot([], [server('r1')])
    items = patchMidRunItem(items, 'r1', { state: 'starting' })
    expect(applyMidRunSnapshot(items, []).map((item) => item.requestId)).toEqual(['r1'])
    expect(applyMidRunSnapshot(items, [], { keepStarting: false })).toEqual([])
  })

  it('follows server order and ignores malformed snapshot entries', () => {
    const items = applyMidRunSnapshot([], [server('b'), null, { nope: true }, server('a')])
    expect(items.map((item) => item.requestId)).toEqual(['b', 'a'])
    expect(applyMidRunSnapshot(items, 'not-a-list')).toBe(items)
  })

  it('patches, finds and removes', () => {
    let items = applyMidRunSnapshot([], [server('r1')])
    items = patchMidRunItem(items, 'r1', { withdrawing: true })
    expect(findMidRunItem(items, ' r1 ')?.withdrawing).toBe(true)
    expect(findMidRunItem(items, 'missing')).toBeNull()
    expect(removeMidRunItem(items, 'r1')).toEqual([])
    expect(removeMidRunItem(items, 'missing')).toBe(items)
  })

  it('describes every state', () => {
    const base = applyMidRunSnapshot([], [server('r1')])[0]
    expect(describeMidRunItem({ ...base, state: 'steering' }).label).toBe('Next step')
    expect(describeMidRunItem({ ...base, state: 'queued' }).label).toBe('Queued')
    expect(describeMidRunItem({ ...base, state: 'pending_steer' }).label).toBe('Waiting')
    expect(describeMidRunItem({ ...base, state: 'sending' }).label).toBe('Sending…')
    expect(describeMidRunItem({ ...base, state: 'starting' }).label).toBe('Starting')
    expect(describeMidRunItem({ ...base, state: 'failed', error: 'offline' }).hint).toBe('offline')
    expect(describeMidRunItem({ ...base, withdrawing: true }).label).toBe('Removing…')
  })

  it('maps keys to delivery modes only while busy with content', () => {
    expect(midRunModeForKey('Enter', { busy: true, hasContent: true })).toBe('steer')
    expect(midRunModeForKey('Tab', { busy: true, hasContent: true })).toBe('queue')
    expect(midRunModeForKey('Enter', { busy: true, hasContent: true, shiftKey: true })).toBeNull()
    expect(midRunModeForKey('Tab', { busy: false, hasContent: true })).toBeNull()
    expect(midRunModeForKey('Tab', { busy: true, hasContent: false })).toBeNull()
  })
})

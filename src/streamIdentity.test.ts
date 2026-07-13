import { describe, expect, it } from 'vitest'
import {
  canClaimActiveStreamSlot,
  isEventForActiveStream,
  shouldBindToLastAssistantMessage,
} from './streamIdentity'

describe('isEventForActiveStream', () => {
  it('matches when the event request id equals the active request id', () => {
    expect(isEventForActiveStream({ request_id: 'req_1' }, 'req_1', null)).toBe(true)
  })

  it('matches when the event task id equals the active task id', () => {
    expect(isEventForActiveStream({ task_id: 'task_1' }, null, 'task_1')).toBe(true)
  })

  it('rejects an autonomous event with an unrelated request id while a stream is active', () => {
    // This is the exact shape of a heartbeat/Gmail single-shot delivery
    // arriving while a document-editing task is still mid-stream.
    expect(isEventForActiveStream({ request_id: 'heartbeat_req_42' }, 'req_document_task', null)).toBe(false)
  })

  it('rejects an autonomous event with an unrelated task id while a stream is active', () => {
    expect(isEventForActiveStream({ task_id: 'cron_task_99' }, null, 'task_document_task')).toBe(false)
  })

  it('treats an identity-less event as belonging to the active stream (legacy safety net)', () => {
    expect(isEventForActiveStream({}, 'req_document_task', null)).toBe(true)
  })

  it('treats any identified event as active when nothing is currently tracked', () => {
    expect(isEventForActiveStream({ request_id: 'req_anything' }, null, null)).toBe(true)
  })
})

describe('shouldBindToLastAssistantMessage', () => {
  it('binds identity-less events to the last assistant bubble', () => {
    expect(shouldBindToLastAssistantMessage({}, 'req_document_task', null)).toBe(true)
  })

  it('binds an event that matches the active stream to the last assistant bubble', () => {
    expect(shouldBindToLastAssistantMessage({ request_id: 'req_document_task' }, 'req_document_task', null)).toBe(true)
  })

  it('does NOT bind an unrelated autonomous delivery to the last assistant bubble', () => {
    // Regression test for: an incoming Gmail/heartbeat reply must never get
    // spliced into an unrelated, still-streaming task's answer.
    expect(
      shouldBindToLastAssistantMessage({ request_id: 'gmail_surface_req_7' }, 'req_document_task', null),
    ).toBe(false)
  })
})

describe('canClaimActiveStreamSlot', () => {
  it('allows claiming an empty slot', () => {
    expect(canClaimActiveStreamSlot('req_new', null)).toBe(true)
  })

  it('allows re-claiming the same id (restart of the tracked stream)', () => {
    expect(canClaimActiveStreamSlot('req_same', 'req_same')).toBe(true)
  })

  it('refuses to steal the slot from a different in-flight stream', () => {
    expect(canClaimActiveStreamSlot('req_new_autonomous', 'req_document_task')).toBe(false)
  })
})

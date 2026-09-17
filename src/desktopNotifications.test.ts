import { describe, expect, it } from 'vitest'
import {
  buildProphetNoticeFromGateway,
  classifyAssistantNoticeKind,
  isChatInactiveForNotices,
  isProphetCronSource,
  noticeSurvivesActiveChat,
  pendingProphetIds,
  previewNoticeContent,
  shouldDisplayNotice,
  shouldSurfacePendingProphet,
} from './desktopNotifications'

describe('desktop notice surface', () => {
  it('treats hidden Cosmic as inactive, matching Ctrl+Shift+Space', () => {
    expect(isChatInactiveForNotices({
      searchState: 'hidden',
      mode: 'chat',
      showLauncherTray: false,
    })).toBe(true)
  })

  it('treats visible chat as active', () => {
    expect(isChatInactiveForNotices({
      searchState: 'visible',
      mode: 'chat',
      showLauncherTray: false,
    })).toBe(false)
  })

  it('keeps notices up over Spaces, meeting, or the launcher', () => {
    expect(isChatInactiveForNotices({
      searchState: 'visible',
      mode: 'spaces',
      showLauncherTray: false,
    })).toBe(true)
    expect(isChatInactiveForNotices({
      searchState: 'visible',
      mode: 'chat',
      showLauncherTray: true,
    })).toBe(true)
  })

  it('lets Prophet stay on screen while chat is active, and hides the rest', () => {
    expect(noticeSurvivesActiveChat('prophet')).toBe(true)
    expect(noticeSurvivesActiveChat('response')).toBe(false)
    expect(shouldDisplayNotice('prophet', false)).toBe(true)
    expect(shouldDisplayNotice('response', false)).toBe(false)
    expect(shouldDisplayNotice('response', true)).toBe(true)
  })
})

describe('assistant notice classification', () => {
  it('does not turn Daily Prophet cron completions into a chat card', () => {
    expect(isProphetCronSource('cron', 'prophet.morning')).toBe(true)
    expect(classifyAssistantNoticeKind('cron', 'prophet.evening')).toBe(null)
  })

  it('classifies heartbeat, cron, and ordinary replies', () => {
    expect(classifyAssistantNoticeKind('heartbeat', 'heartbeat:daily')).toBe('heartbeat')
    expect(classifyAssistantNoticeKind('cron', 'cron_todo')).toBe('cron')
    expect(classifyAssistantNoticeKind(null, null)).toBe('response')
    expect(classifyAssistantNoticeKind('orchestrator', 'task_1')).toBe('response')
  })
})

describe('prophet gateway catch-up', () => {
  it('builds a stable notice from a pending row', () => {
    const notice = buildProphetNoticeFromGateway({
      notification_id: 'pnote_abc',
      slot: 'evening',
      story_count: 12,
      headline: 'Chip stocks just had their worst day',
      created_at: '2026-09-16T23:05:00Z',
    })
    expect(notice).toEqual({
      id: 'prophet_edition_pnote_abc',
      kind: 'prophet',
      content: 'Evening edition ready · 12 stories · Chip stocks just had their worst day',
      createdAt: '2026-09-16T23:05:00Z',
      prophetNotificationId: 'pnote_abc',
    })
  })

  it('honors a local Later snooze until it expires', () => {
    const settled = new Map([
      ['pnote_abc', { state: 'snoozed' as const, snoozedUntil: '2026-09-16T22:00:00.000Z' }],
    ])
    expect(shouldSurfacePendingProphet('pnote_abc', settled, Date.parse('2026-09-16T21:00:00.000Z'))).toBe(false)
    expect(shouldSurfacePendingProphet('pnote_abc', settled, Date.parse('2026-09-16T22:01:00.000Z'))).toBe(true)
    expect(settled.has('pnote_abc')).toBe(false)
  })

  it('does not resurface a paper opened on this device', () => {
    const settled = new Map([
      ['pnote_abc', { state: 'opened' as const }],
    ])
    expect(shouldSurfacePendingProphet('pnote_abc', settled, Date.now())).toBe(false)
  })

  it('collects pending ids for cross-device dismiss', () => {
    expect(pendingProphetIds([
      { notification_id: 'a' },
      { notificationId: 'b' },
      {},
    ])).toEqual(new Set(['a', 'b']))
  })
})

describe('previewNoticeContent', () => {
  it('collapses whitespace and truncates', () => {
    expect(previewNoticeContent('  hello   world  ')).toBe('hello world')
    expect(previewNoticeContent('abcdefghij', 8)).toBe('abcdefg…')
  })
})

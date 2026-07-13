import { describe, expect, it } from 'vitest'
import {
  getCodexAuthAttentionItems,
  getCursorAuthAttentionItems,
  mergeAuthAttentionItems,
} from './authAttention'

describe('getCodexAuthAttentionItems', () => {
  it('returns nothing when Codex is authenticated', () => {
    expect(getCodexAuthAttentionItems({ status: 'authenticated' })).toEqual([])
  })

  it('surfaces relogin_required for island/settings attention', () => {
    const items = getCodexAuthAttentionItems({
      status: 'relogin_required',
      login_required_reason: 'api_key_relogin_required',
      auth_mode: 'api_key',
    })
    expect(items).toHaveLength(1)
    expect(items[0]?.key).toBe('codex:alpha')
    expect(items[0]?.settingsView).toBe('agents-codex')
    expect(items[0]?.detail).toContain('API key')
  })
})

describe('getCursorAuthAttentionItems', () => {
  it('returns nothing when Cursor is authenticated', () => {
    expect(getCursorAuthAttentionItems({ status: 'authenticated' })).toEqual([])
  })

  it('surfaces login_required for island/settings attention', () => {
    const items = getCursorAuthAttentionItems({
      status: 'login_required',
      login_required_reason: 'cursor_oauth_login_required',
    })
    expect(items).toHaveLength(1)
    expect(items[0]?.key).toBe('cursor:alpha')
    expect(items[0]?.settingsView).toBe('agents-cursor')
  })
})

describe('mergeAuthAttentionItems', () => {
  it('deduplicates by key and preserves order across providers', () => {
    const merged = mergeAuthAttentionItems(
      getCodexAuthAttentionItems({ status: 'relogin_required' }),
      getCursorAuthAttentionItems({ status: 'login_required' }),
      getCodexAuthAttentionItems({ status: 'relogin_required' }),
    )
    expect(merged.map((item) => item.key)).toEqual(['codex:alpha', 'cursor:alpha'])
  })
})

import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  consumeVaultEditorRestoreOnShow,
  getVaultEditorDraft,
  isVaultEditorVisible,
  noteVaultEditorDismissed,
  noteVaultEditorHide,
  resetVaultEditorSession,
  setVaultEditorDraft,
  setVaultEditorVisible,
} from './vaultEditorSession'

const SAMPLE_DRAFT = {
  entryId: null,
  title: 'GitHub',
  siteUrl: 'https://github.com',
  username: 'you@example.com',
  password: '',
  totpSeed: '',
  notes: '',
}

afterEach(() => {
  resetVaultEditorSession()
  vi.useRealTimers()
})

describe('vaultEditorSession', () => {
  it('does not restore on show when the editor was not on screen at hide', () => {
    setVaultEditorDraft(SAMPLE_DRAFT)
    setVaultEditorVisible(false)
    noteVaultEditorHide()
    expect(consumeVaultEditorRestoreOnShow()).toBe(false)
    expect(getVaultEditorDraft()).toEqual(SAMPLE_DRAFT)
  })

  it('restores on show when hide happened with the add/edit form open', () => {
    setVaultEditorDraft(SAMPLE_DRAFT)
    setVaultEditorVisible(true)
    noteVaultEditorHide()
    expect(isVaultEditorVisible()).toBe(true)
    expect(consumeVaultEditorRestoreOnShow()).toBe(true)
    expect(consumeVaultEditorRestoreOnShow()).toBe(false)
    expect(getVaultEditorDraft()?.username).toBe('you@example.com')
  })

  it('still restores after the editor unmounts during hide', () => {
    setVaultEditorDraft(SAMPLE_DRAFT)
    setVaultEditorVisible(true)
    noteVaultEditorHide()
    setVaultEditorVisible(false)
    expect(consumeVaultEditorRestoreOnShow()).toBe(true)
    expect(getVaultEditorDraft()?.username).toBe('you@example.com')
  })

  it('clears restore when the user cancels or saves', () => {
    setVaultEditorDraft(SAMPLE_DRAFT)
    setVaultEditorVisible(true)
    noteVaultEditorHide()
    setVaultEditorDraft(null)
    expect(consumeVaultEditorRestoreOnShow()).toBe(false)
    expect(getVaultEditorDraft()).toBeNull()
  })

  it('restores when Esc closed the form just before Cosmic hid', () => {
    setVaultEditorDraft(SAMPLE_DRAFT)
    setVaultEditorVisible(true)
    noteVaultEditorDismissed()
    setVaultEditorVisible(false)
    noteVaultEditorHide()
    expect(consumeVaultEditorRestoreOnShow()).toBe(true)
    expect(getVaultEditorDraft()?.username).toBe('you@example.com')
  })

  it('does not restore from a stale settings close after the user stayed in Cosmic', () => {
    vi.useFakeTimers()
    setVaultEditorDraft(SAMPLE_DRAFT)
    setVaultEditorVisible(true)
    noteVaultEditorDismissed()
    setVaultEditorVisible(false)
    vi.advanceTimersByTime(1500)
    noteVaultEditorHide()
    expect(consumeVaultEditorRestoreOnShow()).toBe(false)
    expect(getVaultEditorDraft()?.username).toBe('you@example.com')
    vi.useRealTimers()
  })
})

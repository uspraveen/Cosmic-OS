/** In-memory only. Never written to disk — this holds unsaved vault form fields
 *  (including a password) while the user copies values from another app. */
export type VaultEditorDraft = {
  entryId: string | null
  title: string
  siteUrl: string
  username: string
  password: string
  totpSeed: string
  notes: string
}

let draft: VaultEditorDraft | null = null
let editorVisible = false
let restoreOnShow = false
/** Escape closes Settings before Cosmic hide IPC arrives. Keep a short window. */
let pendingRestoreUntil = 0

const ESCAPE_HIDE_RESTORE_MS = 1000

export function getVaultEditorDraft(): VaultEditorDraft | null {
  return draft
}

export function setVaultEditorDraft(next: VaultEditorDraft | null) {
  draft = next
  if (!next) {
    editorVisible = false
    restoreOnShow = false
    pendingRestoreUntil = 0
  }
}

export function setVaultEditorVisible(visible: boolean) {
  editorVisible = visible
}

export function isVaultEditorVisible(): boolean {
  return editorVisible && draft !== null
}

/** Settings closed (Esc / overlay) while the add/edit form was on screen. */
export function noteVaultEditorDismissed() {
  if (editorVisible && draft !== null) {
    pendingRestoreUntil = Date.now() + ESCAPE_HIDE_RESTORE_MS
  }
}

/** Call when Cosmic is hiding. Restores the live form, or one Esc just closed. */
export function noteVaultEditorHide() {
  if (editorVisible && draft !== null) {
    restoreOnShow = true
    pendingRestoreUntil = 0
    return
  }
  restoreOnShow = draft !== null && pendingRestoreUntil > Date.now()
  pendingRestoreUntil = 0
}

export function consumeVaultEditorRestoreOnShow(): boolean {
  const shouldRestore = restoreOnShow && draft !== null
  restoreOnShow = false
  return shouldRestore
}

export function resetVaultEditorSession() {
  draft = null
  editorVisible = false
  restoreOnShow = false
  pendingRestoreUntil = 0
}

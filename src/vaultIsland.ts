import { normalizeVaultCredentialKind, vaultKindLabel } from './vaultKinds'

export type VaultIslandAction = 'use_entry' | 'add_entry' | 'browser_credential_request'

export type VaultApproveGrant = 'once' | 'window' | 'always'

export const VAULT_ALLOW_WINDOW_SECONDS = 24 * 60 * 60

export type VaultIslandRequest = {
  requestId: string
  action: VaultIslandAction
  title: string
  siteDomain: string
  username: string
  purpose: string
  credentialKind: string
  summary: string
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : null
}

function text(value: unknown): string {
  return String(value || '').trim()
}

function islandAction(value: unknown): VaultIslandAction | null {
  const action = text(value) || 'use_entry'
  if (action === 'add_entry' || action === 'use_entry' || action === 'browser_credential_request') return action
  return null
}

function fromFields(fields: Record<string, unknown>, fallbacks: Record<string, unknown> = {}): VaultIslandRequest | null {
  const requestId = text(fields.request_id || fields.requestId || fallbacks.request_id || fallbacks.requestId)
  const action = islandAction(fields.action || fallbacks.action)
  if (!requestId || !action) return null
  const title = text(fields.title || fallbacks.title)
  const siteDomain = text(fields.site_domain || fields.siteDomain || fallbacks.site_domain || fallbacks.siteDomain)
  const username = text(
    fields.username || fallbacks.username
    || fields.username_hint || fields.usernameHint || fallbacks.username_hint || fallbacks.usernameHint,
  )
  const purpose = text(fields.purpose || fallbacks.purpose)
  const credentialKind = normalizeVaultCredentialKind(
    text(fields.credential_kind || fields.credentialKind || fallbacks.credential_kind || fallbacks.credentialKind) || 'login',
  )
  const summary = text(fields.summary || fallbacks.summary)
  return {
    requestId,
    action,
    title: title || siteDomain || 'Saved credential',
    siteDomain,
    username,
    purpose,
    credentialKind,
    summary,
  }
}

export function vaultIslandHeadline(request: VaultIslandRequest): string {
  if (request.action === 'add_entry') return 'Cosmic wants to save a credential'
  if (request.action === 'browser_credential_request') return 'The browser agent needs credentials'
  return 'Cosmic wants to use a saved credential'
}

export function vaultIslandAllowLabel(request: VaultIslandRequest): string {
  if (request.action === 'add_entry') return 'Allow & save'
  if (request.action === 'browser_credential_request') return 'Provide credentials'
  return 'Allow once'
}

export function vaultIslandAllowWindowLabel(): string {
  return 'Allow 24 hours'
}

export function vaultIslandAlwaysAllowLabel(): string {
  return 'Always allow'
}

export function vaultIslandKindLabel(request: VaultIslandRequest): string {
  return vaultKindLabel(request.credentialKind)
}

export function vaultIslandFromPending(item: unknown): VaultIslandRequest | null {
  const row = asRecord(item)
  if (!row) return null
  const status = text(row.status).toLowerCase()
  if (status && status !== 'pending') return null
  const payload = asRecord(row.payload) || {}
  return fromFields(
    {
      request_id: row.request_id,
      action: row.action,
      purpose: row.purpose,
      title: payload.title,
      site_domain: payload.site_domain,
      username: payload.username,
      username_hint: payload.username_hint,
      credential_kind: payload.credential_kind,
    },
    row,
  )
}

export function parseVaultIslandOpen(event: unknown): VaultIslandRequest | null {
  const rec = asRecord(event)
  if (!rec) return null
  const type = text(rec.type)
  if (type === 'vault.notification') {
    return fromFields(rec)
  }
  if (type === 'response.action.updated') {
    const block = asRecord(rec.response_block)
    const blockType = text(rec.block_type || block?.type)
    if (blockType !== 'vault_permission_request' && blockType !== 'browser_credential_request') return null
    const status = text(rec.status || block?.status).toLowerCase()
    if (status && status !== 'pending') return null
    if (!block) return null
    return fromFields(block, rec)
  }
  return null
}

export function isVaultIslandResolved(event: unknown, requestId: string): boolean {
  if (!requestId) return false
  const rec = asRecord(event)
  if (!rec) return false
  const type = text(rec.type)
  const block = asRecord(rec.response_block)
  const eventRequestId = text(
    rec.request_id || rec.requestId || rec.approval_id || block?.request_id || block?.requestId,
  )
  if (eventRequestId !== requestId) return false
  if (type === 'response.action.updated') {
    const blockType = text(rec.block_type || block?.type)
    if (blockType && blockType !== 'vault_permission_request' && blockType !== 'browser_credential_request') return false
    const status = text(rec.status || block?.status).toLowerCase()
    return Boolean(status) && status !== 'pending'
  }
  return false
}

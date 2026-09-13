import { describe, expect, it } from 'vitest'
import {
  isVaultIslandResolved,
  parseVaultIslandOpen,
  vaultIslandAllowLabel,
  vaultIslandAllowWindowLabel,
  vaultIslandFromPending,
  vaultIslandHeadline,
} from './vaultIsland'

describe('vaultIsland', () => {
  it('opens an island card from a vault.notification', () => {
    const request = parseVaultIslandOpen({
      type: 'vault.notification',
      request_id: 'req_1',
      action: 'use_entry',
      title: 'HuggingFace',
      credential_kind: 'api_key',
      purpose: 'Push updated README',
      summary: 'Cosmic wants to use your saved API key for HuggingFace',
    })
    expect(request?.requestId).toBe('req_1')
    expect(request?.title).toBe('HuggingFace')
    expect(request?.credentialKind).toBe('api_key')
    expect(vaultIslandHeadline(request!)).toBe('Cosmic wants to use a saved credential')
    expect(vaultIslandAllowLabel(request!)).toBe('Allow once')
    expect(vaultIslandAllowWindowLabel()).toBe('Allow 15 min')
  })

  it('ignores browser credential requests that need a form', () => {
    expect(parseVaultIslandOpen({
      type: 'vault.notification',
      request_id: 'req_2',
      action: 'browser_credential_request',
      title: 'github.com',
    })).toBeNull()
  })

  it('opens from a pending vault permission block and closes when it resolves', () => {
    const opened = parseVaultIslandOpen({
      type: 'response.action.updated',
      block_type: 'vault_permission_request',
      status: 'pending',
      response_block: {
        type: 'vault_permission_request',
        request_id: 'req_3',
        action: 'add_entry',
        title: 'OpenAI',
        status: 'pending',
      },
    })
    expect(opened?.action).toBe('add_entry')
    expect(vaultIslandAllowLabel(opened!)).toBe('Allow & save')
    expect(isVaultIslandResolved({
      type: 'response.action.updated',
      block_type: 'vault_permission_request',
      status: 'approved',
      response_block: { request_id: 'req_3', status: 'approved' },
    }, 'req_3')).toBe(true)
    expect(isVaultIslandResolved({
      type: 'response.action.updated',
      status: 'approved',
      response_block: { request_id: 'other', status: 'approved' },
    }, 'req_3')).toBe(false)
  })

  it('maps a settings pending row', () => {
    const request = vaultIslandFromPending({
      request_id: 'req_4',
      action: 'use_entry',
      status: 'pending',
      purpose: 'Sign in',
      payload: { title: 'GitHub', site_domain: 'github.com', username: 'you', credential_kind: 'login' },
    })
    expect(request).toMatchObject({
      requestId: 'req_4',
      title: 'GitHub',
      siteDomain: 'github.com',
      username: 'you',
    })
    expect(vaultIslandFromPending({ request_id: 'req_4', action: 'use_entry', status: 'approved' })).toBeNull()
  })
})

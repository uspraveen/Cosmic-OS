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

  it('opens browser credential requests with a provide-credentials action', () => {
    const request = parseVaultIslandOpen({
      type: 'vault.notification',
      request_id: 'req_2',
      action: 'browser_credential_request',
      title: 'github.com',
      purpose: 'Log in to download invoices',
    })
    expect(request?.requestId).toBe('req_2')
    expect(request?.action).toBe('browser_credential_request')
    expect(vaultIslandHeadline(request!)).toBe('The browser agent needs credentials')
    expect(vaultIslandAllowLabel(request!)).toBe('Provide credentials')
  })

  it('maps a browser credential pending row with the username hint', () => {
    const request = vaultIslandFromPending({
      request_id: 'req_2b',
      action: 'browser_credential_request',
      status: 'pending',
      purpose: 'Sign in with Google on Appollo',
      payload: { title: 'accounts.google.com', site_domain: 'accounts.google.com', username_hint: 'you@gmail.com' },
    })
    expect(request).toMatchObject({
      requestId: 'req_2b',
      action: 'browser_credential_request',
      title: 'accounts.google.com',
      username: 'you@gmail.com',
    })
  })

  it('resolves browser credential requests from block updates', () => {
    expect(isVaultIslandResolved({
      type: 'response.action.updated',
      block_type: 'browser_credential_request',
      status: 'approved',
      response_block: { request_id: 'req_2', status: 'approved' },
    }, 'req_2')).toBe(true)
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

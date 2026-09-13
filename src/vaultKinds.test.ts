import { describe, expect, it } from 'vitest'
import {
  formatVaultExpiry,
  normalizeVaultCredentialKind,
  vaultExpiryIsPast,
  vaultKindLabel,
} from './vaultKinds'

describe('vaultKinds', () => {
  it('normalizes aliases to the three stored kinds', () => {
    expect(normalizeVaultCredentialKind('API-key')).toBe('api_key')
    expect(normalizeVaultCredentialKind('bearer')).toBe('token')
    expect(normalizeVaultCredentialKind('password')).toBe('login')
    expect(vaultKindLabel('api_key')).toBe('API key')
  })

  it('formats and detects expiry dates', () => {
    expect(formatVaultExpiry('2027-03-12')).toMatch(/2027/)
    expect(vaultExpiryIsPast('1999-01-01')).toBe(true)
    expect(vaultExpiryIsPast('2999-01-01')).toBe(false)
    expect(vaultExpiryIsPast('')).toBe(false)
  })
})

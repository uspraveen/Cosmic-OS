export const VAULT_CREDENTIAL_KINDS = ['login', 'api_key', 'token'] as const
export type VaultCredentialKind = (typeof VAULT_CREDENTIAL_KINDS)[number]

export const VAULT_CREDENTIAL_KIND_OPTIONS: Array<{
  id: VaultCredentialKind
  label: string
  hint: string
}> = [
  { id: 'login', label: 'Login', hint: 'Username and password' },
  { id: 'api_key', label: 'API key', hint: 'Secret key for an API' },
  { id: 'token', label: 'Token', hint: 'Bearer or access token' },
]

export function normalizeVaultCredentialKind(value?: string | null): VaultCredentialKind {
  const raw = String(value || '').trim().toLowerCase().replace(/[-\s]/g, '_')
  if (raw === 'api_key' || raw === 'api' || raw === 'apikey') return 'api_key'
  if (raw === 'token' || raw === 'bearer' || raw === 'access_token') return 'token'
  return 'login'
}

export function vaultKindLabel(kind?: string | null): string {
  const match = VAULT_CREDENTIAL_KIND_OPTIONS.find((option) => option.id === normalizeVaultCredentialKind(kind))
  return match?.label || 'Login'
}

export function formatVaultExpiry(value?: string | null): string {
  const day = String(value || '').trim().slice(0, 10)
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return ''
  const parsed = new Date(`${day}T00:00:00`)
  if (Number.isNaN(parsed.getTime())) return day
  return parsed.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' })
}

export function vaultExpiryIsPast(value?: string | null): boolean {
  const day = String(value || '').trim().slice(0, 10)
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return false
  return day < new Date().toISOString().slice(0, 10)
}

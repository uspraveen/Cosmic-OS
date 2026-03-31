export type IntegrationStatus = 'needs_auth' | 'connected' | 'revoked'

export interface IntegrationToolDefinition {
  id: string
  label: string
  platformLabel: string
  description: string
  scopes: string[]
}

export interface IntegrationAccountTool {
  tool_id: string
  tool_name: string
  platform_key: string
  scopes: string[]
  config?: Record<string, unknown>
  created_at?: number
  updated_at?: number
}

export interface IntegrationAccountMetadata {
  provider_account_id?: string
  avatar_url?: string
  hosted_domain?: string
  last_connected_at?: number
  last_disconnected_at?: number
  access_token_expires_at?: number
  has_refresh_token?: boolean
  last_auth_error?: string
  scope_match?: boolean
  [key: string]: unknown
}

export interface IntegrationAccountRecord {
  account_id: string
  provider: string
  platform_key: string
  email: string
  display_name: string
  account_label: string
  status: IntegrationStatus
  is_primary: boolean
  granted_scopes: string[]
  required_scopes: string[]
  selected_tools: string[]
  metadata: IntegrationAccountMetadata
  created_at?: number
  updated_at?: number
  tools: IntegrationAccountTool[]
}

export interface IntegrationProviderSnapshot {
  provider: string
  display_name: string
  metadata: Record<string, unknown>
  accounts: IntegrationAccountRecord[]
  account_count: number
  connected_count: number
  created_at?: number
  updated_at?: number
}

export interface IntegrationsSnapshot {
  providers: IntegrationProviderSnapshot[]
}

export interface IntegrationEvent {
  type: string
  provider: string
  account_id: string
  message: string
  account_label?: string
  email?: string
  display_name?: string
}

export const GOOGLE_BASE_SCOPES = [
  'openid',
  'https://www.googleapis.com/auth/userinfo.profile',
  'https://www.googleapis.com/auth/userinfo.email',
]

export const GOOGLE_TOOL_DEFINITIONS: IntegrationToolDefinition[] = [
  {
    id: 'calendar',
    label: 'Calendar',
    platformLabel: 'Events',
    description: 'Upcoming events, meeting timelines, and schedule context.',
    scopes: [
      'https://www.googleapis.com/auth/calendar',
      'https://www.googleapis.com/auth/calendar.events',
    ],
  },
  {
    id: 'gmail',
    label: 'Gmail',
    platformLabel: 'Mail',
    description: 'Inbox state, triage, and email follow-up actions.',
    scopes: ['https://www.googleapis.com/auth/gmail.modify'],
  },
  {
    id: 'docs',
    label: 'Docs',
    platformLabel: 'Documents',
    description: 'Drafting, editing, and structured document generation.',
    scopes: ['https://www.googleapis.com/auth/documents'],
  },
  {
    id: 'drive',
    label: 'Drive',
    platformLabel: 'Files',
    description: 'Files opened or created by Cosmic inside Google Drive.',
    scopes: ['https://www.googleapis.com/auth/drive.file'],
  },
]

const GOOGLE_TOOL_MAP = new Map(GOOGLE_TOOL_DEFINITIONS.map((tool) => [tool.id, tool]))

function makeDraftId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `draft-${crypto.randomUUID()}`
  }
  return `draft-${Date.now()}`
}

export function dedupeScopes(scopes: string[]) {
  const unique: string[] = []
  for (const scope of scopes) {
    const value = String(scope).trim()
    if (value && !unique.includes(value)) {
      unique.push(value)
    }
  }
  return unique
}

export function getGoogleToolDefinition(toolId: string) {
  return GOOGLE_TOOL_MAP.get(toolId) ?? null
}

export function getGoogleRequiredScopes(toolIds: string[]) {
  const toolScopes = toolIds.flatMap((toolId) => GOOGLE_TOOL_MAP.get(toolId)?.scopes ?? [])
  return dedupeScopes([...GOOGLE_BASE_SCOPES, ...toolScopes])
}

export function normalizeIntegrationTool(raw: Partial<IntegrationAccountTool>): IntegrationAccountTool {
  const toolId = String(raw.tool_id || '').trim()
  const definition = getGoogleToolDefinition(toolId)
  return {
    tool_id: toolId,
    tool_name: String(raw.tool_name || definition?.label || toolId).trim() || toolId,
    platform_key: String(raw.platform_key || 'workspace').trim() || 'workspace',
    scopes: dedupeScopes([...(raw.scopes ?? []), ...(definition?.scopes ?? [])]),
    config: raw.config ?? {},
    created_at: raw.created_at,
    updated_at: raw.updated_at,
  }
}

export function normalizeIntegrationAccount(raw: Partial<IntegrationAccountRecord>): IntegrationAccountRecord {
  const tools = (raw.tools ?? [])
    .map((tool) => normalizeIntegrationTool(tool))
    .filter((tool) => tool.tool_id)
  const selectedTools = raw.selected_tools?.length
    ? Array.from(new Set(raw.selected_tools))
    : tools.map((tool) => tool.tool_id)
  const requiredScopes = dedupeScopes(raw.required_scopes?.length ? raw.required_scopes : getGoogleRequiredScopes(selectedTools))
  const grantedScopes = dedupeScopes(raw.granted_scopes ?? [])

  return {
    account_id: String(raw.account_id || makeDraftId()),
    provider: String(raw.provider || 'google').trim() || 'google',
    platform_key: String(raw.platform_key || 'workspace').trim() || 'workspace',
    email: String(raw.email || '').trim(),
    display_name: String(raw.display_name || '').trim(),
    account_label: String(raw.account_label || raw.display_name || raw.email || 'Google account').trim() || 'Google account',
    status: (raw.status as IntegrationStatus) || 'needs_auth',
    is_primary: Boolean(raw.is_primary),
    granted_scopes: grantedScopes,
    required_scopes: requiredScopes,
    selected_tools: selectedTools,
    metadata: (raw.metadata ?? {}) as IntegrationAccountMetadata,
    created_at: raw.created_at,
    updated_at: raw.updated_at,
    tools,
  }
}

export function createGoogleAccountDraft(existingAccounts: IntegrationAccountRecord[]) {
  const selectedTools = GOOGLE_TOOL_DEFINITIONS.map((tool) => tool.id)
  return normalizeIntegrationAccount({
    account_id: makeDraftId(),
    provider: 'google',
    platform_key: 'workspace',
    status: 'needs_auth',
    is_primary: existingAccounts.length === 0,
    granted_scopes: [],
    required_scopes: getGoogleRequiredScopes(selectedTools),
    selected_tools: selectedTools,
    tools: GOOGLE_TOOL_DEFINITIONS.map((tool) =>
      normalizeIntegrationTool({
        tool_id: tool.id,
        tool_name: tool.label,
        platform_key: 'workspace',
        scopes: tool.scopes,
      }),
    ),
    metadata: {
      has_refresh_token: false,
      scope_match: false,
    },
  })
}

export function toggleGoogleAccountTool(account: IntegrationAccountRecord, toolId: string) {
  const definition = getGoogleToolDefinition(toolId)
  if (!definition) return account

  const hasTool = account.selected_tools.includes(toolId)
  const selectedTools = hasTool
    ? account.selected_tools.filter((id) => id !== toolId)
    : [...account.selected_tools, toolId]

  const nextTools = hasTool
    ? account.tools.filter((tool) => tool.tool_id !== toolId)
    : [
        ...account.tools,
        normalizeIntegrationTool({
          tool_id: definition.id,
          tool_name: definition.label,
          platform_key: 'workspace',
          scopes: definition.scopes,
        }),
      ]

  return normalizeIntegrationAccount({
    ...account,
    selected_tools: selectedTools,
    required_scopes: getGoogleRequiredScopes(selectedTools),
    tools: nextTools,
  })
}

export function statusLabel(status: IntegrationStatus) {
  if (status === 'connected') return 'Connected'
  if (status === 'revoked') return 'Disconnected'
  return 'Needs Auth'
}

export function formatAccountTimestamp(timestamp?: number) {
  if (!timestamp) return ''
  const value = Number(timestamp)
  if (!Number.isFinite(value) || value <= 0) return ''
  return new Date(value * 1000).toLocaleDateString([], {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
}

export function formatRelativeConnection(timestamp?: number) {
  if (!timestamp) return 'Never connected'
  const delta = Date.now() - Number(timestamp) * 1000
  const hours = Math.round(delta / (1000 * 60 * 60))
  if (hours < 1) return 'Connected moments ago'
  if (hours < 24) return `Connected ${hours}h ago`
  const days = Math.round(hours / 24)
  return `Connected ${days}d ago`
}

export function scopesMatch(account: IntegrationAccountRecord) {
  return account.required_scopes.every((scope) => account.granted_scopes.includes(scope))
}

export function accountNeedsReconnect(account: IntegrationAccountRecord) {
  return (
    account.status !== 'connected' ||
    !account.metadata?.has_refresh_token ||
    !scopesMatch(account)
  )
}

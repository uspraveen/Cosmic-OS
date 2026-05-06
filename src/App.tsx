import { ArrowDownToLine, ChevronDown, ChevronRight, Square, Terminal } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'
import LiquidGlass from './LiquidGlass'
import DynamicIsland from './DynamicIsland'
import CosmicLoginModal from './CosmicLoginModal'
import LiquidGlassLoader from './LiquidGlassLoader'
import MeetingMode from './MeetingMode'
import SpacesControlCenter from './SpacesControlCenter'
import cosmicBallLogo from './assets/cosmic-ball-logo-v1.1.png'
import moveToBackgroundIcon from './assets/move-to-background.png'
import bringToForegroundIcon from './assets/bring-to-foreground.png'
import './spotlight.css'

export type SearchPosition = 'bottom' | 'middle'
export type QueryMode = 'chat' | 'task' | 'meeting' | 'spaces'
export type GatewayModelSelection = 'cosmic' | 'haiku' | 'opus' | 'perplexity'
type LauncherTileId = 'chat' | 'meeting' | 'task' | 'spaces'

interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  attachments?: MessageAttachment[]
  producedArtifacts?: ProducedArtifact[]
  responseBlocks?: ResponseBlock[]
  thinking?: string
  activity?: string
  activityLog?: ActivityLogEntry[]
  alphaTerminalLog?: AlphaTerminalEntry[]
  sources?: Array<{ url: string; title?: string; domain?: string } | string>
  stopped?: boolean
  channel?: string | null
  requestId?: string | null
  source?: string | null
  sourceId?: string | null
  createdAt?: string | null
  progress?: DocsProgressState | TabularProgressState
  backgroundState?: 'working' | 'ready' | 'failed'
}

interface PendingTaskInput {
  inputRequestId: string
  taskId: string
  sessionId?: string | null
  agent?: string | null
  channel?: string | null
  question: string
  options: string[]
  status?: string
  timestamp?: string | null
}

interface BackgroundTask {
  requestId: string
  taskId?: string | null
  sessionId?: string | null
  route?: string | null
  userQueryExcerpt: string
  partialContent: string
  partialThinking: string
  backgroundedAt?: string | null
  completed: boolean
  failed?: boolean
  error?: string | null
  activity?: string
  activityLog?: ActivityLogEntry[]
  alphaTerminalLog?: AlphaTerminalEntry[]
  progress?: DocsProgressState | TabularProgressState
  producedArtifacts?: ProducedArtifact[]
  sources?: Array<{ url: string; title?: string; domain?: string } | string>
}

interface CronResultNotification {
  id: string
  requestId?: string | null
  sourceId?: string | null
  sessionId?: string | null
  content: string
  channel?: string | null
  createdAt?: string | null
}

interface ProducedArtifactNotification {
  id: string
  messageId: string
  requestId?: string | null
  sourceId?: string | null
  sessionId?: string | null
  channel?: string | null
  createdAt?: string | null
  artifacts: ProducedArtifact[]
}

interface GatewayStatus {
  state: 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'error'
  connected: boolean
  detail?: string
  sessionId?: string | null
}

interface GatewayForegroundStreamSnapshot {
  requestId?: string | null
  taskId?: string | null
  sessionId?: string | null
  route?: string | null
  messageId?: string | null
  content: string
  thinking?: string
  activity?: string
  activityLog?: ActivityLogEntry[]
  alphaTerminalLog?: AlphaTerminalEntry[]
  progress?: DocsProgressState | TabularProgressState
  producedArtifacts?: ProducedArtifact[]
  responseBlocks?: ResponseBlock[]
  snapshotSeq?: number | null
  sources?: Array<{ url: string; title?: string; domain?: string } | string>
  channel?: string | null
  source?: string | null
  sourceId?: string | null
  awaitingReply?: boolean
  completed: boolean
  failed: boolean
  error?: string | null
  updatedAt?: string | null
}

interface PendingDocumentAttachment {
  filePath: string
  filename: string
  mimeType: string
  sizeBytes: number
}

interface MessageAttachment {
  filePath: string | null
  filename: string
  mimeType: string | null
  sizeBytes: number | null
}

interface ProducedArtifact {
  artifactId: string
  taskId?: string | null
  filename: string
  mimeType: string | null
  sizeBytes: number | null
  kind?: string | null
  createdByAgent?: string | null
  createdAt?: string | null
  downloadable: boolean
}

interface ResponseMarkdownBlock {
  id: string
  type: 'markdown'
  text: string
}

interface ResponseCodeBlock {
  id: string
  type: 'code'
  language?: string | null
  code: string
}

interface ResponseArtifactBlock {
  id: string
  type: 'image_artifact' | 'file_artifact'
  artifactId: string
  filename: string
  mimeType?: string | null
  sizeBytes?: number | null
  kind?: string | null
  downloadable?: boolean
  previewUrl?: string | null
  caption?: string | null
  provenance?: ResponseBlockProvenance
}

interface ResponseBlockProvenance {
  sourceUrl?: string | null
  sourceTitle?: string | null
  sourceDomain?: string | null
  sourceImageUrl?: string | null
  attributionLabel?: string | null
  selectionReason?: string | null
  altText?: string | null
  confidence?: number | null
}

interface ResponseSlotBlock {
  id: string
  type: 'image_slot' | 'chart_slot'
  status?: string | null
  loadingLabel?: string | null
  timeoutMs?: number | null
}

type ResponseBlock =
  | ResponseMarkdownBlock
  | ResponseCodeBlock
  | ResponseArtifactBlock
  | ResponseSlotBlock

interface ActivityLogEntry {
  id: string
  label: string
  detail?: string
  status?: string | null
  stage?: string | null
  kind?: string | null
  createdAt: string
  flowRole?: string | null
  delegatedTaskId?: string | null
  parentDelegatedTaskId?: string | null
  specialistTaskId?: string | null
  agentId?: string | null
  agentLabel?: string | null
  intent?: string | null
  specialistEventType?: string | null
}

interface AlphaTerminalEntry {
  id: string
  taskId?: string | null
  provider?: string | null
  stream?: 'stdout' | 'stderr' | 'system'
  eventType?: string | null
  text: string
  detail?: string | null
  createdAt: string
}

type ActivityLogEntryInput = Partial<ActivityLogEntry> & {
  label: string
}

type ActivityLogEntryLike = ActivityLogEntry | ActivityLogEntryInput

interface DocsProgressState {
  kind: 'docs_parse'
  stage: 'prepare' | 'parse' | 'enhance' | 'ready'
  label: string
  detail?: string
  current: number
  total: number
  percent: number
}

interface TabularProgressState {
  kind: 'tabular_parse'
  stage: string
  label: string
  detail?: string
  current: number
  total: number
  percent: number
}

interface SurfaceLaunchState {
  target: 'chat' | 'meeting' | 'spaces'
  token: number
  composerOffsetX: number
  composerOffsetY: number
  responseOffsetX: number
  responseOffsetY: number
  meetingOffsetX: number
  meetingOffsetY: number
  spacesOffsetX: number
  spacesOffsetY: number
}

type HoverTooltipTone = 'launcher' | 'model' | 'control'

interface HoverTooltipState {
  label: string
  x: number
  y: number
  tone: HoverTooltipTone
}

// Helper to strip "PROMPT:" from legacy database entries
const cleanText = (text: string) => {
  if (!text) return ""
  return text.replace(/^PROMPT:/, '')
}

const normalizeMessageAttachments = (value: unknown): MessageAttachment[] | undefined => {
  if (!Array.isArray(value)) {
    return undefined
  }
  const normalized = value
    .map((item) => {
      if (!item || typeof item !== 'object') {
        return null
      }
      const rawFilePath = typeof (item as any).filePath === 'string'
        ? (item as any).filePath.trim()
        : typeof (item as any).file_path === 'string'
          ? (item as any).file_path.trim()
          : typeof (item as any).path === 'string'
            ? (item as any).path.trim()
          : ''
      const derivedFilename = rawFilePath
        ? rawFilePath.split(/[\\/]/).pop() || ''
        : ''
      const filename = typeof (item as any).filename === 'string'
        ? (item as any).filename.trim()
        : typeof (item as any).file_name === 'string'
          ? (item as any).file_name.trim()
          : derivedFilename
      if (!filename) {
        return null
      }
      const rawSize = Number((item as any).sizeBytes ?? (item as any).size_bytes ?? 0)
      return {
        filePath: rawFilePath || null,
        filename,
        mimeType: typeof (item as any).mimeType === 'string'
          ? (item as any).mimeType.trim()
          : typeof (item as any).mime_type === 'string'
            ? (item as any).mime_type.trim()
            : typeof (item as any).mime === 'string'
              ? (item as any).mime.trim()
            : null,
        sizeBytes: Number.isFinite(rawSize) && rawSize > 0 ? rawSize : null,
      } satisfies MessageAttachment
    })
    .filter((item): item is MessageAttachment => item !== null)
  return normalized.length > 0 ? normalized : undefined
}

const extractMessageAttachments = (metadata: any): MessageAttachment[] | undefined => {
  return normalizeMessageAttachments(metadata?.attachments) ?? normalizeMessageAttachments(metadata?.input_artifacts)
}

const normalizeProducedArtifacts = (value: unknown): ProducedArtifact[] | undefined => {
  if (!Array.isArray(value)) {
    return undefined
  }
  const normalized: ProducedArtifact[] = []
  for (const item of value) {
    if (!item || typeof item !== 'object') {
      continue
    }
    const audience = typeof (item as any).audience === 'string'
      ? (item as any).audience.trim()
      : ''
    if (audience && audience !== 'deliverable') {
      continue
    }
    const artifactId = typeof (item as any).artifact_id === 'string'
      ? (item as any).artifact_id.trim()
      : typeof (item as any).artifactId === 'string'
        ? (item as any).artifactId.trim()
        : ''
    const filename = typeof (item as any).filename === 'string'
      ? (item as any).filename.trim()
      : ''
    if (!artifactId || !filename) {
      continue
    }
    const rawSize = Number((item as any).size_bytes ?? (item as any).sizeBytes ?? 0)
    normalized.push({
      artifactId,
      taskId: typeof (item as any).task_id === 'string'
        ? (item as any).task_id.trim()
        : typeof (item as any).taskId === 'string'
          ? (item as any).taskId.trim()
          : null,
      filename,
      mimeType: typeof (item as any).mime_type === 'string'
        ? (item as any).mime_type.trim()
        : typeof (item as any).mimeType === 'string'
          ? (item as any).mimeType.trim()
          : null,
      sizeBytes: Number.isFinite(rawSize) && rawSize > 0 ? rawSize : null,
      kind: typeof (item as any).kind === 'string' ? (item as any).kind.trim() : null,
      createdByAgent: typeof (item as any).created_by_agent === 'string'
        ? (item as any).created_by_agent.trim()
        : typeof (item as any).createdByAgent === 'string'
          ? (item as any).createdByAgent.trim()
          : null,
      createdAt: typeof (item as any).created_at === 'string'
        ? (item as any).created_at.trim()
        : typeof (item as any).createdAt === 'string'
          ? (item as any).createdAt.trim()
          : null,
      downloadable: (item as any).downloadable !== false,
    })
  }
  return normalized.length > 0 ? normalized : undefined
}

const normalizeResponseBlockProvenance = (value: unknown): ResponseBlockProvenance | undefined => {
  if (!value || typeof value !== 'object') {
    return undefined
  }
  const rawConfidence = Number((value as any).confidence)
  const normalized: ResponseBlockProvenance = {
    sourceUrl: typeof (value as any).source_url === 'string'
      ? (value as any).source_url.trim()
      : typeof (value as any).sourceUrl === 'string'
        ? (value as any).sourceUrl.trim()
        : null,
    sourceTitle: typeof (value as any).source_title === 'string'
      ? (value as any).source_title.trim()
      : typeof (value as any).sourceTitle === 'string'
        ? (value as any).sourceTitle.trim()
        : null,
    sourceDomain: typeof (value as any).source_domain === 'string'
      ? (value as any).source_domain.trim()
      : typeof (value as any).sourceDomain === 'string'
        ? (value as any).sourceDomain.trim()
        : null,
    sourceImageUrl: typeof (value as any).source_image_url === 'string'
      ? (value as any).source_image_url.trim()
      : typeof (value as any).sourceImageUrl === 'string'
        ? (value as any).sourceImageUrl.trim()
        : null,
    attributionLabel: typeof (value as any).attribution_label === 'string'
      ? (value as any).attribution_label.trim()
      : typeof (value as any).attributionLabel === 'string'
        ? (value as any).attributionLabel.trim()
        : null,
    selectionReason: typeof (value as any).selection_reason === 'string'
      ? (value as any).selection_reason.trim()
      : typeof (value as any).selectionReason === 'string'
        ? (value as any).selectionReason.trim()
        : null,
    altText: typeof (value as any).alt_text === 'string'
      ? (value as any).alt_text.trim()
      : typeof (value as any).altText === 'string'
        ? (value as any).altText.trim()
        : null,
    confidence: Number.isFinite(rawConfidence) ? rawConfidence : null,
  }
  return Object.values(normalized).some((item) => item !== null && item !== undefined && item !== '')
    ? normalized
    : undefined
}

const normalizeResponseBlocks = (value: unknown): ResponseBlock[] | undefined => {
  if (!Array.isArray(value)) {
    return undefined
  }
  const normalized: ResponseBlock[] = []
  for (const item of value) {
    if (!item || typeof item !== 'object') {
      continue
    }
    const type = typeof (item as any).type === 'string' ? (item as any).type.trim() : ''
    const id = typeof (item as any).id === 'string' && (item as any).id.trim()
      ? (item as any).id.trim()
      : `block_${crypto.randomUUID()}`
    if (type === 'markdown') {
      const text = String((item as any).text || '')
      if (!text) continue
      normalized.push({ id, type: 'markdown', text })
      continue
    }
    if (type === 'code') {
      const code = String((item as any).code || '')
      if (!code) continue
      normalized.push({
        id,
        type: 'code',
        code,
        language: typeof (item as any).language === 'string' && (item as any).language.trim()
          ? (item as any).language.trim()
          : null,
      })
      continue
    }
    if (type === 'image_slot' || type === 'chart_slot') {
      const rawTimeout = Number((item as any).timeout_ms ?? (item as any).timeoutMs ?? 0)
      normalized.push({
        id,
        type,
        status: typeof (item as any).status === 'string' && (item as any).status.trim()
          ? (item as any).status.trim()
          : null,
        loadingLabel: typeof (item as any).loading_label === 'string'
          ? (item as any).loading_label.trim()
          : typeof (item as any).loadingLabel === 'string'
            ? (item as any).loadingLabel.trim()
            : null,
        timeoutMs: Number.isFinite(rawTimeout) && rawTimeout > 0 ? rawTimeout : null,
      })
      continue
    }
    if (type === 'image_artifact' || type === 'file_artifact') {
      const artifactId = typeof (item as any).artifact_id === 'string' && (item as any).artifact_id.trim()
        ? (item as any).artifact_id.trim()
        : typeof (item as any).artifactId === 'string' && (item as any).artifactId.trim()
          ? (item as any).artifactId.trim()
          : ''
      const filename = typeof (item as any).filename === 'string' && (item as any).filename.trim()
        ? (item as any).filename.trim()
        : ''
      if (!artifactId || !filename) continue
      const rawSize = Number((item as any).size_bytes ?? (item as any).sizeBytes ?? 0)
      normalized.push({
        id,
        type,
        artifactId,
        filename,
        mimeType: typeof (item as any).mime_type === 'string'
          ? (item as any).mime_type.trim()
          : typeof (item as any).mimeType === 'string'
            ? (item as any).mimeType.trim()
            : null,
        sizeBytes: Number.isFinite(rawSize) && rawSize > 0 ? rawSize : null,
        kind: typeof (item as any).kind === 'string' ? (item as any).kind.trim() : null,
        downloadable: (item as any).downloadable !== false,
        previewUrl: typeof (item as any).preview_url === 'string' && (item as any).preview_url.trim()
          ? (item as any).preview_url.trim()
          : typeof (item as any).previewUrl === 'string' && (item as any).previewUrl.trim()
            ? (item as any).previewUrl.trim()
            : null,
        caption: typeof (item as any).caption === 'string' && (item as any).caption.trim()
          ? (item as any).caption.trim()
          : null,
        provenance: normalizeResponseBlockProvenance((item as any).provenance),
      })
    }
  }
  return normalized.length > 0 ? normalized : undefined
}

const appendStreamText = (current: string | undefined, incoming: unknown): string => {
  const prev = String(current || '')
  const next = String(incoming || '')
  if (!next) {
    return prev
  }
  if (!prev) {
    return next
  }

  const prevEnd = prev.slice(-1)
  const nextStart = next.slice(0, 1)
  if (!prevEnd || !nextStart || /\s/.test(prevEnd) || /\s/.test(nextStart)) {
    return `${prev}${next}`
  }
  if (/[\.\!\?\:\u2026]/.test(prevEnd) && /[A-Z0-9"'`(\[]/.test(nextStart)) {
    return `${prev}\n\n${next}`
  }
  if (/[A-Za-z0-9]/.test(prevEnd) && /[A-Za-z0-9]/.test(nextStart)) {
    return `${prev} ${next}`
  }
  return `${prev}${next}`
}

const mergeCompletedStreamText = (current: string | undefined, completed: unknown): string => {
  const prev = String(current || '')
  const finalText = String(completed || '')
  if (!prev) {
    return finalText
  }
  if (!finalText) {
    return prev
  }
  const normalizedPrev = prev.replace(/\s+/g, ' ').trim()
  const normalizedFinal = finalText.replace(/\s+/g, ' ').trim()
  if (normalizedPrev && normalizedFinal && normalizedPrev === normalizedFinal) {
    return prev
  }
  if (normalizedPrev && normalizedFinal && normalizedFinal.startsWith(normalizedPrev)) {
    return prev
  }
  return finalText
}

const appendActivityLogEntry = (
  current: ActivityLogEntry[] | undefined,
  entry: ActivityLogEntryLike,
): ActivityLogEntry[] => {
  const label = String(entry.label || '').trim()
  if (!label) {
    return Array.isArray(current) ? current : []
  }
  const nextEntry: ActivityLogEntry = {
    ...entry,
    id: typeof entry.id === 'string' && entry.id.trim() ? entry.id.trim() : `activity_${crypto.randomUUID()}`,
    createdAt: typeof entry.createdAt === 'string' && entry.createdAt.trim() ? entry.createdAt.trim() : new Date().toISOString(),
    label,
  }
  const existing = Array.isArray(current) ? current : []
  const nextSignature = [
    nextEntry.label,
    nextEntry.detail || '',
    nextEntry.status || '',
    nextEntry.stage || '',
    nextEntry.kind || '',
    nextEntry.flowRole || '',
    nextEntry.delegatedTaskId || '',
    nextEntry.parentDelegatedTaskId || '',
    nextEntry.specialistTaskId || '',
    nextEntry.agentId || '',
    nextEntry.intent || '',
    nextEntry.specialistEventType || '',
  ].join('\u241f')
  const hasDuplicate = existing.some((item) => {
    const itemId = String(item.id || '').trim()
    if (itemId && itemId === nextEntry.id) {
      return true
    }
    const itemSignature = [
      item.label,
      item.detail || '',
      item.status || '',
      item.stage || '',
      item.kind || '',
      item.flowRole || '',
      item.delegatedTaskId || '',
      item.parentDelegatedTaskId || '',
      item.specialistTaskId || '',
      item.agentId || '',
      item.intent || '',
      item.specialistEventType || '',
    ].join('\u241f')
    return itemSignature === nextSignature
  })
  if (hasDuplicate) {
    return existing
  }
  return [...existing, nextEntry]
}

const normalizeActivityLog = (value: unknown): ActivityLogEntry[] | undefined => {
  if (!Array.isArray(value)) {
    return undefined
  }
  const normalized: ActivityLogEntry[] = []
  for (const item of value) {
    if (!item || typeof item !== 'object') {
      continue
    }
    const label = typeof (item as any).label === 'string' ? (item as any).label.trim() : ''
    if (!label) {
      continue
    }
    const entry: ActivityLogEntry = {
      id: typeof (item as any).id === 'string' && (item as any).id.trim()
        ? (item as any).id.trim()
        : `activity_${crypto.randomUUID()}`,
      label,
      detail: typeof (item as any).detail === 'string' && (item as any).detail.trim()
        ? (item as any).detail.trim()
        : undefined,
      status: typeof (item as any).status === 'string' && (item as any).status.trim()
        ? (item as any).status.trim()
        : null,
      stage: typeof (item as any).stage === 'string' && (item as any).stage.trim()
        ? (item as any).stage.trim()
        : null,
      kind: typeof (item as any).kind === 'string' && (item as any).kind.trim()
        ? (item as any).kind.trim()
        : null,
      createdAt: typeof (item as any).created_at === 'string' && (item as any).created_at.trim()
        ? (item as any).created_at.trim()
        : typeof (item as any).createdAt === 'string' && (item as any).createdAt.trim()
          ? (item as any).createdAt.trim()
          : new Date().toISOString(),
      flowRole: typeof (item as any).flow_role === 'string' && (item as any).flow_role.trim()
        ? (item as any).flow_role.trim()
        : typeof (item as any).flowRole === 'string' && (item as any).flowRole.trim()
          ? (item as any).flowRole.trim()
          : null,
      delegatedTaskId: typeof (item as any).delegated_task_id === 'string' && (item as any).delegated_task_id.trim()
        ? (item as any).delegated_task_id.trim()
        : typeof (item as any).delegatedTaskId === 'string' && (item as any).delegatedTaskId.trim()
          ? (item as any).delegatedTaskId.trim()
          : null,
      parentDelegatedTaskId: typeof (item as any).parent_delegated_task_id === 'string' && (item as any).parent_delegated_task_id.trim()
        ? (item as any).parent_delegated_task_id.trim()
        : typeof (item as any).parentDelegatedTaskId === 'string' && (item as any).parentDelegatedTaskId.trim()
          ? (item as any).parentDelegatedTaskId.trim()
          : null,
      specialistTaskId: typeof (item as any).specialist_task_id === 'string' && (item as any).specialist_task_id.trim()
        ? (item as any).specialist_task_id.trim()
        : typeof (item as any).specialistTaskId === 'string' && (item as any).specialistTaskId.trim()
          ? (item as any).specialistTaskId.trim()
          : null,
      agentId: typeof (item as any).agent_id === 'string' && (item as any).agent_id.trim()
        ? (item as any).agent_id.trim()
        : typeof (item as any).agentId === 'string' && (item as any).agentId.trim()
          ? (item as any).agentId.trim()
          : null,
      agentLabel: typeof (item as any).agent_label === 'string' && (item as any).agent_label.trim()
        ? (item as any).agent_label.trim()
        : typeof (item as any).agentLabel === 'string' && (item as any).agentLabel.trim()
          ? (item as any).agentLabel.trim()
          : null,
      intent: typeof (item as any).intent === 'string' && (item as any).intent.trim()
        ? (item as any).intent.trim()
        : null,
      specialistEventType: typeof (item as any).specialist_event_type === 'string' && (item as any).specialist_event_type.trim()
        ? (item as any).specialist_event_type.trim()
        : typeof (item as any).specialistEventType === 'string' && (item as any).specialistEventType.trim()
          ? (item as any).specialistEventType.trim()
          : null,
    }
    const deduped = appendActivityLogEntry(normalized, entry)
    normalized.splice(0, normalized.length, ...deduped)
  }
  return normalized.length > 0 ? normalized : undefined
}

const mergeActivityLogEntries = (
  current: ActivityLogEntry[] | undefined,
  incoming: ActivityLogEntryLike[] | undefined,
): ActivityLogEntry[] | undefined => {
  let merged = Array.isArray(current) ? current : undefined
  for (const entry of incoming || []) {
    merged = appendActivityLogEntry(merged, entry)
  }
  return merged
}

const normalizeAlphaTerminalEntry = (value: unknown): AlphaTerminalEntry | null => {
  if (!value || typeof value !== 'object') {
    return null
  }
  const text = typeof (value as any).text === 'string' && (value as any).text.trim()
    ? (value as any).text.trim()
    : typeof (value as any).message === 'string' && (value as any).message.trim()
      ? (value as any).message.trim()
      : ''
  if (!text) {
    return null
  }
  const rawStream = typeof (value as any).stream === 'string' ? (value as any).stream.trim().toLowerCase() : ''
  const stream = rawStream === 'stderr' || rawStream === 'system' ? rawStream : 'stdout'
  return {
    id: typeof (value as any).id === 'string' && (value as any).id.trim()
      ? (value as any).id.trim()
      : `alpha_terminal_${crypto.randomUUID()}`,
    taskId: typeof (value as any).task_id === 'string' && (value as any).task_id.trim()
      ? (value as any).task_id.trim()
      : typeof (value as any).taskId === 'string' && (value as any).taskId.trim()
        ? (value as any).taskId.trim()
        : null,
    provider: typeof (value as any).provider === 'string' && (value as any).provider.trim()
      ? (value as any).provider.trim().toLowerCase()
      : null,
    stream,
    eventType: typeof (value as any).event_type === 'string' && (value as any).event_type.trim()
      ? (value as any).event_type.trim()
      : typeof (value as any).eventType === 'string' && (value as any).eventType.trim()
        ? (value as any).eventType.trim()
        : null,
    text,
    detail: typeof (value as any).detail === 'string' && (value as any).detail.trim()
      ? (value as any).detail.trim()
      : null,
    createdAt: typeof (value as any).created_at === 'string' && (value as any).created_at.trim()
      ? (value as any).created_at.trim()
      : typeof (value as any).createdAt === 'string' && (value as any).createdAt.trim()
        ? (value as any).createdAt.trim()
        : new Date().toISOString(),
  }
}

const normalizeAlphaTerminalLog = (value: unknown): AlphaTerminalEntry[] | undefined => {
  if (!Array.isArray(value)) {
    return undefined
  }
  let entries: AlphaTerminalEntry[] | undefined
  for (const item of value) {
    entries = appendAlphaTerminalEntry(entries, normalizeAlphaTerminalEntry(item))
  }
  return entries
}

const appendAlphaTerminalEntry = (
  current: AlphaTerminalEntry[] | undefined,
  incoming: AlphaTerminalEntry | null | undefined,
): AlphaTerminalEntry[] | undefined => {
  if (!incoming) {
    return current
  }
  const existing = Array.isArray(current) ? current : []
  const hasDuplicate = existing.some((item) => (
    item.id === incoming.id ||
    (
      item.taskId === incoming.taskId &&
      item.eventType === incoming.eventType &&
      item.text === incoming.text &&
      item.createdAt === incoming.createdAt
    )
  ))
  if (hasDuplicate) {
    return existing
  }
  return [...existing, incoming].slice(-120)
}

const mergeAlphaTerminalLogs = (
  current: AlphaTerminalEntry[] | undefined,
  incoming: AlphaTerminalEntry[] | undefined,
): AlphaTerminalEntry[] | undefined => {
  let merged = Array.isArray(current) ? current : undefined
  for (const entry of incoming || []) {
    merged = appendAlphaTerminalEntry(merged, entry)
  }
  return merged
}

const formatSpecialistAgentLabel = (value: unknown) => {
  const raw = String(value || '').trim()
  if (!raw) {
    return 'Specialist'
  }
  const normalized = raw
    .replace(/^cosmic\//i, '')
    .replace(/:.*$/, '')
    .replace(/[-_]/g, ' ')
    .trim()
  if (!normalized) {
    return raw
  }
  return normalized.charAt(0).toUpperCase() + normalized.slice(1)
}

const buildProgressActivityEntries = (
  event: any,
  activityText: string,
  statusMessage: string,
  progressState?: DocsProgressState | TabularProgressState,
): Omit<ActivityLogEntry, 'id' | 'createdAt'>[] => {
  const specialistDelegations = Array.isArray(event?.specialist_delegations)
    ? event.specialist_delegations
    : []
  if (specialistDelegations.length > 0) {
    return specialistDelegations.map((item: any) => ({
      label: String(item?.activity || '').trim() || `Delegated ${String(item?.intent || 'specialist work').trim()}`,
      detail: statusMessage || undefined,
      status: String(event?.status || '').trim() || null,
      stage: progressState?.stage || null,
      kind: 'delegation',
      flowRole: 'delegation',
      delegatedTaskId: String(item?.task_id || '').trim() || null,
      agentId: String(item?.agent_id || '').trim() || null,
      agentLabel: String(item?.agent_label || '').trim() || formatSpecialistAgentLabel(item?.agent_id),
      intent: String(item?.intent || '').trim() || null,
    }))
  }
  const specialist = event?.specialist && typeof event.specialist === 'object'
    ? event.specialist
    : null
  if (specialist) {
    return [{
      label: activityText,
      detail: statusMessage && statusMessage !== activityText ? statusMessage : undefined,
      status: String(event?.status || '').trim() || null,
      stage: progressState?.stage || null,
      kind: 'specialist_flow',
      flowRole: 'specialist',
      parentDelegatedTaskId: String(specialist.attach_to_task_id || '').trim() || null,
      specialistTaskId: String(specialist.task_id || '').trim() || null,
      agentId: String(specialist.agent_id || '').trim() || null,
      agentLabel: String(specialist.agent_label || '').trim() || formatSpecialistAgentLabel(specialist.agent_id),
      intent: String(specialist.intent || '').trim() || null,
      specialistEventType: String(specialist.event_type || '').trim() || null,
    }]
  }
  return [{
    label: activityText,
    detail: statusMessage || undefined,
    status: String(event?.status || '').trim() || null,
    stage: progressState?.stage || null,
    kind: progressState?.kind || 'generic',
  }]
}

const historyToMessages = (history: any[] = []): Message[] => {
  return history
    .filter((item) => item && (item.role === 'user' || item.role === 'assistant'))
    .map((item, index) => ({
      id: String(item.message_id || `${item.role}-${index}-${crypto.randomUUID()}`),
      role: item.role,
      content: String(item.content || ''),
      attachments: extractMessageAttachments(item?.metadata),
      producedArtifacts: normalizeProducedArtifacts(item?.metadata?.produced_artifacts),
      responseBlocks: normalizeResponseBlocks(item?.metadata?.response_blocks),
      thinking: typeof item?.metadata?.thinking_text === 'string' ? item.metadata.thinking_text : undefined,
      activityLog: normalizeActivityLog(item?.metadata?.activity_log),
      alphaTerminalLog: normalizeAlphaTerminalLog(item?.metadata?.alpha_terminal_log),
      sources: Array.isArray(item?.metadata?.sources) ? item.metadata.sources : undefined,
      stopped: Boolean(item?.metadata?.interrupted),
      channel: typeof item?.channel === 'string' ? item.channel : null,
      requestId: typeof item?.request_id === 'string'
        ? item.request_id
        : typeof item?.metadata?.request_id === 'string'
          ? item.metadata.request_id
          : null,
      source: typeof item?.metadata?.source === 'string' ? item.metadata.source : null,
      sourceId: typeof item?.metadata?.source_id === 'string' ? item.metadata.source_id : null,
      createdAt: typeof item?.created_at === 'string' ? item.created_at : null,
      activity: typeof item?.metadata?.activity === 'string' ? item.metadata.activity : undefined,
      progress: normalizeTabularProgress(item?.metadata?.tabular_progress) ?? normalizeDocsProgress(item?.metadata?.docs_progress),
    }))
}

const normalizeForegroundStreamSnapshot = (value: unknown): GatewayForegroundStreamSnapshot | null => {
  if (!value || typeof value !== 'object') {
    return null
  }
  const requestId = typeof (value as any).request_id === 'string'
    ? (value as any).request_id.trim()
    : typeof (value as any).requestId === 'string'
      ? (value as any).requestId.trim()
      : ''
  const taskId = typeof (value as any).task_id === 'string'
    ? (value as any).task_id.trim()
    : typeof (value as any).taskId === 'string'
      ? (value as any).taskId.trim()
      : ''
  if (!requestId && !taskId) {
    return null
  }
  const progress =
    normalizeTabularProgress((value as any).tabular_progress ?? (value as any).tabularProgress) ??
    normalizeDocsProgress((value as any).docs_progress ?? (value as any).docsProgress)
  const failed = Boolean((value as any).failed)
  const error = typeof (value as any).error === 'string' && (value as any).error.trim()
    ? (value as any).error.trim()
    : null
  return {
    requestId: requestId || null,
    taskId: taskId || null,
    sessionId: typeof (value as any).session_id === 'string'
      ? (value as any).session_id.trim()
      : typeof (value as any).sessionId === 'string'
        ? (value as any).sessionId.trim()
        : null,
    route: typeof (value as any).route === 'string' ? (value as any).route.trim() || null : null,
    messageId: typeof (value as any).message_id === 'string'
      ? (value as any).message_id.trim() || null
      : typeof (value as any).messageId === 'string'
        ? (value as any).messageId.trim() || null
        : null,
    content: failed
      ? (error || String((value as any).content || ''))
      : String((value as any).content || ''),
    thinking: typeof (value as any).thinking_text === 'string'
      ? (value as any).thinking_text
      : typeof (value as any).thinking === 'string'
        ? (value as any).thinking
        : undefined,
    activity: typeof (value as any).activity === 'string' && (value as any).activity.trim()
      ? (value as any).activity.trim()
      : undefined,
    activityLog: normalizeActivityLog((value as any).activity_log ?? (value as any).activityLog),
    alphaTerminalLog: normalizeAlphaTerminalLog((value as any).alpha_terminal_log ?? (value as any).alphaTerminalLog),
    progress,
    producedArtifacts: normalizeProducedArtifacts((value as any).produced_artifacts ?? (value as any).producedArtifacts),
    responseBlocks: normalizeResponseBlocks((value as any).response_blocks ?? (value as any).responseBlocks ?? (value as any).blocks),
    snapshotSeq: Number.isFinite(Number((value as any).snapshot_seq ?? (value as any).snapshotSeq))
      ? Number((value as any).snapshot_seq ?? (value as any).snapshotSeq)
      : null,
    sources: Array.isArray((value as any).sources) ? (value as any).sources : undefined,
    channel: typeof (value as any).channel === 'string' ? (value as any).channel : null,
    source: typeof (value as any).source === 'string' ? (value as any).source : null,
    sourceId: typeof (value as any).source_id === 'string'
      ? (value as any).source_id.trim() || null
      : typeof (value as any).sourceId === 'string'
        ? (value as any).sourceId.trim() || null
        : taskId || null,
    awaitingReply: (value as any).awaiting_reply === true || (value as any).awaitingReply === true,
    completed: Boolean((value as any).completed),
    failed,
    error,
    updatedAt: typeof (value as any).updated_at === 'string'
      ? (value as any).updated_at
      : typeof (value as any).updatedAt === 'string'
        ? (value as any).updatedAt
        : null,
  }
}

const normalizeForegroundStreamSnapshots = (value: unknown): GatewayForegroundStreamSnapshot[] => {
  if (!Array.isArray(value)) {
    return []
  }
  return value
    .map(normalizeForegroundStreamSnapshot)
    .filter((item): item is GatewayForegroundStreamSnapshot => Boolean(item))
    .sort((left, right) => {
      const leftTs = Date.parse(left.updatedAt || '')
      const rightTs = Date.parse(right.updatedAt || '')
      if (Number.isFinite(leftTs) && Number.isFinite(rightTs) && leftTs !== rightTs) {
        return leftTs - rightTs
      }
      return String(left.requestId || left.taskId || '').localeCompare(String(right.requestId || right.taskId || ''))
    })
}

const mergeHydratedMessages = (current: Message[], hydrated: Message[]): Message[] => {
  if (!Array.isArray(hydrated) || hydrated.length === 0) {
    return hydrated
  }

  const currentById = new Map<string, Message>()
  const currentByRoleAndRequest = new Map<string, Message>()
  for (const message of current) {
    if (typeof message.id === 'string' && message.id.trim()) {
      currentById.set(message.id.trim(), message)
    }
    if (message.role && typeof message.requestId === 'string' && message.requestId.trim()) {
      currentByRoleAndRequest.set(`${message.role}:${message.requestId.trim()}`, message)
    }
  }

  return hydrated.map((message) => {
    const existing =
      (typeof message.id === 'string' && message.id.trim()
        ? currentById.get(message.id.trim())
        : undefined) ??
      (message.role && typeof message.requestId === 'string' && message.requestId.trim()
        ? currentByRoleAndRequest.get(`${message.role}:${message.requestId.trim()}`)
        : undefined)

    if (!existing) {
      return message
    }

    return {
      ...message,
      content: String(message.content || '').trim() ? message.content : existing.content,
      attachments: message.attachments ?? existing.attachments,
      producedArtifacts: message.producedArtifacts ?? existing.producedArtifacts,
      responseBlocks: message.responseBlocks ?? existing.responseBlocks,
      thinking: typeof message.thinking === 'string' && message.thinking.trim()
        ? message.thinking
        : existing.thinking,
      activity: typeof message.activity === 'string' && message.activity.trim()
        ? message.activity
        : existing.activity,
      activityLog: message.activityLog ?? existing.activityLog,
      alphaTerminalLog: message.alphaTerminalLog ?? existing.alphaTerminalLog,
      sources: message.sources ?? existing.sources,
      requestId: message.requestId ?? existing.requestId,
      source: message.source ?? existing.source,
      sourceId: message.sourceId ?? existing.sourceId,
      channel: message.channel ?? existing.channel,
      stopped: message.stopped ?? existing.stopped,
      progress: message.progress ?? existing.progress,
      backgroundState: message.backgroundState ?? existing.backgroundState,
    }
  })
}

/** Extract a human-readable channel label from the raw channel string. */
const channelLabel = (ch: string | null | undefined): string | null => {
  if (!ch) return null
  const lower = ch.toLowerCase()
  if (lower.startsWith('mobile:')) return 'Mobile'
  if (lower.startsWith('whatsapp:')) return 'WhatsApp'
  if (lower.startsWith('telegram:')) return 'Telegram'
  return null
}

/** Check if a message originated from a non-desktop channel. */
const isExternalChannel = (msg: Message): boolean => {
  return channelLabel(msg.channel) !== null
}

const buildConversationContext = (messages: Message[]) => {
  return messages.slice(-10).map((message) => ({
    role: message.role,
    content: message.content,
  }))
}

const formatAttachmentSize = (sizeBytes: number) => {
  if (!Number.isFinite(sizeBytes) || sizeBytes <= 0) {
    return ''
  }
  if (sizeBytes >= 1024 * 1024) {
    return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MB`
  }
  return `${Math.max(1, Math.round(sizeBytes / 1024))} KB`
}

const buildPendingAttachmentSummary = (attachments: PendingDocumentAttachment[]) => {
  if (attachments.length <= 0) {
    return ''
  }
  if (attachments.length === 1) {
    return `Attached ${attachments[0].filename}`
  }
  return `Attached ${attachments.length} files`
}

const MAX_IMAGE_ATTACHMENTS_PER_MESSAGE = 20

const isImageAttachment = (attachment: Pick<PendingDocumentAttachment, 'mimeType'>) => {
  return String(attachment.mimeType || '').trim().toLowerCase().startsWith('image/')
}

const countImageAttachments = (attachments: PendingDocumentAttachment[]) => {
  return attachments.reduce((count, attachment) => count + (isImageAttachment(attachment) ? 1 : 0), 0)
}

const buildImageAttachmentLimitError = (imageCount: number) => {
  return `Up to ${MAX_IMAGE_ATTACHMENTS_PER_MESSAGE} images can be attached in one message. You selected ${imageCount}.`
}

const normalizeTabularProgress = (value: unknown): TabularProgressState | undefined => {
  if (!value || typeof value !== 'object') {
    return undefined
  }
  const label = String((value as any).label || '').trim()
  if (!label) {
    return undefined
  }
  const rawStage = String((value as any).stage || '').trim().toLowerCase()
  const total = Math.max(1, Number((value as any).total || 0) || 1)
  const current = Math.max(0, Math.min(total, Number((value as any).current || 0) || 0))
  const rawPercent = Number((value as any).percent || 0)
  const percent = Math.max(0, Math.min(1, Number.isFinite(rawPercent) ? rawPercent : 0))
  return {
    kind: 'tabular_parse',
    stage: rawStage || 'prepare',
    label,
    detail: String((value as any).detail || '').trim() || undefined,
    current,
    total,
    percent,
  }
}

const normalizeDocsProgress = (value: unknown): DocsProgressState | undefined => {
  if (!value || typeof value !== 'object') {
    return undefined
  }
  const rawStage = String((value as any).stage || '').trim().toLowerCase()
  const stage: DocsProgressState['stage'] =
    rawStage === 'parse' || rawStage === 'enhance' || rawStage === 'ready'
      ? rawStage
      : 'prepare'
  const label = String((value as any).label || '').trim()
  if (!label) {
    return undefined
  }
  const total = Math.max(1, Number((value as any).total || 0) || 1)
  const current = Math.max(0, Math.min(total, Number((value as any).current || 0) || 0))
  const rawPercent = Number((value as any).percent || 0)
  const percent = Math.max(0, Math.min(1, Number.isFinite(rawPercent) ? rawPercent : 0))
  return {
    kind: 'docs_parse',
    stage,
    label,
    detail: String((value as any).detail || '').trim() || undefined,
    current,
    total,
    percent,
  }
}

const normalizeBackgroundTask = (value: unknown): BackgroundTask | null => {
  if (!value || typeof value !== 'object') {
    return null
  }
  const requestId = String((value as any).request_id || (value as any).requestId || '').trim()
  if (!requestId) {
    return null
  }
  return {
    requestId,
    taskId: typeof (value as any).task_id === 'string' && (value as any).task_id.trim()
      ? (value as any).task_id.trim()
      : typeof (value as any).taskId === 'string' && (value as any).taskId.trim()
        ? (value as any).taskId.trim()
        : null,
    sessionId: typeof (value as any).session_id === 'string' && (value as any).session_id.trim()
      ? (value as any).session_id.trim()
      : typeof (value as any).sessionId === 'string' && (value as any).sessionId.trim()
        ? (value as any).sessionId.trim()
        : null,
    route: typeof (value as any).route === 'string' && (value as any).route.trim()
      ? (value as any).route.trim()
      : null,
    userQueryExcerpt: String((value as any).user_query_excerpt || (value as any).userQueryExcerpt || '').trim(),
    partialContent: String((value as any).partial_content || (value as any).partialContent || ''),
    partialThinking: String((value as any).partial_thinking || (value as any).partialThinking || ''),
    backgroundedAt: typeof (value as any).backgrounded_at === 'string' && (value as any).backgrounded_at.trim()
      ? (value as any).backgrounded_at.trim()
      : typeof (value as any).backgroundedAt === 'string' && (value as any).backgroundedAt.trim()
        ? (value as any).backgroundedAt.trim()
        : null,
    completed: Boolean((value as any).completed),
    failed: Boolean((value as any).failed),
    error: typeof (value as any).error === 'string' && (value as any).error.trim()
      ? (value as any).error.trim()
      : null,
    activity: typeof (value as any).activity === 'string' && (value as any).activity.trim()
      ? (value as any).activity.trim()
      : undefined,
    activityLog: normalizeActivityLog((value as any).activity_log ?? (value as any).activityLog),
    alphaTerminalLog: normalizeAlphaTerminalLog((value as any).alpha_terminal_log ?? (value as any).alphaTerminalLog),
    progress: normalizeTabularProgress((value as any).tabular_progress) ?? normalizeDocsProgress((value as any).docs_progress),
    producedArtifacts: normalizeProducedArtifacts((value as any).produced_artifacts ?? (value as any).producedArtifacts),
    sources: Array.isArray((value as any).sources) ? (value as any).sources : undefined,
  }
}

const TabularProgressCard = ({ progress }: { progress: TabularProgressState }) => {
  const percentLabel = `${Math.max(1, Math.round(progress.percent * 100))}%`
  const showCount = progress.total > 1
  const st = (progress.stage || '').toLowerCase()
  const stageLabel =
    st === 'ready'
      ? 'Ready'
      : st === 'parse_sheets' || st === 'parse'
        ? 'Parsing'
        : st === 'prepare'
          ? 'Preparing'
          : 'Working'
  return (
    <div className="docs-progress-card" role="status" aria-live="polite">
      <div className="docs-progress-head">
        <span className="docs-progress-kicker">Spreadsheet Processing</span>
        <span className={`docs-progress-stage ${st === 'ready' ? 'ready' : st}`}>{stageLabel}</span>
      </div>
      <div className="docs-progress-label">{progress.label}</div>
      {progress.detail && <div className="docs-progress-detail">{progress.detail}</div>}
      <div className="docs-progress-bar" aria-hidden="true">
        <span className="docs-progress-bar-fill" style={{ width: `${Math.max(4, Math.round(progress.percent * 100))}%` }} />
      </div>
      <div className="docs-progress-foot">
        <span>{percentLabel}</span>
        {showCount && <span>{progress.current}/{progress.total} files</span>}
      </div>
    </div>
  )
}

const DocsProgressCard = ({ progress }: { progress: DocsProgressState }) => {
  const percentLabel = `${Math.max(1, Math.round(progress.percent * 100))}%`
  const showCount = progress.total > 1
  const stageLabel =
    progress.stage === 'prepare'
      ? 'Preparing'
      : progress.stage === 'parse'
        ? 'Parsing'
        : progress.stage === 'enhance'
          ? 'Enhancing'
          : 'Ready'
  return (
    <div className="docs-progress-card" role="status" aria-live="polite">
      <div className="docs-progress-head">
        <span className="docs-progress-kicker">Document Processing</span>
        <span className={`docs-progress-stage ${progress.stage}`}>{stageLabel}</span>
      </div>
      <div className="docs-progress-label">{progress.label}</div>
      {progress.detail && <div className="docs-progress-detail">{progress.detail}</div>}
      <div className="docs-progress-bar" aria-hidden="true">
        <span className="docs-progress-bar-fill" style={{ width: `${Math.max(4, Math.round(progress.percent * 100))}%` }} />
      </div>
      <div className="docs-progress-foot">
        <span>{percentLabel}</span>
        {showCount && <span>{progress.current}/{progress.total} docs</span>}
      </div>
    </div>
  )
}

const DocumentAttachmentIcon = () => (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none">
    <path
      d="M8.5 12.5L13.7 7.3a3 3 0 1 1 4.24 4.24l-7.07 7.07a5 5 0 1 1-7.07-7.07l8.48-8.49"
      stroke="currentColor"
      strokeWidth="1.9"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </svg>
)

const UserMessageAttachments = ({ attachments }: { attachments?: MessageAttachment[] }) => {
  if (!attachments || attachments.length <= 0) {
    return null
  }
  return (
    <div className="message-attachment-bar">
      {attachments.map((attachment, index) => (
        <div
          key={attachment.filePath || `${attachment.filename}-${index}`}
          className="message-attachment-chip"
        >
          <span className="message-attachment-icon" aria-hidden="true">
            <DocumentAttachmentIcon />
          </span>
          <div className="message-attachment-copy">
            <span className="message-attachment-name">{attachment.filename}</span>
            <span className="message-attachment-meta">
              {formatAttachmentSize(Number(attachment.sizeBytes || 0)) || attachment.mimeType || 'Attachment'}
            </span>
          </div>
        </div>
      ))}
    </div>
  )
}

const AssistantFlowTimeline = ({ entries }: { entries?: ActivityLogEntry[] }) => {
  if (!entries || entries.length <= 0) {
    return null
  }
  const delegationMap = new Map<string, ActivityLogEntry>()
  for (const entry of entries) {
    const delegatedTaskId = String(entry.delegatedTaskId || '').trim()
    if (delegatedTaskId) {
      delegationMap.set(delegatedTaskId, entry)
    }
  }
  const childEntriesByParent = new Map<string, ActivityLogEntry[]>()
  const rootEntries: ActivityLogEntry[] = []
  for (const entry of entries) {
    const parentDelegatedTaskId = String(entry.parentDelegatedTaskId || '').trim()
    if (parentDelegatedTaskId && delegationMap.has(parentDelegatedTaskId)) {
      const existing = childEntriesByParent.get(parentDelegatedTaskId) || []
      childEntriesByParent.set(parentDelegatedTaskId, [...existing, entry])
      continue
    }
    rootEntries.push(entry)
  }
  return (
    <div className="assistant-flow" title="Agentic flow captured during this response">
      <div className="assistant-flow-label">Flow</div>
      <div className="assistant-flow-list">
        {rootEntries.map((entry, index) => {
          const children = childEntriesByParent.get(String(entry.delegatedTaskId || '').trim()) || []
          return (
            <div key={entry.id} className="assistant-flow-node">
              <div className="assistant-flow-item">
                <div className="assistant-flow-marker" aria-hidden="true">
                  <span>{index + 1}</span>
                </div>
                <div className="assistant-flow-copy">
                  <div className="assistant-flow-title">{entry.label}</div>
                  {entry.detail && entry.detail !== entry.label && (
                    <div className="assistant-flow-detail">{entry.detail}</div>
                  )}
                </div>
              </div>
              {children.length > 0 && (
                <div className="assistant-flow-children">
                  {children.map((child, childIndex) => (
                    <div key={child.id} className="assistant-flow-item child">
                      <div className="assistant-flow-marker child" aria-hidden="true">
                        <span>{`${index + 1}.${childIndex + 1}`}</span>
                      </div>
                      <div className="assistant-flow-copy">
                        <div className="assistant-flow-title">{child.label}</div>
                        {child.detail && child.detail !== child.label && (
                          <div className="assistant-flow-detail">{child.detail}</div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

type AlphaConsoleStatus = 'running' | 'completed' | 'failed' | 'stopped'

interface AlphaConsoleView {
  taskId: string | null
  provider: string | null
  status: AlphaConsoleStatus
  lines: AlphaTerminalEntry[]
}

const isAlphaActivityEntry = (entry: ActivityLogEntry) => {
  const agentId = String(entry.agentId || '').toLowerCase()
  const agentLabel = String(entry.agentLabel || '').toLowerCase()
  const intent = String(entry.intent || '').toLowerCase()
  const label = String(entry.label || '').toLowerCase()
  return (
    agentId.includes('alpha-agent') ||
    agentLabel.includes('alpha agent') ||
    intent.startsWith('alpha.') ||
    label.includes('alpha agent')
  )
}

const getAlphaEntryTaskId = (entry: ActivityLogEntry) => {
  return (
    String(entry.specialistTaskId || '').trim() ||
    String(entry.delegatedTaskId || '').trim() ||
    null
  )
}

const buildAlphaConsoleView = (
  entries?: ActivityLogEntry[],
  terminalLog?: AlphaTerminalEntry[],
  options: { stopped?: boolean } = {},
): AlphaConsoleView | null => {
  const terminalEntries = terminalLog || []
  if (terminalEntries.length <= 0) {
    return null
  }

  let taskId: string | null = null
  for (let index = terminalEntries.length - 1; index >= 0; index -= 1) {
    const candidate = String(terminalEntries[index].taskId || '').trim()
    if (candidate) {
      taskId = candidate
      break
    }
  }
  const alphaEntries = (entries || []).filter((entry) => {
    if (!isAlphaActivityEntry(entry)) {
      return false
    }
    if (!taskId) {
      return true
    }
    return getAlphaEntryTaskId(entry) === taskId
  })
  if (!taskId) {
    for (let index = alphaEntries.length - 1; index >= 0; index -= 1) {
      const candidate = getAlphaEntryTaskId(alphaEntries[index])
      if (candidate) {
        taskId = candidate
        break
      }
    }
  }
  const visibleTerminalEntries = taskId
    ? terminalEntries.filter((entry) => !entry.taskId || entry.taskId === taskId)
    : terminalEntries

  const latestLifecycle = [...alphaEntries].reverse().find((entry) => {
    const eventType = String(entry.specialistEventType || '').toLowerCase()
    const stage = String(entry.stage || '').toLowerCase()
    const status = String(entry.status || '').toLowerCase()
    return (
      eventType === 'task.completed' ||
      eventType === 'task.failed' ||
      eventType === 'task.cancelled' ||
      stage.includes('alpha.codex.completed') ||
      stage.includes('alpha.codex.failed') ||
      stage.includes('alpha.cursor.completed') ||
      stage.includes('alpha.cursor.failed') ||
      status === 'completed' ||
      status === 'failed' ||
      status === 'cancelled'
    )
  })
  const lifecycleText = `${latestLifecycle?.status || ''} ${latestLifecycle?.stage || ''} ${latestLifecycle?.specialistEventType || ''}`.toLowerCase()
  const status: AlphaConsoleStatus = Boolean(options.stopped) || lifecycleText.includes('cancelled') || lifecycleText.includes('canceled')
    ? 'stopped'
    : lifecycleText.includes('failed') || lifecycleText.includes('error')
      ? 'failed'
      : lifecycleText.includes('completed')
        ? 'completed'
        : 'running'

  return {
    taskId,
    provider: [...visibleTerminalEntries].reverse().find((entry) => entry.provider)?.provider || null,
    status,
    lines: visibleTerminalEntries.slice(-80),
  }
}

const formatAlphaProviderLabel = (provider?: string | null) => {
  const normalized = String(provider || '').trim().toLowerCase()
  if (normalized === 'cursor') return 'Cursor CLI'
  if (normalized === 'codex') return 'Codex CLI'
  return 'Alpha CLI'
}

const formatAlphaConsoleTime = (value?: string | null) => {
  if (!value) {
    return ''
  }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return ''
  }
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

const AlphaAgentConsole = ({
  entries,
  terminalLog,
  requestId,
  stopped,
  onStop,
}: {
  entries?: ActivityLogEntry[]
  terminalLog?: AlphaTerminalEntry[]
  requestId?: string | null
  stopped?: boolean
  onStop: (payload: { requestId?: string; taskId?: string }) => void
}) => {
  const view = buildAlphaConsoleView(entries, terminalLog, { stopped })
  const [expanded, setExpanded] = useState(() => view?.status === 'running')
  const [isPinnedToBottom, setIsPinnedToBottom] = useState(true)
  const terminalRef = useRef<HTMLDivElement | null>(null)

  const scrollTerminalToBottom = useCallback((behavior: ScrollBehavior = 'auto') => {
    const terminal = terminalRef.current
    if (!terminal) {
      return
    }
    terminal.scrollTo({ top: terminal.scrollHeight, behavior })
    setIsPinnedToBottom(true)
  }, [])

  const updateTerminalPinState = useCallback(() => {
    const terminal = terminalRef.current
    if (!terminal) {
      return
    }
    const distanceFromBottom = terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight
    setIsPinnedToBottom(distanceFromBottom <= 24)
  }, [])

  useEffect(() => {
    if (view?.status === 'running') {
      setExpanded(true)
    }
  }, [view?.status, view?.lines.length])

  useEffect(() => {
    setIsPinnedToBottom(true)
  }, [view?.taskId])

  useEffect(() => {
    if (!expanded || !isPinnedToBottom) {
      return
    }
    requestAnimationFrame(() => scrollTerminalToBottom('auto'))
  }, [expanded, isPinnedToBottom, scrollTerminalToBottom, view?.lines.length])

  if (!view) {
    return null
  }

  const isRunning = view.status === 'running'
  const statusLabel = view.status === 'running'
    ? 'Running'
    : view.status === 'completed'
      ? 'Complete'
      : view.status === 'failed'
        ? 'Failed'
        : 'Stopped'
  const normalizedRequestId = String(requestId || '').trim()
  const normalizedTaskId = String(view.taskId || '').trim()
  const canStop = isRunning && Boolean(normalizedTaskId || normalizedRequestId)

  return (
    <div className={`alpha-agent-console is-${view.status}`}>
      <div className="alpha-agent-console-head">
        <button
          type="button"
          className="alpha-agent-console-toggle"
          onClick={() => setExpanded((value) => {
            const next = !value
            if (next) {
              setIsPinnedToBottom(true)
            }
            return next
          })}
          aria-expanded={expanded}
          aria-label={expanded ? 'Collapse Alpha Agent console' : 'Expand Alpha Agent console'}
          title={expanded ? 'Collapse Alpha Agent console' : 'Expand Alpha Agent console'}
        >
          {expanded ? <ChevronDown size={15} strokeWidth={2.2} /> : <ChevronRight size={15} strokeWidth={2.2} />}
        </button>
        <div className="alpha-agent-console-title">
          <span className="alpha-agent-console-icon" aria-hidden="true">
            <Terminal size={15} strokeWidth={2.1} />
          </span>
          <span>{formatAlphaProviderLabel(view.provider)}</span>
        </div>
        <div className="alpha-agent-console-meta">
          <span className="alpha-agent-console-status">{statusLabel}</span>
          {normalizedTaskId && <span className="alpha-agent-console-task">{normalizedTaskId}</span>}
        </div>
        <button
          type="button"
          className="alpha-agent-console-stop"
          onClick={() => onStop({
            requestId: normalizedRequestId || undefined,
            taskId: normalizedTaskId || undefined,
          })}
          disabled={!canStop}
          title={canStop ? 'Stop Alpha Agent' : 'Alpha Agent is not running'}
          aria-label="Stop Alpha Agent"
        >
          <Square size={12} fill="currentColor" strokeWidth={2.2} />
        </button>
      </div>

      {expanded && (
        <div className="alpha-agent-terminal-shell">
          <div
            ref={terminalRef}
            className="alpha-agent-terminal"
            role="log"
            aria-live={isRunning ? 'polite' : 'off'}
            onScroll={updateTerminalPinState}
          >
            {view.lines.map((entry) => {
              return (
                <div key={entry.id} className={`alpha-agent-terminal-line is-${entry.stream || 'stdout'}`}>
                  <span className="alpha-agent-terminal-time">{formatAlphaConsoleTime(entry.createdAt)}</span>
                  <span className="alpha-agent-terminal-prompt">$</span>
                  <span className="alpha-agent-terminal-copy">
                    <span>{entry.text}</span>
                    {entry.detail && <span className="alpha-agent-terminal-detail">{entry.detail}</span>}
                  </span>
                </div>
              )
            })}
            {isRunning && (
              <div className="alpha-agent-terminal-line is-live">
                <span className="alpha-agent-terminal-time">live</span>
                <span className="alpha-agent-terminal-prompt">$</span>
                <span className="alpha-agent-terminal-copy">
                  streaming {formatAlphaProviderLabel(view.provider).toLowerCase()} events
                </span>
              </div>
            )}
          </div>
          {!isPinnedToBottom && (
            <button
              type="button"
              className="alpha-agent-terminal-bottom"
              onClick={() => scrollTerminalToBottom('smooth')}
              aria-label="Jump to latest Alpha CLI output"
              title="Jump to latest Alpha CLI output"
            >
              <ArrowDownToLine size={14} strokeWidth={2.2} />
            </button>
          )}
        </div>
      )}
    </div>
  )
}

const AssistantProducedArtifacts = ({
  messageId,
  artifacts,
  downloadingArtifactId,
  onDownload,
}: {
  messageId: string
  artifacts?: ProducedArtifact[]
  downloadingArtifactId: string | null
  onDownload: (messageId: string, artifact: ProducedArtifact) => void
}) => {
  if (!artifacts || artifacts.length <= 0) {
    return null
  }
  return (
    <div className="produced-artifacts-section">
      <div className="sources-header">PRODUCED FILES</div>
      <div className="sources-grid produced-artifacts-grid">
        {artifacts.map((artifact) => {
          const isDownloading = downloadingArtifactId === artifact.artifactId
          return (
            <div key={artifact.artifactId} className="source-card produced-artifact-card">
              <div className="source-header-row produced-artifact-header">
                <div className="produced-artifact-icon" aria-hidden="true">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M14 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V8l-6-6zm0 2.5L18.5 9H14V4.5zM12 17l-4-4h2.5v-3h3v3H16l-4 4z" />
                  </svg>
                </div>
                <div className="source-title produced-artifact-title" title={artifact.filename}>
                  {artifact.filename}
                </div>
              </div>
              <div className="source-snippet produced-artifact-snippet">
                {formatProducedArtifactKind(artifact)}
                {artifact.sizeBytes ? ` · ${formatAttachmentSize(artifact.sizeBytes)}` : ''}
                {artifact.createdByAgent ? ` · ${artifact.createdByAgent}` : ''}
              </div>
              <div className="source-footer produced-artifact-footer">
                <span className="source-idx">
                  {artifact.downloadable ? '↓' : '•'}
                </span>
                <button
                  type="button"
                  className="produced-artifact-download"
                  onClick={() => onDownload(messageId, artifact)}
                  disabled={!artifact.downloadable || isDownloading}
                >
                  {isDownloading ? 'Saving…' : artifact.downloadable ? 'Download' : 'Unavailable'}
                </button>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

const formatArtifactKind = ({ kind, mimeType }: { kind?: string | null; mimeType?: string | null }) => {
  const normalizedKind = String(kind || '').trim()
  if (normalizedKind) {
    return normalizedKind.replace(/[_-]+/g, ' ')
  }
  const mime = String(mimeType || '').trim()
  if (mime.startsWith('application/pdf')) return 'pdf'
  if (mime.includes('spreadsheet') || mime.includes('excel')) return 'spreadsheet'
  if (mime.startsWith('image/')) return 'image'
  return 'file'
}

const formatProducedArtifactKind = (artifact: ProducedArtifact) => {
  const kind = String(artifact.kind || '').trim()
  if (kind) {
    return kind.replace(/[_-]+/g, ' ')
  }
  const mime = String(artifact.mimeType || '').trim()
  if (mime.startsWith('application/pdf')) return 'pdf'
  if (mime.includes('spreadsheet') || mime.includes('excel')) return 'spreadsheet'
  if (mime.startsWith('image/')) return 'image'
  return 'file'
}

const formatProducedArtifactNotificationSummary = (artifacts: ProducedArtifact[]) => {
  if (!artifacts || artifacts.length === 0) {
    return 'A file is ready to download.'
  }
  const names = artifacts
    .slice(0, 3)
    .map((item) => String(item.filename || '').trim())
    .filter(Boolean)
  if (artifacts.length === 1) {
    return names[0] || 'A file is ready to download.'
  }
  const listed = names.join(' · ')
  const remainder = artifacts.length - names.length
  if (remainder > 0) {
    return `${listed} · +${remainder} more`
  }
  return listed || `${artifacts.length} files are ready to download.`
}

const GATEWAY_OUTPUT_ARTIFACT_PATH_RE = /^\/desktop\/messages\/([^/]+)\/artifacts\/([^/]+)\/download\/?$/

const extractMarkdownLinkText = (children: any): string => {
  if (typeof children === 'string' || typeof children === 'number') {
    return String(children)
  }
  if (Array.isArray(children)) {
    return children.map((child) => extractMarkdownLinkText(child)).join('')
  }
  if (children && typeof children === 'object' && 'props' in children) {
    return extractMarkdownLinkText((children as any).props?.children)
  }
  return ''
}

const parseGatewayOutputArtifactHref = (href: string) => {
  const rawHref = String(href || '').trim()
  if (!rawHref) {
    return null
  }
  try {
    const parsed = new URL(rawHref, 'http://cosmic.local')
    const match = parsed.pathname.match(GATEWAY_OUTPUT_ARTIFACT_PATH_RE)
    if (!match) {
      return null
    }
    return {
      messageId: decodeURIComponent(match[1] || ''),
      artifactId: decodeURIComponent(match[2] || ''),
    }
  } catch {
    return null
  }
}

const isExternalMarkdownHref = (href: string) => /^(https?:|mailto:|tel:)/i.test(String(href || '').trim())

const AssistantMarkdownLink = ({
  node,
  href,
  children,
  onClick,
  target,
  rel,
  ...props
}: any) => {
  const rawHref = String(href || '').trim()
  const artifactDownload = parseGatewayOutputArtifactHref(rawHref)
  const textLabel = extractMarkdownLinkText(children).trim()

  const handleClick = (event: React.MouseEvent<HTMLAnchorElement>) => {
    onClick?.(event)
    if (event.defaultPrevented || !rawHref) {
      return
    }
    if (artifactDownload && window.cosmic?.downloadGatewayOutputArtifact) {
      event.preventDefault()
      void window.cosmic.downloadGatewayOutputArtifact({
        messageId: artifactDownload.messageId,
        artifactId: artifactDownload.artifactId,
        suggestedFilename: textLabel || undefined,
      }).catch((error) => {
        console.error('Failed to download gateway output artifact from markdown link:', error)
      })
      return
    }
    if (isExternalMarkdownHref(rawHref) && window.cosmic?.openExternal) {
      event.preventDefault()
      window.cosmic.openExternal(rawHref)
    }
  }

  return (
    <a
      href={rawHref || href}
      onClick={handleClick}
      target={artifactDownload ? undefined : target ?? '_blank'}
      rel={artifactDownload ? undefined : rel ?? 'noopener noreferrer'}
      {...props}
    >
      {children}
    </a>
  )
}

const assistantMarkdownComponents = {
  table: ({ node, ...props }: any) => <div className="table-wrapper"><table {...props} /></div>,
  pre: ({ node, className, ...props }: any) => (
    <pre className={['code-block', className].filter(Boolean).join(' ')} {...props} />
  ),
  code: ({ node, inline, className, children, ...props }: any) => {
    if (inline) return <code className="inline-code" {...props}>{children}</code>
    return <code className={className} {...props}>{children}</code>
  },
  a: AssistantMarkdownLink,
}

const AssistantMarkdownBlock = ({ content }: { content: string }) => (
  <ReactMarkdown
    remarkPlugins={[remarkGfm, remarkMath]}
    rehypePlugins={[rehypeKatex]}
    components={assistantMarkdownComponents}
  >
    {content}
  </ReactMarkdown>
)

const formatInlineVisualAttribution = (block: ResponseArtifactBlock) => {
  const attribution = block.provenance?.attributionLabel?.trim()
  if (attribution) {
    return attribution
  }
  const sourceTitle = block.provenance?.sourceTitle?.trim()
  if (sourceTitle) {
    return sourceTitle
  }
  const sourceDomain = block.provenance?.sourceDomain?.trim()
  if (sourceDomain) {
    return sourceDomain
  }
  return ''
}

const humanizeArtifactFilename = (filename?: string | null) => {
  const raw = String(filename || '').trim()
  if (!raw) {
    return ''
  }
  return raw
    .replace(/\.[a-z0-9]+$/i, '')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

const formatInlineVisualBadge = (block: ResponseArtifactBlock) => {
  const kind = formatArtifactKind(block)
  if (kind === 'chart') return 'Generated chart'
  if (kind === 'reference image') return 'Reference image'
  if (kind === 'image') return 'Inline image'
  return kind
}

const formatInlineVisualSourceChip = (block: ResponseArtifactBlock) => {
  const sourceDomain = String(block.provenance?.sourceDomain || '').trim()
  if (sourceDomain) {
    return sourceDomain.replace(/^www\./i, '')
  }
  if (formatArtifactKind(block) === 'chart') {
    return 'Cosmic'
  }
  return ''
}

const formatInlineVisualTitle = (block: ResponseArtifactBlock) => {
  const caption = String(block.caption || '').trim()
  if (caption) return caption
  const sourceTitle = String(block.provenance?.sourceTitle || '').trim()
  if (sourceTitle) return sourceTitle
  const filename = humanizeArtifactFilename(block.filename)
  if (filename) return filename
  return formatArtifactKind(block) === 'chart' ? 'Inline chart' : 'Inline visual'
}

const formatInlineVisualSubtitle = (block: ResponseArtifactBlock, title: string) => {
  const kind = formatArtifactKind(block)
  if (kind === 'chart') {
    return 'Generated from structured data in this response.'
  }
  const sourceTitle = String(block.provenance?.sourceTitle || '').trim()
  if (sourceTitle && sourceTitle !== title) {
    return sourceTitle
  }
  const attribution = formatInlineVisualAttribution(block)
  if (attribution && attribution !== title) {
    return attribution
  }
  return ''
}

const AssistantResponseBlocks = ({
  blocks,
}: {
  blocks?: ResponseBlock[]
}) => {
  if (!blocks || blocks.length <= 0) {
    return null
  }
  return (
    <div className="assistant-response-blocks">
      {blocks.map((block) => {
        if (block.type === 'markdown') {
          return (
            <div key={block.id} className="assistant-response-markdown">
              <AssistantMarkdownBlock content={block.text} />
            </div>
          )
        }
        if (block.type === 'code') {
          return (
            <div key={block.id} className="assistant-response-code-shell">
              {block.language && (
                <div className="assistant-response-code-language">{block.language}</div>
              )}
              <pre className="code-block assistant-response-code-block">
                <code className={block.language ? `language-${block.language}` : undefined}>{block.code}</code>
              </pre>
            </div>
          )
        }
        if (block.type === 'image_slot' || block.type === 'chart_slot') {
          const slotStatus = typeof block.status === 'string' ? block.status.toLowerCase() : ''
          const slotFailed = slotStatus === 'failed'
          const slotLabel = slotFailed
            ? (block.type === 'chart_slot' ? 'Inline chart unavailable' : 'Inline image unavailable')
            : block.loadingLabel || (block.type === 'chart_slot' ? 'Generating a chart' : 'Finding a relevant image')
          return (
            <div
              key={block.id}
              className={[
                'assistant-inline-visual-slot',
                block.type === 'chart_slot' ? 'is-chart' : 'is-image',
                slotFailed ? 'is-failed' : '',
              ].join(' ')}
            >
              {!slotFailed && (
                <div className="assistant-inline-visual-slot-shell" aria-hidden="true">
                  <div className="assistant-inline-visual-slot-band" />
                  <div className="assistant-inline-visual-slot-band short" />
                </div>
              )}
              <div className="assistant-inline-visual-slot-copy">
                <div className="assistant-inline-visual-slot-badge">
                  {block.type === 'chart_slot' ? 'INLINE CHART' : 'INLINE IMAGE'}
                </div>
                <div className="assistant-inline-visual-slot-label">{slotLabel}</div>
                <div className="assistant-inline-visual-slot-subtle">
                  {slotFailed
                    ? 'Cosmic could not attach a reliable visual for this response.'
                    : 'Cosmic is preparing this visual without stopping the response.'}
                </div>
              </div>
            </div>
          )
        }
        if (block.type === 'image_artifact') {
          const attribution = formatInlineVisualAttribution(block)
          const badge = formatInlineVisualBadge(block)
          const sourceChip = formatInlineVisualSourceChip(block)
          const visualTitle = formatInlineVisualTitle(block)
          const visualSubtitle = formatInlineVisualSubtitle(block, visualTitle)
          const isChart = formatArtifactKind(block) === 'chart'
          return (
            <figure key={block.id} className={['assistant-inline-image-card', isChart ? 'is-chart' : 'is-image'].join(' ')}>
              <div className="assistant-inline-image-frame">
                {block.previewUrl ? (
                  <img
                    src={block.previewUrl}
                    alt={block.provenance?.altText || block.caption || block.filename}
                    className="assistant-inline-image"
                    loading="lazy"
                  />
                ) : (
                  <div className="assistant-inline-image-placeholder">Preview unavailable</div>
                )}
              </div>
              <figcaption className="assistant-inline-image-meta">
                <div className="assistant-inline-image-topline">
                  <div className="assistant-inline-image-badge">{badge}</div>
                  {sourceChip && (
                    <div className="assistant-inline-image-source-chip">{sourceChip}</div>
                  )}
                </div>
                <div className="assistant-inline-image-name">{visualTitle}</div>
                {visualSubtitle && (
                  <div className="assistant-inline-image-subtitle">{visualSubtitle}</div>
                )}
                {attribution && attribution !== visualTitle && attribution !== visualSubtitle && (
                  <div className="assistant-inline-image-provenance">{attribution}</div>
                )}
              </figcaption>
            </figure>
          )
        }
        if (block.type === 'file_artifact') {
          return (
            <div key={block.id} className="assistant-inline-file-card">
              <div className="assistant-inline-file-icon" aria-hidden="true">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M14 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V8l-6-6zm0 2.5L18.5 9H14V4.5zM12 17l-4-4h2.5v-3h3v3H16l-4 4z" />
                </svg>
              </div>
              <div className="assistant-inline-file-copy">
                <div className="assistant-inline-file-name">{block.filename}</div>
                <div className="assistant-inline-file-meta">
                  {formatArtifactKind(block)}
                  {block.sizeBytes ? ` · ${formatAttachmentSize(block.sizeBytes)}` : ''}
                </div>
              </div>
            </div>
          )
        }
        return null
      })}
    </div>
  )
}

const normalizeGatewayModelSelection = (value: unknown): GatewayModelSelection => {
  const normalized = String(value || '').trim().toLowerCase()
  if (normalized === 'haiku' || normalized === 'opus' || normalized === 'perplexity') {
    return normalized
  }
  return 'cosmic'
}

const MODEL_OPTIONS: Array<{ id: GatewayModelSelection; label: string; shortLabel: string }> = [
  { id: 'cosmic', label: 'Cosmic', shortLabel: 'COSMIC' },
  { id: 'haiku', label: 'Haiku', shortLabel: 'HAIKU' },
  { id: 'opus', label: 'Opus', shortLabel: 'OPUS' },
  { id: 'perplexity', label: 'Perplexity', shortLabel: 'PPLX' },
]

const LAUNCHPAD_TILES: Array<{ id: LauncherTileId; label: string; locked: boolean }> = [
  { id: 'chat', label: 'Chat', locked: false },
  { id: 'meeting', label: 'Meeting', locked: false },
  { id: 'task', label: 'Task', locked: false },
  { id: 'spaces', label: 'Spaces', locked: false },
]

const normalizePendingTaskInput = (value: any): PendingTaskInput | null => {
  const inputRequestId = String(value?.input_request_id || '').trim()
  const taskId = String(value?.task_id || '').trim()
  const question = String(value?.question || '').trim()
  if (!inputRequestId || !taskId || !question) {
    return null
  }
  return {
    inputRequestId,
    taskId,
    sessionId: typeof value?.session_id === 'string' ? value.session_id : null,
    agent: typeof value?.agent === 'string' ? value.agent : null,
    channel: typeof value?.channel === 'string' ? value.channel : null,
    question,
    options: Array.isArray(value?.options) ? value.options.map((item: any) => String(item || '').trim()).filter(Boolean) : [],
    status: typeof value?.status === 'string' ? value.status : undefined,
    timestamp: typeof value?.timestamp === 'string' ? value.timestamp : null,
  }
}

const sortPendingTaskInputs = (items: PendingTaskInput[]) => {
  return [...items].sort((left, right) => {
    const leftTs = Date.parse(left.timestamp || '')
    const rightTs = Date.parse(right.timestamp || '')
    if (Number.isFinite(leftTs) && Number.isFinite(rightTs) && leftTs !== rightTs) {
      return leftTs - rightTs
    }
    return left.inputRequestId.localeCompare(right.inputRequestId)
  })
}

const mergePendingTaskInputs = (existing: PendingTaskInput[], incoming: PendingTaskInput[]) => {
  const byId = new Map<string, PendingTaskInput>()
  for (const item of existing) {
    byId.set(item.inputRequestId, item)
  }
  for (const item of incoming) {
    byId.set(item.inputRequestId, item)
  }
  return sortPendingTaskInputs(Array.from(byId.values()))
}

// --- Foregrounded-task persistence helpers ---
// Tracks request IDs that have been moved to chat so the resume handler can
// filter them out even if the backend hasn't cleared the background flag yet.
const FOREGROUNDED_STORAGE_KEY = 'cosmic_foregrounded_ids'

function markRequestForegrounded(requestId: string) {
  try {
    const ids: string[] = JSON.parse(localStorage.getItem(FOREGROUNDED_STORAGE_KEY) || '[]')
    if (!ids.includes(requestId)) {
      ids.push(requestId)
      localStorage.setItem(FOREGROUNDED_STORAGE_KEY, JSON.stringify(ids))
    }
  } catch { /* best-effort */ }
}

function getForegroundedRequestIds(): Set<string> {
  try {
    const raw = JSON.parse(localStorage.getItem(FOREGROUNDED_STORAGE_KEY) || '[]')
    if (!Array.isArray(raw)) {
      return new Set<string>()
    }
    return new Set<string>(raw.map((value) => String(value)))
  } catch {
    return new Set<string>()
  }
}

function pruneStaleforegroundedIds(stillBackgroundIds: Set<string>) {
  try {
    const ids: string[] = JSON.parse(localStorage.getItem(FOREGROUNDED_STORAGE_KEY) || '[]')
    const kept = ids.filter((id) => stillBackgroundIds.has(id))
    if (kept.length !== ids.length) {
      localStorage.setItem(FOREGROUNDED_STORAGE_KEY, JSON.stringify(kept))
    }
  } catch { /* best-effort */ }
}

export default function App() {
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const modelDialRef = useRef<HTMLDivElement>(null)
  const composerSurfaceRef = useRef<HTMLDivElement>(null)
  const chatResponseSurfaceRef = useRef<HTMLDivElement>(null)
  const taskInterruptStackRef = useRef<HTMLDivElement>(null)
  const cronResultStackRef = useRef<HTMLDivElement>(null)
  const responseEndRef = useRef<HTMLDivElement>(null)
  const responseContainerRef = useRef<HTMLDivElement>(null)
  const modelDialSettleTimeoutRef = useRef<number | null>(null)
  const modelDialPulseTimeoutRef = useRef<number | null>(null)
  const modelDialWheelLockUntilRef = useRef(0)
  const surfaceLaunchResetTimeoutRef = useRef<number | null>(null)
  const meetingSurfaceRef = useRef<HTMLDivElement>(null)
  const spacesSurfaceRef = useRef<HTMLDivElement>(null)
  const activeAssistantMessageByRequestRef = useRef<Map<string, string>>(new Map())
  const activeAssistantMessageByTaskRef = useRef<Map<string, string>>(new Map())
  const streamedResponseRequestIdsRef = useRef<Set<string>>(new Set())
  const streamedResponseTaskIdsRef = useRef<Set<string>>(new Set())
  const activeStreamingRequestIdRef = useRef<string | null>(null)
  const activeStreamingTaskIdRef = useRef<string | null>(null)
  const messagesRef = useRef<Message[]>([])
  const backgroundTasksRef = useRef<BackgroundTask[]>([])
  const activeSessionIdRef = useRef<string | null>(null)
  const authStateRef = useRef<'loading' | 'unauthenticated' | 'authenticated'>('loading')
  const isStreamingRef = useRef(false)
  const searchStateRef = useRef<'hidden' | 'visible' | 'hiding'>('hidden')
  const showLauncherTrayRef = useRef(false)
  const lastGatewayResumeRequestAtRef = useRef(0)
  const shouldAutoScrollRef = useRef(true)
  const selectedModelRef = useRef<GatewayModelSelection>('cosmic')
  const seenCronResultKeysRef = useRef<Set<string>>(new Set())
  const seenArtifactReadyKeysRef = useRef<Set<string>>(new Set())

  // Composer input is uncontrolled — browser holds the value natively via `inputRef`.
  // React only re-renders on the empty↔non-empty transition (via `hasText`), so individual
  // keystrokes don't trigger a 5910-line App re-render. Submit reads inputRef.current.value.
  const [hasText, setHasText] = useState(false)
  const inputHasTextRef = useRef(false)
  const [pendingAttachments, setPendingAttachments] = useState<PendingDocumentAttachment[]>([])
  const [searchState, setSearchState] = useState<'hidden' | 'visible' | 'hiding'>('hidden')
  const [isIslandHovered, setIsIslandHovered] = useState(false)
  const [searchPosition, setSearchPosition] = useState<SearchPosition>('bottom')
  const [staybackTime, setStaybackTime] = useState(0)
  const [islandOpacity, setIslandOpacity] = useState(0.85) // Default opacity

  const [mode, setMode] = useState<QueryMode>('chat')
  const modeRef = useRef<QueryMode>('chat')
  const [isInputFocused, setIsInputFocused] = useState(false)
  const [showLauncherTray, setShowLauncherTray] = useState(false)
  /** Bumped with mailbox id so Spaces can open Agent Email → Inbox (e.g. Dynamic Island mail notify). */
  const [agentEmailInboxNavigateSignal, setAgentEmailInboxNavigateSignal] = useState(0)
  const [agentEmailInboxNavigateMailboxId, setAgentEmailInboxNavigateMailboxId] = useState<string | null>(null)
  const [agentEmailApprovalsNavigateSignal, setAgentEmailApprovalsNavigateSignal] = useState(0)
  const [agentEmailApprovalsNavigateId, setAgentEmailApprovalsNavigateId] = useState<string | null>(null)
  const [selectedModel, setSelectedModel] = useState<GatewayModelSelection>('cosmic')
  const [modelPulseModel, setModelPulseModel] = useState<GatewayModelSelection | null>(null)
  const [hoverTooltip, setHoverTooltip] = useState<HoverTooltipState | null>(null)
  const [surfaceLaunch, setSurfaceLaunch] = useState<SurfaceLaunchState | null>(null)
  const [viewportSize, setViewportSize] = useState(() => ({
    width: typeof window !== 'undefined' ? window.innerWidth : 1440,
    height: typeof window !== 'undefined' ? window.innerHeight : 900,
  }))

  // --- CHAT STATE ---
  const [messages, setMessages] = useState<Message[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [downloadingArtifactId, setDownloadingArtifactId] = useState<string | null>(null)
  const [showScrollButton, setShowScrollButton] = useState(false)
  const [streamingProgress, setStreamingProgress] = useState('')
  const [expandedCrossChannelIds, setExpandedCrossChannelIds] = useState<Set<string>>(new Set())

  // --- AUTH STATE ---
  const [authState, setAuthState] = useState<'loading' | 'unauthenticated' | 'authenticated'>('loading')
  const [authData, setAuthData] = useState<any>(null)
  const [gatewayStatus, setGatewayStatus] = useState<GatewayStatus>({ state: 'idle', connected: false })

  // --- HISTORY / DB STATE ---
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)
  const [pendingTaskInputs, setPendingTaskInputs] = useState<PendingTaskInput[]>([])
  const [backgroundTasks, setBackgroundTasks] = useState<BackgroundTask[]>([])
  const [taskInputDrafts, setTaskInputDrafts] = useState<Record<string, string>>({})
  const [submittingTaskInputs, setSubmittingTaskInputs] = useState<Record<string, boolean>>({})
  const [backgroundTaskErrors, setBackgroundTaskErrors] = useState<Record<string, string>>({})
  const [backgroundingRequestId, setBackgroundingRequestId] = useState<string | null>(null)
  const [foregroundingRequestId, setForegroundingRequestId] = useState<string | null>(null)
  const [taskInputErrors, setTaskInputErrors] = useState<Record<string, string>>({})
  const [dismissedTaskInterruptIds, setDismissedTaskInterruptIds] = useState<string[]>([])
  const [selectedTaskInputId, setSelectedTaskInputId] = useState<string | null>(null)
  const [selectedBackgroundRequestId, setSelectedBackgroundRequestId] = useState<string | null>(null)
  const [backgroundTaskListRetracted, setBackgroundTaskListRetracted] = useState(false)
  const [pendingTaskListRetracted, setPendingTaskListRetracted] = useState(false)
  const [taskInterruptIndex, setTaskInterruptIndex] = useState(0)
  const [cronResultNotifications, setCronResultNotifications] = useState<CronResultNotification[]>([])
  const [cronResultIndex, setCronResultIndex] = useState(0)
  const [artifactReadyNotifications, setArtifactReadyNotifications] = useState<ProducedArtifactNotification[]>([])
  const pendingTaskCount = pendingTaskInputs.length
  const backgroundTaskCount = backgroundTasks.length
  const taskDashboardCount = pendingTaskCount + backgroundTaskCount
  const orderedPendingTaskInputs = useMemo(() => [...pendingTaskInputs].reverse(), [pendingTaskInputs])
  const orderedBackgroundTasks = useMemo(() => backgroundTasks
    .map((task, index) => ({ task, index }))
    .filter(({ task }) => task.requestId !== foregroundingRequestId)
    .sort((a, b) => {
      const aCompleted = a.task.completed ? 1 : 0
      const bCompleted = b.task.completed ? 1 : 0
      if (aCompleted !== bCompleted) {
        return aCompleted - bCompleted
      }
      const tsA = String(a.task.backgroundedAt || '').trim()
      const tsB = String(b.task.backgroundedAt || '').trim()
      if (tsA && tsB) {
        const cmp = tsB.localeCompare(tsA)
        if (cmp !== 0) {
          return cmp
        }
        return b.index - a.index
      }
      if (tsA && !tsB) {
        return 1
      }
      if (!tsA && tsB) {
        return -1
      }
      return b.index - a.index
    })
    .map(({ task }) => task), [backgroundTasks, foregroundingRequestId])
  const visibleTaskInterrupts = useMemo(
    () => orderedPendingTaskInputs.filter((item) => !dismissedTaskInterruptIds.includes(item.inputRequestId)),
    [dismissedTaskInterruptIds, orderedPendingTaskInputs],
  )
  const orderedCronResultNotifications = useMemo(
    () => [...cronResultNotifications].reverse(),
    [cronResultNotifications],
  )
  const orderedArtifactReadyNotifications = useMemo(
    () => [...artifactReadyNotifications].reverse(),
    [artifactReadyNotifications],
  )
  const selectedTaskInput = useMemo(() => {
    if (orderedPendingTaskInputs.length === 0) {
      return null
    }
    if (selectedTaskInputId) {
      const matched = orderedPendingTaskInputs.find((item) => item.inputRequestId === selectedTaskInputId)
      if (matched) {
        return matched
      }
    }
    return orderedPendingTaskInputs[0]
  }, [orderedPendingTaskInputs, selectedTaskInputId])
  const selectedBackgroundTask = useMemo(() => {
    if (orderedBackgroundTasks.length === 0) {
      return null
    }
    if (selectedBackgroundRequestId) {
      const matched = orderedBackgroundTasks.find((item) => item.requestId === selectedBackgroundRequestId)
      if (matched) {
        return matched
      }
    }
    return orderedBackgroundTasks[0]
  }, [orderedBackgroundTasks, selectedBackgroundRequestId])
  const canBringBackgroundTaskToForeground = !isStreaming && !activeStreamingRequestIdRef.current && !activeStreamingTaskIdRef.current

  // Track key status for SetupModal
  const [keyStatus, setKeyStatus] = useState({
    haiku: false,
    perplexity: false,
    deepgram: false,
    groq: false,
    anthropic: false,
  })

  const resetInFlightAssistantMaps = () => {
    activeAssistantMessageByRequestRef.current.clear()
    activeAssistantMessageByTaskRef.current.clear()
    streamedResponseRequestIdsRef.current.clear()
    streamedResponseTaskIdsRef.current.clear()
    activeStreamingRequestIdRef.current = null
    activeStreamingTaskIdRef.current = null
  }

  const createAssistantMessageId = () => `assistant_${crypto.randomUUID()}`

  const createAssistantMessage = (overrides: Partial<Message> = {}): Message => ({
    id: overrides.id || createAssistantMessageId(),
    role: 'assistant',
    content: '',
    thinking: '',
    activity: '',
    activityLog: [],
    ...overrides,
  })

  const createUserMessage = (
    content: string,
    attachments?: MessageAttachment[],
    overrides: Partial<Message> = {},
  ): Message => ({
    id: `user_${crypto.randomUUID()}`,
    role: 'user',
    content,
    attachments,
    ...overrides,
  })

  const patchMessagesForRequest = (
    requestId: string,
    updater: (message: Message) => Message,
  ) => {
    const normalizedRequestId = String(requestId || '').trim()
    if (!normalizedRequestId) {
      return
    }
    setMessages((prev) => prev.map((message) => {
      if (message.role !== 'user' || message.requestId !== normalizedRequestId) {
        return message
      }
      return updater(message)
    }))
  }

  const buildCronResultNotificationKey = (value: {
    requestId?: string | null
    sourceId?: string | null
    createdAt?: string | null
    id?: string | null
    content?: string | null
  }) => {
    const requestId = String(value.requestId || '').trim()
    if (requestId) {
      return `request:${requestId}`
    }
    const messageId = String(value.id || '').trim()
    if (messageId) {
      return `message:${messageId}`
    }
    const sourceId = String(value.sourceId || '').trim()
    const createdAt = String(value.createdAt || '').trim()
    if (sourceId && createdAt) {
      return `source:${sourceId}:${createdAt}`
    }
    if (sourceId) {
      return `source:${sourceId}`
    }
    const content = String(value.content || '').trim()
    return content ? `content:${content.slice(0, 120)}` : ''
  }

  const buildArtifactReadyNotificationKey = (value: {
    messageId?: string | null
    requestId?: string | null
    sourceId?: string | null
    artifactIds?: string[]
  }) => {
    const messageId = String(value.messageId || '').trim()
    if (messageId) {
      return `message:${messageId}`
    }
    const requestId = String(value.requestId || '').trim()
    if (requestId) {
      return `request:${requestId}`
    }
    const sourceId = String(value.sourceId || '').trim()
    const artifactIds = Array.isArray(value.artifactIds)
      ? value.artifactIds.map((item) => String(item || '').trim()).filter(Boolean)
      : []
    if (sourceId && artifactIds.length > 0) {
      return `source:${sourceId}:${artifactIds.join(',')}`
    }
    return artifactIds.length > 0 ? `artifacts:${artifactIds.join(',')}` : ''
  }

  const isCronResultChatInactive = () => {
    return (
      searchStateRef.current !== 'visible' ||
      modeRef.current !== 'chat' ||
      showLauncherTrayRef.current
    )
  }

  const isProducedArtifactChatInactive = () => {
    return (
      searchStateRef.current !== 'visible' ||
      modeRef.current !== 'chat' ||
      showLauncherTrayRef.current
    )
  }

  const enqueueCronResultNotification = (notification: CronResultNotification) => {
    const dedupeKey = buildCronResultNotificationKey({
      requestId: notification.requestId,
      sourceId: notification.sourceId,
      createdAt: notification.createdAt,
      id: notification.id,
      content: notification.content,
    })
    if (!dedupeKey || seenCronResultKeysRef.current.has(dedupeKey)) {
      return
    }
    seenCronResultKeysRef.current.add(dedupeKey)
    setCronResultNotifications((prev) => {
      const exists = prev.some((item) => (
        buildCronResultNotificationKey({
          requestId: item.requestId,
          sourceId: item.sourceId,
          createdAt: item.createdAt,
          id: item.id,
          content: item.content,
        }) === dedupeKey
      ))
      if (exists) {
        return prev
      }
      return [...prev, notification]
    })
  }

  const enqueueArtifactReadyNotification = (notification: ProducedArtifactNotification) => {
    const dedupeKey = buildArtifactReadyNotificationKey({
      messageId: notification.messageId,
      requestId: notification.requestId,
      sourceId: notification.sourceId,
      artifactIds: notification.artifacts.map((item) => item.artifactId),
    })
    if (!dedupeKey || seenArtifactReadyKeysRef.current.has(dedupeKey)) {
      return
    }
    seenArtifactReadyKeysRef.current.add(dedupeKey)
    setArtifactReadyNotifications((prev) => {
      const exists = prev.some((item) => (
        buildArtifactReadyNotificationKey({
          messageId: item.messageId,
          requestId: item.requestId,
          sourceId: item.sourceId,
          artifactIds: item.artifacts.map((artifact) => artifact.artifactId),
        }) === dedupeKey
      ))
      if (exists) {
        return prev
      }
      return [...prev, notification]
    })
  }

  const dismissCronResultNotification = (notificationId: string) => {
    setCronResultNotifications((prev) => prev.filter((item) => item.id !== notificationId))
  }

  const dismissArtifactReadyNotification = (notificationId: string) => {
    setArtifactReadyNotifications((prev) => prev.filter((item) => item.id !== notificationId))
  }

  const clearCronResultNotifications = () => {
    setCronResultNotifications([])
    setCronResultIndex(0)
  }

  const clearArtifactReadyNotifications = () => {
    setArtifactReadyNotifications([])
  }

  const upsertBackgroundTask = (task: BackgroundTask) => {
    setBackgroundTasks((prev) => {
      const nextTask = {
        ...task,
        partialContent: String(task.partialContent || ''),
        partialThinking: String(task.partialThinking || ''),
        userQueryExcerpt: String(task.userQueryExcerpt || ''),
      }
      const existingIndex = prev.findIndex((item) => item.requestId === nextTask.requestId)
      if (existingIndex < 0) {
        return [...prev, nextTask]
      }
      return prev.map((item, index) => {
        if (index !== existingIndex) {
          return item
        }
        return {
          ...item,
          ...nextTask,
          activityLog: nextTask.activityLog ?? item.activityLog,
          alphaTerminalLog: mergeAlphaTerminalLogs(item.alphaTerminalLog, nextTask.alphaTerminalLog),
          producedArtifacts: nextTask.producedArtifacts ?? item.producedArtifacts,
          sources: nextTask.sources ?? item.sources,
        }
      })
    })
  }

  const patchBackgroundTask = (requestId: string, patch: (current: BackgroundTask) => BackgroundTask) => {
    const normalizedRequestId = String(requestId || '').trim()
    if (!normalizedRequestId) {
      return
    }
    setBackgroundTasks((prev) => prev.map((item) => {
      if (item.requestId !== normalizedRequestId) {
        return item
      }
      return patch(item)
    }))
  }

  const removeBackgroundTask = (requestId: string) => {
    const normalizedRequestId = String(requestId || '').trim()
    if (!normalizedRequestId) {
      return
    }
    setBackgroundTasks((prev) => prev.filter((item) => item.requestId !== normalizedRequestId))
    setBackgroundTaskErrors((prev) => {
      if (!(normalizedRequestId in prev)) {
        return prev
      }
      const next = { ...prev }
      delete next[normalizedRequestId]
      return next
    })
  }

  const openChatFromCronNotification = () => {
    shouldAutoScrollRef.current = true
    clearCronResultNotifications()
    modeRef.current = 'chat'
    setMode('chat')
    setShowLauncherTray(false)
    if (searchStateRef.current !== 'visible') {
      window.cosmic?.toggle?.()
    } else {
      showChatComposer()
    }
    window.setTimeout(() => {
      responseEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    }, 140)
  }

  const openChatFromArtifactNotification = () => {
    shouldAutoScrollRef.current = true
    clearArtifactReadyNotifications()
    modeRef.current = 'chat'
    setMode('chat')
    setShowLauncherTray(false)
    if (searchStateRef.current !== 'visible') {
      window.cosmic?.toggle?.()
    } else {
      showChatComposer()
    }
    window.setTimeout(() => {
      responseEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    }, 140)
  }

  const triggerModelDialPulse = (model: GatewayModelSelection) => {
    if (modelDialPulseTimeoutRef.current !== null) {
      window.clearTimeout(modelDialPulseTimeoutRef.current)
    }
    setModelPulseModel(model)
    modelDialPulseTimeoutRef.current = window.setTimeout(() => {
      setModelPulseModel((current) => (current === model ? null : current))
      modelDialPulseTimeoutRef.current = null
    }, 260)
  }

  const commitSelectedModel = (model: GatewayModelSelection, persist = true, withPulse = false) => {
    if (selectedModelRef.current === model) {
      if (withPulse) {
        triggerModelDialPulse(model)
      }
      return
    }
    selectedModelRef.current = model
    setSelectedModel(model)
    if (withPulse) {
      triggerModelDialPulse(model)
    }
    if (persist) {
      window.cosmic?.saveSetting('gatewayModelSelection', model)
    }
  }

  const scrollModelDialTo = (model: GatewayModelSelection, behavior: ScrollBehavior = 'smooth') => {
    const viewport = modelDialRef.current
    if (!viewport) {
      return
    }
    const target = viewport.querySelector<HTMLButtonElement>(`[data-model="${model}"]`)
    if (!target) {
      return
    }
    const targetLeft = target.offsetLeft - (viewport.clientWidth - target.offsetWidth) / 2
    viewport.scrollTo({
      left: Math.max(0, targetLeft),
      behavior,
    })
  }

  const getCenteredModelFromDial = (): GatewayModelSelection | null => {
    const viewport = modelDialRef.current
    if (!viewport) {
      return null
    }
    const buttons = Array.from(viewport.querySelectorAll<HTMLButtonElement>('[data-model]'))
    if (buttons.length === 0) {
      return null
    }

    const viewportCenter = viewport.scrollLeft + viewport.clientWidth / 2
    let closestModel: GatewayModelSelection | null = null
    let smallestDistance = Number.POSITIVE_INFINITY

    for (const button of buttons) {
      const model = button.dataset.model as GatewayModelSelection | undefined
      if (!model) {
        continue
      }
      const buttonCenter = button.offsetLeft + button.offsetWidth / 2
      const distance = Math.abs(buttonCenter - viewportCenter)
      if (distance < smallestDistance) {
        smallestDistance = distance
        closestModel = model
      }
    }

    return closestModel
  }

  const stepModelDial = (direction: -1 | 1) => {
    const currentModel = getCenteredModelFromDial() || selectedModelRef.current
    const currentIndex = MODEL_OPTIONS.findIndex((item) => item.id === currentModel)
    const nextIndex = Math.min(Math.max(currentIndex + direction, 0), MODEL_OPTIONS.length - 1)
    const nextModel = MODEL_OPTIONS[nextIndex]?.id
    if (!nextModel) {
      return
    }
    commitSelectedModel(nextModel, true, true)
    scrollModelDialTo(nextModel, 'smooth')
  }

  const showHoverTooltipForElement = (
    label: string,
    element: HTMLElement,
    tone: HoverTooltipTone,
  ) => {
    if (searchStateRef.current !== 'visible' || !element?.isConnected) {
      return
    }
    const rect = element.getBoundingClientRect()
    setHoverTooltip({
      label,
      x: rect.left + rect.width / 2,
      y: rect.top - 10,
      tone,
    })
  }

  const hideHoverTooltip = () => {
    setHoverTooltip(null)
  }

  const measureLaunchOffset = (element: HTMLElement | null, originX: number, originY: number) => {
    if (!element) {
      return { x: 0, y: 0 }
    }
    const rect = element.getBoundingClientRect()
    return {
      x: originX - (rect.left + rect.width / 2),
      y: originY - (rect.top + rect.height / 2),
    }
  }

  const resolveLaunchAnchor = (preferred: HTMLElement | null, fallback: HTMLElement | null) => {
    if (!preferred) {
      return fallback
    }
    const rect = preferred.getBoundingClientRect()
    if (rect.width <= 1 || rect.height <= 1) {
      return fallback
    }
    return preferred
  }

  const clearSurfaceLaunch = () => {
    if (surfaceLaunchResetTimeoutRef.current !== null) {
      window.clearTimeout(surfaceLaunchResetTimeoutRef.current)
      surfaceLaunchResetTimeoutRef.current = null
    }
    setSurfaceLaunch(null)
  }

  const startSurfaceLaunch = (target: 'chat' | 'meeting' | 'spaces', originX: number, originY: number) => {
    clearSurfaceLaunch()
    const composerAnchor = composerSurfaceRef.current
    const responseAnchor = resolveLaunchAnchor(chatResponseSurfaceRef.current, composerAnchor)
    const meetingAnchor = resolveLaunchAnchor(meetingSurfaceRef.current, composerAnchor)
    const spacesAnchor = resolveLaunchAnchor(spacesSurfaceRef.current, composerAnchor)
    const composerOffset = measureLaunchOffset(composerAnchor, originX, originY)
    const responseOffset = measureLaunchOffset(responseAnchor, originX, originY)
    const meetingOffset = measureLaunchOffset(meetingAnchor, originX, originY)
    const spacesOffset = measureLaunchOffset(spacesAnchor, originX, originY)
    setSurfaceLaunch({
      target,
      token: Date.now(),
      composerOffsetX: composerOffset.x,
      composerOffsetY: composerOffset.y,
      responseOffsetX: responseOffset.x,
      responseOffsetY: responseOffset.y,
      meetingOffsetX: meetingOffset.x,
      meetingOffsetY: meetingOffset.y,
      spacesOffsetX: spacesOffset.x,
      spacesOffsetY: spacesOffset.y,
    })
    surfaceLaunchResetTimeoutRef.current = window.setTimeout(() => {
      setSurfaceLaunch(null)
      surfaceLaunchResetTimeoutRef.current = null
    }, 420)
  }

  const bindAssistantMessageToEvent = (event: any, messageId: string) => {
    const requestId = typeof event?.request_id === 'string' ? event.request_id.trim() : ''
    const taskId = typeof event?.task_id === 'string' ? event.task_id.trim() : ''
    if (requestId) {
      activeAssistantMessageByRequestRef.current.set(requestId, messageId)
    }
    if (taskId) {
      activeAssistantMessageByTaskRef.current.set(taskId, messageId)
    }
  }

  const findAssistantMessageIdForEvent = (event: any) => {
    const requestId = typeof event?.request_id === 'string' ? event.request_id.trim() : ''
    const taskId = typeof event?.task_id === 'string' ? event.task_id.trim() : ''
    if (taskId) {
      const taskBoundId = activeAssistantMessageByTaskRef.current.get(taskId)
      if (taskBoundId) {
        return taskBoundId
      }
    }
    if (requestId) {
      const requestBoundId = activeAssistantMessageByRequestRef.current.get(requestId)
      if (requestBoundId) {
        return requestBoundId
      }
    }
    return null
  }

  const forgetAssistantMessageBindings = (event: any) => {
    const requestId = typeof event?.request_id === 'string' ? event.request_id.trim() : ''
    const taskId = typeof event?.task_id === 'string' ? event.task_id.trim() : ''
    if (requestId) {
      activeAssistantMessageByRequestRef.current.delete(requestId)
      streamedResponseRequestIdsRef.current.delete(requestId)
    }
    if (taskId) {
      activeAssistantMessageByTaskRef.current.delete(taskId)
      streamedResponseTaskIdsRef.current.delete(taskId)
    }
  }

  const markResponseStreamSeen = (event: any) => {
    const requestId = typeof event?.request_id === 'string' ? event.request_id.trim() : ''
    const taskId = typeof event?.task_id === 'string' ? event.task_id.trim() : ''
    if (requestId) {
      streamedResponseRequestIdsRef.current.add(requestId)
    }
    if (taskId) {
      streamedResponseTaskIdsRef.current.add(taskId)
    }
  }

  const clearActiveStreamingRefs = () => {
    activeStreamingRequestIdRef.current = null
    activeStreamingTaskIdRef.current = null
  }

  const findAssistantMessageIdForForegroundStream = (messages: Message[], stream: GatewayForegroundStreamSnapshot) => {
    const targetMessageId = String(stream.messageId || '').trim()
    if (targetMessageId) {
      const match = messages.find((message) => message.id === targetMessageId)
      if (match) {
        return match.id
      }
    }
    const requestId = String(stream.requestId || '').trim()
    if (requestId) {
      const match = messages.find((message) => message.role === 'assistant' && message.requestId === requestId)
      if (match) {
        return match.id
      }
    }
    const taskId = String(stream.taskId || '').trim()
    if (taskId) {
      const match = messages.find((message) => message.role === 'assistant' && message.sourceId === taskId)
      if (match) {
        return match.id
      }
    }
    return null
  }

  const restoreForegroundStreamsFromState = (
    streamsRaw: unknown,
    options: { historyTail?: any[] | null; clearIfNone?: boolean } = {},
  ) => {
    const streams = normalizeForegroundStreamSnapshots(streamsRaw)
    const usingHistoryTail = Array.isArray(options.historyTail)
    if (!usingHistoryTail && streams.length <= 0) {
      if (options.clearIfNone) {
        clearActiveStreamingRefs()
        setStreamingProgress('')
        setIsStreaming(false)
      }
      return
    }

    if (
      usingHistoryTail &&
      streams.length <= 0 &&
      (isStreamingRef.current || activeStreamingRequestIdRef.current || activeStreamingTaskIdRef.current)
    ) {
      const historyMessages = historyToMessages(options.historyTail || [])
      const activeRequestId = String(activeStreamingRequestIdRef.current || '').trim()
      const activeTaskId = String(activeStreamingTaskIdRef.current || '').trim()
      const historyHasActiveAssistant = historyMessages.some((message) => (
        message.role === 'assistant' &&
        (
          (activeRequestId && message.requestId === activeRequestId) ||
          (activeTaskId && message.sourceId === activeTaskId)
        )
      ))
      const activeInFlightMessages = messagesRef.current.filter((message) => (
        message.role === 'assistant' &&
        (
          (activeRequestId && message.requestId === activeRequestId) ||
          (activeTaskId && message.sourceId === activeTaskId)
        )
      ))
      if (!historyHasActiveAssistant && activeInFlightMessages.length > 0) {
        const historyMessageIds = new Set(historyMessages.map((message) => message.id))
        const nextMessages = [
          ...historyMessages,
          ...activeInFlightMessages.filter((message) => !historyMessageIds.has(message.id)),
        ]
        setMessages(nextMessages)
        messagesRef.current = nextMessages
        return
      }
    }

    const baseMessages = usingHistoryTail
      ? historyToMessages(options.historyTail || [])
      : messagesRef.current
    let nextMessages = [...baseMessages]

    if (usingHistoryTail) {
      resetInFlightAssistantMaps()
    }

    for (const stream of streams) {
      const requestId = String(stream.requestId || '').trim()
      const taskId = String(stream.taskId || '').trim()
      const fallbackMessageId =
        String(stream.messageId || '').trim() ||
        (requestId ? `pending_assistant_${requestId}` : taskId ? `pending_assistant_task_${taskId}` : createAssistantMessageId())
      const existingMessageId = findAssistantMessageIdForForegroundStream(nextMessages, stream)
      const existingIndex = existingMessageId
        ? nextMessages.findIndex((message) => message.id === existingMessageId)
        : -1
      const existingMessage = existingIndex >= 0 ? nextMessages[existingIndex] : null
      const nextMessage: Message = {
        ...(existingMessage || createAssistantMessage({ id: fallbackMessageId })),
        id: existingMessage?.id || fallbackMessageId,
        role: 'assistant',
        content: String(stream.content || existingMessage?.content || ''),
        thinking: typeof stream.thinking === 'string' ? stream.thinking : existingMessage?.thinking,
        activity: typeof stream.activity === 'string' ? stream.activity : existingMessage?.activity,
        activityLog: stream.activityLog ?? existingMessage?.activityLog,
        alphaTerminalLog: mergeAlphaTerminalLogs(existingMessage?.alphaTerminalLog, stream.alphaTerminalLog),
        progress: stream.progress ?? existingMessage?.progress,
        producedArtifacts: stream.producedArtifacts ?? existingMessage?.producedArtifacts,
        responseBlocks: stream.responseBlocks ?? existingMessage?.responseBlocks,
        sources: stream.sources ?? existingMessage?.sources,
        requestId: requestId || existingMessage?.requestId || null,
        source: stream.source ?? existingMessage?.source ?? null,
        sourceId: taskId || String(stream.sourceId || '').trim() || existingMessage?.sourceId || null,
        channel: stream.channel ?? existingMessage?.channel ?? null,
        createdAt: stream.updatedAt || existingMessage?.createdAt || new Date().toISOString(),
        stopped: false,
      }
      if (existingIndex >= 0) {
        nextMessages = nextMessages.map((message, index) => (
          index === existingIndex ? nextMessage : message
        ))
      } else {
        nextMessages = [...nextMessages, nextMessage]
      }
      if (requestId) {
        activeAssistantMessageByRequestRef.current.set(requestId, nextMessage.id)
      }
      if (taskId) {
        activeAssistantMessageByTaskRef.current.set(taskId, nextMessage.id)
      }
      if (requestId && (nextMessage.content || nextMessage.thinking || nextMessage.activity || nextMessage.activityLog?.length || nextMessage.alphaTerminalLog?.length)) {
        streamedResponseRequestIdsRef.current.add(requestId)
      }
      if (taskId && (nextMessage.content || nextMessage.thinking || nextMessage.activity || nextMessage.activityLog?.length || nextMessage.alphaTerminalLog?.length)) {
        streamedResponseTaskIdsRef.current.add(taskId)
      }
    }

    setMessages(nextMessages)
    messagesRef.current = nextMessages

    const activeStream = [...streams].reverse().find((stream) => !stream.completed && !stream.failed) || null
    if (activeStream) {
      activeStreamingRequestIdRef.current = String(activeStream.requestId || '').trim() || null
      activeStreamingTaskIdRef.current = String(activeStream.taskId || '').trim() || null
      setStreamingProgress(String(activeStream.activity || activeStream.progress?.label || '').trim())
      setIsStreaming(true)
      return
    }

    if (options.clearIfNone) {
      clearActiveStreamingRefs()
      setStreamingProgress('')
      setIsStreaming(false)
    }
  }

  const syncForegroundStreamsFromGatewayState = async () => {
    if (!window.cosmic?.getGatewayState) {
      return
    }
    try {
      const state = await window.cosmic.getGatewayState()
      if (!state) {
        return
      }
      restoreForegroundStreamsFromState((state as any).foregroundStreams ?? (state as any).foreground_streams, {
        clearIfNone: false,
      })
    } catch {
      return
    }
  }

  const removePendingTaskInput = (inputRequestId: string) => {
    setPendingTaskInputs((prev) => prev.filter((item) => item.inputRequestId !== inputRequestId))
    setTaskInputDrafts((prev) => {
      if (!(inputRequestId in prev)) {
        return prev
      }
      const next = { ...prev }
      delete next[inputRequestId]
      return next
    })
    setSubmittingTaskInputs((prev) => {
      if (!(inputRequestId in prev)) {
        return prev
      }
      const next = { ...prev }
      delete next[inputRequestId]
      return next
    })
    setTaskInputErrors((prev) => {
      if (!(inputRequestId in prev)) {
        return prev
      }
      const next = { ...prev }
      delete next[inputRequestId]
      return next
    })
    setDismissedTaskInterruptIds((prev) => prev.filter((item) => item !== inputRequestId))
  }

  const removePendingTaskInputsForTask = (taskId: string) => {
    const normalizedTaskId = String(taskId || '').trim()
    if (!normalizedTaskId) {
      return
    }
    const relatedInputRequestIds = pendingTaskInputs
      .filter((item) => item.taskId === normalizedTaskId)
      .map((item) => item.inputRequestId)
    setPendingTaskInputs((prev) => prev.filter((item) => item.taskId !== normalizedTaskId))
    setTaskInputDrafts((prev) => {
      if (relatedInputRequestIds.length === 0) {
        return prev
      }
      const next = { ...prev }
      let changed = false
      for (const inputRequestId of relatedInputRequestIds) {
        if (inputRequestId in next) {
          delete next[inputRequestId]
          changed = true
        }
      }
      return changed ? next : prev
    })
    setSubmittingTaskInputs((prev) => {
      if (relatedInputRequestIds.length === 0) {
        return prev
      }
      const next = { ...prev }
      let changed = false
      for (const inputRequestId of relatedInputRequestIds) {
        if (inputRequestId in next) {
          delete next[inputRequestId]
          changed = true
        }
      }
      return changed ? next : prev
    })
    setTaskInputErrors((prev) => {
      if (relatedInputRequestIds.length === 0) {
        return prev
      }
      const next = { ...prev }
      let changed = false
      for (const inputRequestId of relatedInputRequestIds) {
        if (inputRequestId in next) {
          delete next[inputRequestId]
          changed = true
        }
      }
      return changed ? next : prev
    })
    setDismissedTaskInterruptIds((prev) => prev.filter((item) => !relatedInputRequestIds.includes(item)))
  }

  const dismissTaskInterrupt = (inputRequestId: string) => {
    setDismissedTaskInterruptIds((prev) => (
      prev.includes(inputRequestId) ? prev : [...prev, inputRequestId]
    ))
  }

  const resetDesktopVmSessionState = (detail = 'Signed out from your VM.') => {
    hideHoverTooltip()
    clearSurfaceLaunch()
    resetInFlightAssistantMaps()
    clearActiveStreamingRefs()
    seenCronResultKeysRef.current.clear()
    seenArtifactReadyKeysRef.current.clear()
    setStreamingProgress('')
    setMessages([])
    setActiveSessionId(null)
    setPendingTaskInputs([])
    setBackgroundTasks([])
    setTaskInputDrafts({})
    setSubmittingTaskInputs({})
    setBackgroundTaskErrors({})
    setBackgroundingRequestId(null)
    setForegroundingRequestId(null)
    setTaskInputErrors({})
    setDismissedTaskInterruptIds([])
    setSelectedTaskInputId(null)
    setSelectedBackgroundRequestId(null)
    clearCronResultNotifications()
    clearArtifactReadyNotifications()
    setIsStreaming(false)
    setGatewayStatus({ state: 'idle', connected: false, detail, sessionId: null })
    setShowLauncherTray(false)
    setMode('chat')
    clearComposerInput()
    setPendingAttachments([])
    setIsInputFocused(false)
    shouldAutoScrollRef.current = true
    if (inputRef.current) inputRef.current.blur()
  }

  const ensureAssistantMessageForEvent = (messages: Message[], event: any) => {
    const boundId = findAssistantMessageIdForEvent(event)
    if (boundId && messages.some((message) => message.id === boundId)) {
      bindAssistantMessageToEvent(event, boundId)
      return {
        messages,
        messageId: boundId,
      }
    }

    const last = messages[messages.length - 1]
    if (last?.role === 'assistant') {
      bindAssistantMessageToEvent(event, last.id)
      return {
        messages,
        messageId: last.id,
      }
    }

    const messageId = createAssistantMessageId()
    bindAssistantMessageToEvent(event, messageId)
    return {
      messages: [...messages, createAssistantMessage({ id: messageId })],
      messageId,
    }
  }

  const refreshSessionFromGateway = async (sessionId?: string | null) => {
    const targetSessionId = typeof sessionId === 'string' && sessionId.trim()
      ? sessionId.trim()
      : activeSessionIdRef.current
    if (!targetSessionId || !window.cosmic?.getGatewaySessionHistory) {
      return
    }

    try {
      const payload = await window.cosmic.getGatewaySessionHistory(targetSessionId)
      resetInFlightAssistantMaps()
      setMessages((prev) => mergeHydratedMessages(prev, historyToMessages(payload?.messages)))
      setActiveSessionId(targetSessionId)
    } catch {
      return
    }
  }

  useEffect(() => {
    modeRef.current = mode
  }, [mode])

  useEffect(() => {
    searchStateRef.current = searchState
  }, [searchState])

  useEffect(() => {
    if (searchState !== 'visible') {
      hideHoverTooltip()
    }
  }, [searchState])

  useEffect(() => {
    showLauncherTrayRef.current = showLauncherTray
  }, [showLauncherTray])

  useEffect(() => {
    messagesRef.current = messages
  }, [messages])

  useEffect(() => {
    backgroundTasksRef.current = backgroundTasks
  }, [backgroundTasks])

  useEffect(() => {
    activeSessionIdRef.current = activeSessionId
  }, [activeSessionId])

  useEffect(() => {
    authStateRef.current = authState
  }, [authState])

  useEffect(() => {
    isStreamingRef.current = isStreaming
  }, [isStreaming])

  useEffect(() => {
    selectedModelRef.current = selectedModel
  }, [selectedModel])

  useEffect(() => {
    return () => {
      if (modelDialPulseTimeoutRef.current !== null) {
        window.clearTimeout(modelDialPulseTimeoutRef.current)
      }
    }
  }, [])

  useEffect(() => {
    if (orderedPendingTaskInputs.length === 0) {
      if (selectedTaskInputId !== null) {
        setSelectedTaskInputId(null)
      }
      return
    }
    if (!selectedTaskInputId || !orderedPendingTaskInputs.some((item) => item.inputRequestId === selectedTaskInputId)) {
      setSelectedTaskInputId(orderedPendingTaskInputs[0].inputRequestId)
    }
  }, [orderedPendingTaskInputs, selectedTaskInputId])

  useEffect(() => {
    if (orderedBackgroundTasks.length <= 1) {
      setBackgroundTaskListRetracted(false)
    }
  }, [orderedBackgroundTasks.length])

  useEffect(() => {
    if (orderedPendingTaskInputs.length <= 1) {
      setPendingTaskListRetracted(false)
    }
  }, [orderedPendingTaskInputs.length])

  useEffect(() => {
    if (dismissedTaskInterruptIds.length === 0) {
      return
    }
    const validIds = new Set(pendingTaskInputs.map((item) => item.inputRequestId))
    setDismissedTaskInterruptIds((prev) => prev.filter((id) => validIds.has(id)))
  }, [dismissedTaskInterruptIds.length, pendingTaskInputs])

  useEffect(() => {
    if (visibleTaskInterrupts.length === 0) {
      if (taskInterruptIndex !== 0) {
        setTaskInterruptIndex(0)
      }
      return
    }
    if (taskInterruptIndex > visibleTaskInterrupts.length - 1) {
      setTaskInterruptIndex(visibleTaskInterrupts.length - 1)
    }
  }, [taskInterruptIndex, visibleTaskInterrupts.length])

  useEffect(() => {
    const container = taskInterruptStackRef.current
    if (!container || visibleTaskInterrupts.length === 0) {
      return
    }
    const targetLeft = container.clientWidth * taskInterruptIndex
    if (Math.abs(container.scrollLeft - targetLeft) > 2) {
      container.scrollTo({ left: targetLeft, behavior: 'smooth' })
    }
  }, [taskInterruptIndex, visibleTaskInterrupts.length])

  useEffect(() => {
    const container = cronResultStackRef.current
    if (!container || orderedCronResultNotifications.length === 0) {
      return
    }
    const targetLeft = container.clientWidth * cronResultIndex
    if (Math.abs(container.scrollLeft - targetLeft) > 2) {
      container.scrollTo({ left: targetLeft, behavior: 'smooth' })
    }
  }, [cronResultIndex, orderedCronResultNotifications.length])

  useEffect(() => {
    if (orderedCronResultNotifications.length === 0) {
      setCronResultIndex(0)
      return
    }
    if (cronResultIndex > orderedCronResultNotifications.length - 1) {
      setCronResultIndex(orderedCronResultNotifications.length - 1)
    }
  }, [cronResultIndex, orderedCronResultNotifications.length])

  const handleTaskInterruptScroll = () => {
    const container = taskInterruptStackRef.current
    if (!container) {
      return
    }
    const cardWidth = container.clientWidth
    if (cardWidth <= 0) {
      return
    }
    const nextIndex = Math.max(
      0,
      Math.min(visibleTaskInterrupts.length - 1, Math.round(container.scrollLeft / cardWidth)),
    )
    if (nextIndex !== taskInterruptIndex) {
      setTaskInterruptIndex(nextIndex)
    }
  }

  const handleCronResultScroll = () => {
    const container = cronResultStackRef.current
    if (!container) {
      return
    }
    const cardWidth = container.clientWidth
    if (cardWidth <= 0) {
      return
    }
    const nextIndex = Math.max(
      0,
      Math.min(orderedCronResultNotifications.length - 1, Math.round(container.scrollLeft / cardWidth)),
    )
    if (nextIndex !== cronResultIndex) {
      setCronResultIndex(nextIndex)
    }
  }

  const showChatComposer = () => {
    hideHoverTooltip()
    clearCronResultNotifications()
    modeRef.current = 'chat'
    setMode('chat')
    setShowLauncherTray(false)
    setSearchState('visible')
    setIsInputFocused(true)
    setTimeout(() => {
      if (!inputRef.current) return
      inputRef.current.style.height = '24px'
      inputRef.current.focus()
    }, 10)
  }

  const maybeRequestGatewayResumeOnShow = () => {
    if (authStateRef.current !== 'authenticated' || !window.cosmic?.requestGatewayResume) {
      return
    }
    const now = Date.now()
    if (now - lastGatewayResumeRequestAtRef.current < 1500) {
      return
    }
    lastGatewayResumeRequestAtRef.current = now
    window.cosmic.requestGatewayResume().catch(() => { })
  }

  const showTaskSurface = (options: { focusComposer?: boolean; focusInputRequestId?: string | null } = {}) => {
    const { focusComposer = false, focusInputRequestId = null } = options
    hideHoverTooltip()
    setDismissedTaskInterruptIds([])
    if (focusInputRequestId) {
      setSelectedTaskInputId(focusInputRequestId)
    }
    setSearchState('visible')
    modeRef.current = 'task'
    setMode('task')
    setShowLauncherTray(false)
    setIsInputFocused(focusComposer)
    if (!focusComposer && inputRef.current) {
      inputRef.current.blur()
    }
    if (focusComposer) {
      setTimeout(() => {
        if (!inputRef.current) return
        inputRef.current.style.height = '24px'
        inputRef.current.focus()
      }, 10)
    }
  }

  const showMeetingSurface = () => {
    hideHoverTooltip()
    setSearchState('visible')
    modeRef.current = 'meeting'
    setMode('meeting')
    setShowLauncherTray(false)
    setIsInputFocused(false)
  }

  const showSpacesSurface = () => {
    hideHoverTooltip()
    setSearchState('visible')
    modeRef.current = 'spaces'
    setMode('spaces')
    setShowLauncherTray(false)
    setIsInputFocused(false)
    if (inputRef.current) {
      inputRef.current.blur()
    }
  }

  const openAgentEmailInboxFromIsland = (mailboxId: string) => {
    showSpacesSurface()
    setAgentEmailInboxNavigateMailboxId(String(mailboxId || '').trim() || null)
    setAgentEmailInboxNavigateSignal((n) => n + 1)
  }

  const openAgentEmailApprovalsFromIsland = (approvalId?: string | null) => {
    showSpacesSurface()
    setAgentEmailApprovalsNavigateId(approvalId ? String(approvalId).trim() || null : null)
    setAgentEmailApprovalsNavigateSignal((n) => n + 1)
  }

  // --- INIT & MOUSE EVENTS ---
  useEffect(() => {
    const unsubKeys = window.cosmic?.onKeyStatus((status) => {
      setKeyStatus({
        haiku: !!status.haiku,
        perplexity: !!status.perplexity,
        deepgram: !!status.deepgram,
        groq: !!status.groq,
        anthropic: !!status.anthropic,
      })
    })
    window.cosmic?.getLocalKeyStatus()

    // Load Settings + Auth Check
    window.cosmic?.getSettings()
    const unsubSettings = window.cosmic?.onSettingsUpdate((settings) => {
      console.log("App: Loaded settings", settings)
      if (settings['searchPosition']) setSearchPosition(settings['searchPosition'])
      if (settings['staybackTime']) setStaybackTime(parseInt(settings['staybackTime']))
      if (settings['islandOpacity']) setIslandOpacity(parseFloat(settings['islandOpacity']))
      if (settings['gatewayModelSelection']) {
        const nextModel = normalizeGatewayModelSelection(settings['gatewayModelSelection'])
        selectedModelRef.current = nextModel
        setSelectedModel(nextModel)
        requestAnimationFrame(() => {
          scrollModelDialTo(nextModel, 'auto')
        })
      }

      // Check auth from settings
      if (settings['cosmicAuth']) {
        try {
          const auth = typeof settings['cosmicAuth'] === 'string'
            ? JSON.parse(settings['cosmicAuth'])
            : settings['cosmicAuth']
          if (auth?.userId) {
            setAuthState('authenticated')
            setAuthData(auth)
            return
          }
        } catch { /* invalid JSON, treat as unauthenticated */ }
      }
      if (authState === 'loading') {
        setAuthState('unauthenticated')
      }
    })

    if (window.cosmic?.getGatewayState) {
      window.cosmic.getGatewayState()
        .then((state) => {
          if (!state) return
          if (state.status) {
            setGatewayStatus(state.status)
          }
          if (typeof state.sessionId === 'string') {
            setActiveSessionId(state.sessionId)
          }
          restoreForegroundStreamsFromState((state as any).foregroundStreams ?? (state as any).foreground_streams, {
            historyTail: Array.isArray(state.historyTail) ? state.historyTail : [],
            clearIfNone: true,
          })
        })
        .catch(() => { })
    }

    let lastIgnore: boolean | null = null
    let lastIsland: boolean | null = null

    const handleMouseMove = (e: MouseEvent) => {
      const el = document.elementFromPoint(e.clientX, e.clientY)
      if (!el) return
      const island = !!el.closest('.island')
      const settings = !!el.closest('.settings-overlay')
      const overlay = searchState !== 'hidden' && !!el.closest('.overlay')

      const cronNotice = !!el.closest('.cron-result-shell')
      const shouldHighlightIsland = island || settings || overlay
      const isInteractive = shouldHighlightIsland || cronNotice

      if (lastIsland !== shouldHighlightIsland) {
        lastIsland = shouldHighlightIsland
        setIsIslandHovered(shouldHighlightIsland)
      }
      const shouldIgnore = !isInteractive
      if (lastIgnore === shouldIgnore) return
      lastIgnore = shouldIgnore
      if (shouldIgnore) {
        ; (window as any).ipcRenderer.send('set-ignore-mouse-events', true, { forward: true })
      } else {
        ; (window as any).ipcRenderer.send('set-ignore-mouse-events', false)
      }
    }
    window.addEventListener('mousemove', handleMouseMove)
    return () => {
      unsubKeys?.()
      unsubSettings?.()
      window.removeEventListener('mousemove', handleMouseMove)
    }
  }, [searchState])

  // --- VISIBILITY HANDLERS ---
  const performHide = () => {
    hideHoverTooltip()
    setSearchState('hiding')
    setIsInputFocused(false)
    clearSurfaceLaunch()
    setTimeout(() => {
      setSearchState('hidden')
      setShowLauncherTray(false)
    }, 250)
  }

  useEffect(() => {
    const handleShown = () => {
      if (modeRef.current === 'spaces') {
        showSpacesSurface()
      } else if (modeRef.current === 'meeting') {
        showMeetingSurface()
      } else if (modeRef.current === 'task') {
        showTaskSurface({ focusComposer: false })
      } else {
        showChatComposer()
      }
      void syncForegroundStreamsFromGatewayState()
      maybeRequestGatewayResumeOnShow()
      // Scroll to bottom (or streaming point) on every reopen
      shouldAutoScrollRef.current = true
      window.setTimeout(() => {
        responseEndRef.current?.scrollIntoView({ behavior: 'instant' })
      }, 80)
    }
    const off1 = window.cosmic?.onShown(handleShown)
    const off2 = window.cosmic?.onHiding(performHide)
    const off3 = window.cosmic?.onMeetingInvoke(showMeetingSurface)
    const off4 = window.cosmic?.onMeetingToggle(() => {
      if (modeRef.current === 'meeting') {
        window.cosmic?.hide()
        return
      }
      showMeetingSurface()
    })

    return () => { off1?.(); off2?.(); off3?.(); off4?.() }
  }, [])

  useEffect(() => {
    const animationFrame = requestAnimationFrame(() => {
      scrollModelDialTo(selectedModelRef.current, 'auto')
    })

    return () => {
      cancelAnimationFrame(animationFrame)
      clearSurfaceLaunch()
      if (modelDialSettleTimeoutRef.current !== null) {
        window.clearTimeout(modelDialSettleTimeoutRef.current)
        modelDialSettleTimeoutRef.current = null
      }
    }
  }, [])

  useEffect(() => {
    if (mode !== 'chat') {
      return
    }
    const id = requestAnimationFrame(() => {
      scrollModelDialTo(selectedModelRef.current, 'auto')
    })
    return () => cancelAnimationFrame(id)
  }, [mode, selectedModel])

  // --- DATA LISTENERS ---
  useEffect(() => {
    const offEvent = window.cosmic?.onGatewayEvent((event) => {
      const eventType = String(event?.type || '')
      if (!eventType) return

      if (eventType === 'resume.ok') {
        const newSessionId = typeof event.session_id === 'string' ? event.session_id : null
        const oldSessionId = activeSessionIdRef.current
        const sessionRolledOver = oldSessionId && newSessionId && oldSessionId !== newSessionId
        setActiveSessionId(newSessionId)
        // If session rolled over (e.g. 4 AM boundary), prepend a rollover divider
        if (sessionRolledOver) {
          restoreForegroundStreamsFromState((event as any).foreground_streams, {
            historyTail: [
              {
                message_id: `session-rollover-${newSessionId}`,
                role: 'assistant',
                content: '',
                channel: '__session_rollover__',
              },
              ...(Array.isArray(event.history_tail) ? event.history_tail : []),
            ],
            clearIfNone: true,
          })
        }
        setPendingTaskInputs(
          mergePendingTaskInputs(
            [],
            Array.isArray(event.pending_inputs)
              ? event.pending_inputs.map(normalizePendingTaskInput).filter(Boolean) as PendingTaskInput[]
              : [],
          ),
        )
        const foregroundedIds = getForegroundedRequestIds()
        const resumedBackgroundTasks = Array.isArray((event as any).background_tasks)
          ? (event as any).background_tasks
              .map(normalizeBackgroundTask)
              .filter((t: BackgroundTask | null) => t && !foregroundedIds.has(t.requestId)) as BackgroundTask[]
          : []
        setBackgroundTasks(resumedBackgroundTasks)
        // Prune localStorage entries the backend no longer considers background
        const rawBackgroundIds = new Set<string>(
          (Array.isArray((event as any).background_tasks) ? (event as any).background_tasks : [])
            .map((t: any) => String(t?.request_id || '').trim())
            .filter((value: string): value is string => Boolean(value)),
        )
        pruneStaleforegroundedIds(rawBackgroundIds)
        setTaskInputDrafts({})
        setSubmittingTaskInputs({})
        setBackgroundTaskErrors({})
        setBackgroundingRequestId(null)
        setForegroundingRequestId(null)
        setTaskInputErrors({})
        setDismissedTaskInterruptIds([])
        setSelectedTaskInputId(null)
        setSelectedBackgroundRequestId(null)
        if (!sessionRolledOver) {
          restoreForegroundStreamsFromState((event as any).foreground_streams, {
            historyTail: Array.isArray(event.history_tail) ? event.history_tail : [],
            clearIfNone: true,
          })
        }
        return
      }

      if (eventType === 'task.backgrounded') {
        const requestId = String(event?.request_id || '').trim()
        const existingTask = requestId
          ? backgroundTasksRef.current.find((item) => item.requestId === requestId)
          : null
        const existingAssistantMessage = requestId
          ? messagesRef.current.find((message) => message.role === 'assistant' && message.requestId === requestId)
          : null
        const existingUserMessage = requestId
          ? messagesRef.current.find((message) => message.role === 'user' && message.requestId === requestId)
          : null
        const docsProgress = normalizeDocsProgress((event as any)?.docs_progress)
        const tabularProgress = normalizeTabularProgress((event as any)?.tabular_progress)
        const backgroundTask = normalizeBackgroundTask({
          ...(event || {}),
          user_query_excerpt:
            typeof (event as any)?.user_query_excerpt === 'string' && String((event as any).user_query_excerpt).trim()
              ? (event as any).user_query_excerpt
              : existingTask?.userQueryExcerpt || existingUserMessage?.content || '',
          partial_content: typeof (event as any)?.partial_content === 'string'
            ? (event as any).partial_content
            : existingTask?.partialContent || existingAssistantMessage?.content || '',
          partial_thinking: typeof (event as any)?.partial_thinking === 'string'
            ? (event as any).partial_thinking
            : existingTask?.partialThinking || existingAssistantMessage?.thinking || '',
          activity: typeof (event as any)?.activity === 'string'
            ? (event as any).activity
            : existingTask?.activity || existingAssistantMessage?.activity || '',
          activity_log: (event as any)?.activity_log ?? existingTask?.activityLog ?? existingAssistantMessage?.activityLog,
          alpha_terminal_log: (event as any)?.alpha_terminal_log ?? existingTask?.alphaTerminalLog ?? existingAssistantMessage?.alphaTerminalLog,
          docs_progress: docsProgress ?? (existingTask?.progress?.kind === 'docs_parse' ? existingTask.progress : undefined) ?? (existingAssistantMessage?.progress?.kind === 'docs_parse' ? existingAssistantMessage.progress : undefined),
          tabular_progress: tabularProgress ?? (existingTask?.progress?.kind === 'tabular_parse' ? existingTask.progress : undefined) ?? (existingAssistantMessage?.progress?.kind === 'tabular_parse' ? existingAssistantMessage.progress : undefined),
          produced_artifacts: (event as any)?.produced_artifacts ?? existingTask?.producedArtifacts ?? existingAssistantMessage?.producedArtifacts,
          sources: Array.isArray((event as any)?.sources) ? (event as any).sources : existingTask?.sources ?? existingAssistantMessage?.sources,
          completed: false,
        })
        if (!backgroundTask) {
          return
        }
        upsertBackgroundTask(backgroundTask)
        setBackgroundingRequestId((current) => (current === backgroundTask.requestId ? null : current))
        setBackgroundTaskErrors((prev) => {
          if (!(backgroundTask.requestId in prev)) {
            return prev
          }
          const next = { ...prev }
          delete next[backgroundTask.requestId]
          return next
        })
        if (activeStreamingRequestIdRef.current === backgroundTask.requestId) {
          setIsStreaming(false)
          setStreamingProgress('')
          clearActiveStreamingRefs()
          setMessages((prev) => {
            const requestId = backgroundTask.requestId
            const taskId = backgroundTask.taskId
            return prev.filter((message) => {
              if (message.role !== 'assistant') {
                return true
              }
              const matchesRequest = requestId && message.requestId === requestId
              const matchesTask = taskId && message.sourceId === taskId
              return !(matchesRequest || matchesTask)
            })
          })
          forgetAssistantMessageBindings(event)
        }
        patchMessagesForRequest(backgroundTask.requestId, (message) => ({
          ...message,
          backgroundState: 'working',
        }))
        return
      }

      if (eventType === 'task.foregrounded') {
        const requestId = String(event?.request_id || '').trim()
        const preservedTask = requestId
          ? backgroundTasksRef.current.find((item) => item.requestId === requestId)
          : null
        const foregroundTask = normalizeBackgroundTask({
          ...(event || {}),
          user_query_excerpt:
            typeof (event as any)?.user_query_excerpt === 'string' && String((event as any).user_query_excerpt).trim()
              ? (event as any).user_query_excerpt
              : preservedTask?.userQueryExcerpt || '',
          partial_content: typeof (event as any)?.partial_content === 'string'
            ? (event as any).partial_content
            : preservedTask?.partialContent || '',
          partial_thinking: typeof (event as any)?.partial_thinking === 'string'
            ? (event as any).partial_thinking
            : preservedTask?.partialThinking || '',
          activity_log: (event as any)?.activity_log ?? preservedTask?.activityLog,
          alpha_terminal_log: (event as any)?.alpha_terminal_log ?? preservedTask?.alphaTerminalLog,
          docs_progress: (event as any)?.docs_progress ?? (preservedTask?.progress?.kind === 'docs_parse' ? preservedTask.progress : undefined),
          tabular_progress: (event as any)?.tabular_progress ?? (preservedTask?.progress?.kind === 'tabular_parse' ? preservedTask.progress : undefined),
          produced_artifacts: (event as any)?.produced_artifacts ?? preservedTask?.producedArtifacts,
          sources: Array.isArray((event as any)?.sources) ? (event as any).sources : preservedTask?.sources,
          completed: Boolean((event as any).completed ?? preservedTask?.completed),
          failed: Boolean((event as any).failed ?? preservedTask?.failed),
          error: (event as any)?.error ?? preservedTask?.error,
        })
        if (requestId) {
          markRequestForegrounded(requestId)
          removeBackgroundTask(requestId)
          setSelectedBackgroundRequestId((current) => (current === requestId ? null : current))
          setForegroundingRequestId((current) => (current === requestId ? null : current))
          setBackgroundingRequestId((current) => (current === requestId ? null : current))
        }
        if (!requestId) {
          return
        }
        const messageId = createAssistantMessageId()
        bindAssistantMessageToEvent(event, messageId)
        setMessages((prev) => {
          const nextMessages = prev.filter((message) => (
            message.role !== 'assistant' || message.requestId !== requestId
          ))
          return [
            ...nextMessages,
            createAssistantMessage({
              id: messageId,
              requestId,
              content: foregroundTask?.partialContent || '',
              thinking: foregroundTask?.partialThinking || '',
              activity: foregroundTask?.activity,
              activityLog: foregroundTask?.activityLog,
              alphaTerminalLog: foregroundTask?.alphaTerminalLog,
              progress: foregroundTask?.progress,
              producedArtifacts: foregroundTask?.producedArtifacts,
              sources: foregroundTask?.sources,
            }),
          ]
        })
        activeStreamingRequestIdRef.current = requestId
        if (foregroundTask?.taskId) {
          activeStreamingTaskIdRef.current = foregroundTask.taskId
        }
        setStreamingProgress('')
        setIsStreaming(!Boolean((event as any).completed))
        shouldAutoScrollRef.current = true
        if (modeRef.current !== 'chat') {
          showChatComposer()
        }
        patchMessagesForRequest(requestId, (message) => {
          const next = { ...message }
          delete next.backgroundState
          return next
        })
        return
      }

      if (eventType.startsWith('task.background.')) {
        const backgroundEventType = eventType.slice('task.background.'.length)
        const requestId = String(event?.request_id || '').trim()
        const taskId = typeof event?.task_id === 'string' ? event.task_id.trim() : ''
        if (!requestId && !taskId) {
          return
        }
        if (requestId && activeStreamingRequestIdRef.current === requestId) {
          return
        }

        if (backgroundEventType === 'task.created') {
          upsertBackgroundTask({
            requestId: requestId || `background_${crypto.randomUUID()}`,
            taskId: taskId || null,
            sessionId: typeof event.session_id === 'string' ? event.session_id : null,
            route: typeof event.route === 'string' ? event.route : null,
            userQueryExcerpt: '',
            partialContent: '',
            partialThinking: '',
            backgroundedAt: null,
            completed: false,
          })
          return
        }

        if (backgroundEventType === 'task.progress') {
          const eventStatus = String(event.status || '').trim()
          const statusMessage = String(event.message || '').trim()
          const docsProgress = normalizeDocsProgress(event.docs_progress)
          const tabularProgress = normalizeTabularProgress((event as any).tabular_progress)
          const progressState = tabularProgress ?? docsProgress
          const alphaTerminalEntry = normalizeAlphaTerminalEntry((event as any).codex_terminal)
          const fallbackMessage = eventStatus ? `Task ${eventStatus}...` : 'Working in the background...'
          const activityText = progressState?.label || statusMessage || fallbackMessage
          const activityEntries = alphaTerminalEntry
            ? undefined
            : buildProgressActivityEntries(event, activityText, statusMessage, progressState)
          const activityLog = normalizeActivityLog((event as any).activity_log)
          upsertBackgroundTask({
            requestId,
            taskId: taskId || null,
            sessionId: typeof event.session_id === 'string' ? event.session_id : null,
            route: typeof event.route === 'string' ? event.route : null,
            userQueryExcerpt: '',
            partialContent: '',
            partialThinking: '',
            backgroundedAt: null,
            completed: false,
            activity: alphaTerminalEntry ? undefined : activityText,
            activityLog: mergeActivityLogEntries(
              activityLog,
              activityEntries,
            ),
            alphaTerminalLog: appendAlphaTerminalEntry(undefined, alphaTerminalEntry),
            progress: alphaTerminalEntry ? undefined : progressState,
          })
          patchBackgroundTask(requestId, (current) => ({
            ...current,
            taskId: taskId || current.taskId || null,
            sessionId: typeof event.session_id === 'string' ? event.session_id : current.sessionId,
            route: typeof event.route === 'string' ? event.route : current.route,
            activity: alphaTerminalEntry ? current.activity : activityText,
            activityLog: mergeActivityLogEntries(
              mergeActivityLogEntries(current.activityLog, activityLog),
              activityEntries,
            ),
            alphaTerminalLog: appendAlphaTerminalEntry(current.alphaTerminalLog, alphaTerminalEntry),
            progress: alphaTerminalEntry ? current.progress : progressState,
            completed: false,
          }))
          return
        }

        if (backgroundEventType === 'response.thinking.chunk') {
          patchBackgroundTask(requestId, (current) => ({
            ...current,
            taskId: taskId || current.taskId || null,
            partialThinking: appendStreamText(current.partialThinking, event.content),
            progress: undefined,
            completed: false,
          }))
          return
        }

        if (backgroundEventType === 'response.chunk') {
          patchBackgroundTask(requestId, (current) => ({
            ...current,
            taskId: taskId || current.taskId || null,
            partialContent: appendStreamText(current.partialContent, event.content),
            progress: undefined,
            completed: false,
          }))
          return
        }

        if (backgroundEventType === 'response.complete') {
          const producedArtifacts = normalizeProducedArtifacts((event as any).produced_artifacts)
          const activityLog = normalizeActivityLog((event as any).activity_log)
          patchBackgroundTask(requestId, (current) => ({
            ...current,
            taskId: taskId || current.taskId || null,
            sessionId: typeof event.session_id === 'string' ? event.session_id : current.sessionId,
            route: typeof event.route === 'string' ? event.route : current.route,
            partialContent: mergeCompletedStreamText(current.partialContent, event.content),
            partialThinking: typeof event.thinking_text === 'string'
              ? event.thinking_text
              : current.partialThinking,
            producedArtifacts: producedArtifacts ?? current.producedArtifacts,
            activityLog: mergeActivityLogEntries(current.activityLog, activityLog),
            sources: Array.isArray(event.sources) ? event.sources : current.sources,
            progress: undefined,
            completed: true,
            failed: false,
          }))
          if (producedArtifacts && producedArtifacts.length > 0 && isProducedArtifactChatInactive()) {
            const messageId = typeof (event as any).message_id === 'string' && (event as any).message_id.trim()
              ? (event as any).message_id.trim()
              : typeof event.request_id === 'string' && event.request_id.trim()
                ? `pending_assistant_${event.request_id.trim()}`
                : `artifact_ready_${crypto.randomUUID()}`
            enqueueArtifactReadyNotification({
              id: `artifact_ready_${messageId}`,
              messageId,
              requestId,
              sourceId: typeof event.source_id === 'string' ? event.source_id : null,
              sessionId: typeof event.session_id === 'string' ? event.session_id : null,
              channel: typeof event.channel === 'string' ? event.channel : null,
              createdAt: new Date().toISOString(),
              artifacts: producedArtifacts,
            })
          }
          patchMessagesForRequest(requestId, (message) => ({
            ...message,
            backgroundState: 'ready',
          }))
          return
        }

        if (backgroundEventType === 'task.failed') {
          patchBackgroundTask(requestId, (current) => ({
            ...current,
            taskId: taskId || current.taskId || null,
            completed: true,
            failed: true,
            error: String(event?.error?.message || event?.message || 'Background task failed.'),
            progress: undefined,
          }))
          patchMessagesForRequest(requestId, (message) => ({
            ...message,
            backgroundState: 'failed',
          }))
          return
        }

        if (backgroundEventType === 'task.completed' || backgroundEventType === 'task.cancelled') {
          patchBackgroundTask(requestId, (current) => ({
            ...current,
            taskId: taskId || current.taskId || null,
            completed: true,
            failed: backgroundEventType === 'task.cancelled' ? false : current.failed,
            progress: undefined,
          }))
          if (backgroundEventType === 'task.cancelled') {
            patchMessagesForRequest(requestId, (message) => {
              const next = { ...message }
              delete next.backgroundState
              return next
            })
          }
          return
        }
      }

      if (eventType === 'route_result') {
        if (typeof event.request_id === 'string' && event.request_id.trim()) {
          activeStreamingRequestIdRef.current = event.request_id.trim()
        }
        setActiveSessionId((prev) => typeof event.session_id === 'string' ? event.session_id : prev)
        setMessages((prev) => {
          const existingId = findAssistantMessageIdForEvent(event)
          if (existingId) {
            return prev
          }
          const messageId = createAssistantMessageId()
          bindAssistantMessageToEvent(event, messageId)
          return [...prev, createAssistantMessage({ id: messageId })]
        })
        return
      }

      if (eventType === 'task.created') {
        if (typeof event.task_id === 'string' && event.task_id.trim()) {
          activeStreamingTaskIdRef.current = event.task_id.trim()
        }
        setMessages((prev) => {
          const { messages: nextMessages } = ensureAssistantMessageForEvent(prev, event)
          return nextMessages
        })
        return
      }

      if (eventType === 'task.input_required') {
        const pendingInput = normalizePendingTaskInput(event)
        if (!pendingInput) {
          return
        }
        setPendingTaskInputs((prev) => mergePendingTaskInputs(prev, [pendingInput]))
        setTaskInputErrors((prev) => {
          if (!(pendingInput.inputRequestId in prev)) {
            return prev
          }
          const next = { ...prev }
          delete next[pendingInput.inputRequestId]
          return next
        })
        setDismissedTaskInterruptIds((prev) => prev.filter((id) => id !== pendingInput.inputRequestId))
        if (modeRef.current === 'task') {
          setSelectedTaskInputId(pendingInput.inputRequestId)
        }
        return
      }

      if (eventType === 'task.input_reply.accepted') {
        const inputRequestId = String(event?.input_request_id || '').trim()
        if (!inputRequestId) {
          return
        }
        removePendingTaskInput(inputRequestId)
        return
      }

      if (eventType === 'task.progress') {
        if (typeof event.task_id !== 'string' && typeof event.request_id !== 'string') {
          return
        }
        const eventStatus = String(event.status || '').trim()
        const statusMessage = String(event.message || '').trim()
        const docsProgress = normalizeDocsProgress(event.docs_progress)
        const tabularProgress = normalizeTabularProgress((event as any).tabular_progress)
        const progressState = tabularProgress ?? docsProgress
        const alphaTerminalEntry = normalizeAlphaTerminalEntry((event as any).codex_terminal)
        const fallbackMessage = eventStatus ? `Task ${eventStatus}...` : 'Working on your request...'
        const activityText = progressState?.label || statusMessage || fallbackMessage
        const activityEntries = alphaTerminalEntry
          ? undefined
          : buildProgressActivityEntries(event, activityText, statusMessage, progressState)
        const activityLog = normalizeActivityLog((event as any).activity_log)
        if (!alphaTerminalEntry) {
          setStreamingProgress(activityText)
        }
        setMessages((prev) => {
          const { messages: nextMessages, messageId } = ensureAssistantMessageForEvent(prev, event)
          return nextMessages.map((message) => {
            if (message.id !== messageId) {
              return message
            }
            return {
              ...message,
              activity: alphaTerminalEntry ? message.activity : activityText,
              activityLog: mergeActivityLogEntries(
                mergeActivityLogEntries(message.activityLog, activityLog),
                activityEntries,
              ),
              alphaTerminalLog: appendAlphaTerminalEntry(message.alphaTerminalLog, alphaTerminalEntry),
              progress: alphaTerminalEntry ? message.progress : progressState,
              stopped: false,
            }
          })
        })
        return
      }

      if (eventType === 'response.chunk') {
        markResponseStreamSeen(event)
        setMessages((prev) => {
          if (!event.content) return prev
          const { messages: nextMessages, messageId } = ensureAssistantMessageForEvent(prev, event)
          return nextMessages.map((message) => {
            if (message.id !== messageId) {
              return message
            }
            return {
              ...message,
              content: appendStreamText(message.content, event.content),
              progress: undefined,
              stopped: false,
            }
          })
        })
        return
      }

      if (eventType === 'response.thinking.chunk') {
        setMessages((prev) => {
          if (!event.content) return prev
          const { messages: nextMessages, messageId } = ensureAssistantMessageForEvent(prev, event)
          return nextMessages.map((message) => {
            if (message.id !== messageId) {
              return message
            }
            return {
              ...message,
              thinking: appendStreamText(message.thinking, event.content),
              progress: undefined,
              stopped: false,
            }
          })
        })
        return
      }

      if (eventType === 'response.blocks.snapshot') {
        markResponseStreamSeen(event)
        setActiveSessionId((prev) => typeof event.session_id === 'string' ? event.session_id : prev)
        const responseBlocks = normalizeResponseBlocks((event as any).response_blocks ?? (event as any).blocks)
        if (!responseBlocks || responseBlocks.length <= 0) {
          return
        }
        setMessages((prev) => {
          const { messages: nextMessages, messageId } = ensureAssistantMessageForEvent(prev, event)
          return nextMessages.map((message) => {
            if (message.id !== messageId) {
              return message
            }
            return {
              ...message,
              responseBlocks,
              progress: undefined,
              stopped: false,
            }
          })
        })
        return
      }

      if (eventType === 'response.complete') {
        markResponseStreamSeen(event)
        setStreamingProgress('')
        setActiveSessionId((prev) => typeof event.session_id === 'string' ? event.session_id : prev)
        const producedArtifacts = normalizeProducedArtifacts((event as any).produced_artifacts)
        const responseBlocks = normalizeResponseBlocks((event as any).response_blocks ?? (event as any).blocks)
        const activityLog = normalizeActivityLog((event as any).activity_log)
        const alphaTerminalLog = normalizeAlphaTerminalLog((event as any).alpha_terminal_log)
        setMessages((prev) => {
          const sources = Array.isArray(event.sources) ? event.sources : undefined
          const persistedMessageId = typeof (event as any).message_id === 'string'
            ? (event as any).message_id.trim()
            : ''
          const { messages: nextMessages, messageId } = ensureAssistantMessageForEvent(prev, event)
          if (persistedMessageId) {
            bindAssistantMessageToEvent(event, persistedMessageId)
          }
          const updatedMessages = nextMessages.map((message) => {
            if (message.id !== messageId) {
              return message
            }
            return {
              ...message,
              id: persistedMessageId || message.id,
              content: mergeCompletedStreamText(message.content, event.content),
              sources,
              producedArtifacts: producedArtifacts ?? message.producedArtifacts,
              responseBlocks: responseBlocks ?? message.responseBlocks,
              activityLog: mergeActivityLogEntries(message.activityLog, activityLog),
              alphaTerminalLog: mergeAlphaTerminalLogs(message.alphaTerminalLog, alphaTerminalLog),
              requestId: typeof event.request_id === 'string' ? event.request_id : message.requestId,
              source: typeof event.source === 'string' ? event.source : message.source,
              sourceId: typeof event.source_id === 'string' ? event.source_id : message.sourceId,
              createdAt: new Date().toISOString(),
              progress: undefined,
              stopped: false,
            }
          })
          if (!event.task_id) {
            forgetAssistantMessageBindings(event)
          }
          return updatedMessages
        })
        const eventSource = String(event.source || '').trim()
        const eventContent = String(event.content || '').trim()
        if (eventSource === 'cron' && eventContent && isCronResultChatInactive()) {
          enqueueCronResultNotification({
            id: `cron_result_${String(event.request_id || event.source_id || crypto.randomUUID())}`,
            requestId: typeof event.request_id === 'string' ? event.request_id : null,
            sourceId: typeof event.source_id === 'string' ? event.source_id : null,
            sessionId: typeof event.session_id === 'string' ? event.session_id : null,
            content: eventContent,
            channel: typeof event.channel === 'string' ? event.channel : null,
            createdAt: new Date().toISOString(),
          })
        }
        if (producedArtifacts && producedArtifacts.length > 0 && isProducedArtifactChatInactive()) {
          const messageId = typeof (event as any).message_id === 'string' && (event as any).message_id.trim()
            ? (event as any).message_id.trim()
            : typeof event.request_id === 'string' && event.request_id.trim()
              ? `pending_assistant_${event.request_id.trim()}`
              : `artifact_ready_${crypto.randomUUID()}`
          enqueueArtifactReadyNotification({
            id: `artifact_ready_${messageId}`,
            messageId,
            requestId: typeof event.request_id === 'string' ? event.request_id : null,
            sourceId: typeof event.source_id === 'string' ? event.source_id : null,
            sessionId: typeof event.session_id === 'string' ? event.session_id : null,
            channel: typeof event.channel === 'string' ? event.channel : null,
            createdAt: new Date().toISOString(),
            artifacts: producedArtifacts,
          })
        }
        setIsStreaming(false)
        clearActiveStreamingRefs()
        return
      }

      // Cross-channel sync: messages from WhatsApp/Telegram arriving while desktop is open
      if (eventType === 'crosschannel.message') {
        const role = String(event.role || '').trim()
        if (role !== 'user' && role !== 'assistant') return
        const content = String(event.content || '').trim()
        const producedArtifacts = role === 'assistant'
          ? normalizeProducedArtifacts((event as any).produced_artifacts)
          : undefined
        const responseBlocks = role === 'assistant'
          ? normalizeResponseBlocks((event as any).response_blocks)
          : undefined
        if (!content && (!producedArtifacts || producedArtifacts.length === 0) && (!responseBlocks || responseBlocks.length === 0)) return
        const eventSessionId = typeof event.session_id === 'string' ? event.session_id : null

        // If the session rolled over, show a divider and clear old messages
        if (eventSessionId && activeSessionIdRef.current && eventSessionId !== activeSessionIdRef.current) {
          setActiveSessionId(eventSessionId)
          setMessages([
            {
              id: `session-rollover-${eventSessionId}`,
              role: 'assistant' as const,
              content: '',
              channel: '__session_rollover__',
            },
          ])
        }

        setMessages((prev) => {
          const eventMessageId = typeof event.message_id === 'string' ? event.message_id.trim() : ''
          const newMsg: Message = {
            id: eventMessageId || `xchan-${crypto.randomUUID()}`,
            role: role as 'user' | 'assistant',
            content,
            attachments: role === 'user'
              ? (extractMessageAttachments({
                  attachments: event.attachments,
                  input_artifacts: event.input_artifacts,
                }))
              : undefined,
            producedArtifacts: role === 'assistant'
              ? producedArtifacts
              : undefined,
            responseBlocks: role === 'assistant'
              ? responseBlocks
              : undefined,
            channel: typeof event.channel === 'string' ? event.channel : null,
            sources: role === 'assistant' && Array.isArray(event.sources) ? event.sources : undefined,
            thinking: role === 'assistant' && typeof event.thinking_text === 'string' ? event.thinking_text : undefined,
          }
          return [...prev, newMsg]
        })
        if (role === 'assistant' && producedArtifacts && producedArtifacts.length > 0 && isProducedArtifactChatInactive()) {
          const messageId = typeof event.message_id === 'string' && event.message_id.trim()
            ? event.message_id.trim()
            : `xchan_${crypto.randomUUID()}`
          enqueueArtifactReadyNotification({
            id: `artifact_ready_${messageId}`,
            messageId,
            requestId: typeof event.request_id === 'string' ? event.request_id : null,
            sourceId: typeof event.source_id === 'string' ? event.source_id : null,
            sessionId: eventSessionId,
            channel: typeof event.channel === 'string' ? event.channel : null,
            createdAt: new Date().toISOString(),
            artifacts: producedArtifacts,
          })
        }
        return
      }

      if (eventType === 'task.failed') {
        setIsStreaming(false)
        clearActiveStreamingRefs()
        setStreamingProgress('')
        if (event.task_id) {
          removePendingTaskInputsForTask(String(event.task_id))
        }
        const message = String(event?.error?.message || event?.message || 'Opus task failed.')
        setMessages((prev) => {
          const { messages: nextMessages, messageId } = ensureAssistantMessageForEvent(prev, event)
          return nextMessages.map((item) => {
            if (item.id !== messageId) {
              return item
            }
            const existingContent = String(item.content || '').trim()
            return {
              ...item,
              content: existingContent ? item.content : message,
              progress: undefined,
            }
          })
        })
        forgetAssistantMessageBindings(event)
        return
      }

      if (eventType === 'task.completed' || eventType === 'task.cancelled') {
        setIsStreaming(false)
        clearActiveStreamingRefs()
        setStreamingProgress('')
        if (event.task_id) {
          removePendingTaskInputsForTask(String(event.task_id))
        }
        const messageId = findAssistantMessageIdForEvent(event)
        const boundMessage = messageId
          ? messagesRef.current.find((item) => item.id === messageId)
          : null
        const shouldRefreshFromHistory =
          eventType === 'task.completed' &&
          (
            !boundMessage ||
            !String(boundMessage.content || '').trim() ||
            !Array.isArray(boundMessage.producedArtifacts)
          )
        forgetAssistantMessageBindings(event)
        if (eventType === 'task.cancelled' && messageId && boundMessage && !String(boundMessage.content || '').trim() && !String(boundMessage.thinking || '').trim()) {
          setMessages((prev) => prev.filter((item) => item.id !== messageId))
          return
        }
        if (eventType === 'task.cancelled' && messageId) {
          setMessages((prev) => prev.map((item) => {
            if (item.id !== messageId) {
              return item
            }
            if (!String(item.content || '').trim() && !String(item.thinking || '').trim()) {
              return item
            }
            return {
              ...item,
              progress: undefined,
              stopped: true,
            }
          }))
          return
        }
        if (shouldRefreshFromHistory) {
          window.setTimeout(() => {
            void refreshSessionFromGateway(typeof event.session_id === 'string' ? event.session_id : null)
          }, 180)
        }
        return
      }

      if (eventType === 'error') {
        setIsStreaming(false)
        clearActiveStreamingRefs()
        setStreamingProgress('')
        if (event.message) {
          setMessages((prev) => {
            const { messages: nextMessages, messageId } = ensureAssistantMessageForEvent(prev, event)
            return nextMessages.map((item) => {
              if (item.id !== messageId) {
                return item
              }
              const existingContent = String(item.content || '').trim()
              return {
                ...item,
                content: existingContent ? item.content : String(event.message),
                progress: undefined,
              }
            })
          })
        }
      }
    })

    const offStatus = window.cosmic?.onGatewayStatus((status) => {
      if (!status) return
      setGatewayStatus(status)
      if (typeof status?.sessionId === 'string') {
        setActiveSessionId(status.sessionId)
      }
      if (status?.state === 'error' || status?.state === 'idle') {
        setIsStreaming(false)
        setStreamingProgress('')
        clearActiveStreamingRefs()
      }
    })

    return () => { offEvent?.(); offStatus?.() }
  }, [])

  useEffect(() => {
    const shouldFocusComposer =
      mode === 'chat' || (mode === 'task' && pendingTaskCount === 0)
    if (!isStreaming && searchState === 'visible' && !showLauncherTray && mode !== 'meeting' && shouldFocusComposer) {
      setTimeout(() => inputRef.current?.focus(), 10)
    }
  }, [isStreaming, mode, pendingTaskCount, searchState, showLauncherTray])

  useEffect(() => {
    const isChatInactiveNow =
      searchState !== 'visible' ||
      mode !== 'chat' ||
      showLauncherTray
    if (!isChatInactiveNow && cronResultNotifications.length > 0) {
      clearCronResultNotifications()
    }
  }, [cronResultNotifications.length, mode, searchState, showLauncherTray])

  useEffect(() => {
    const isChatInactiveNow =
      searchState !== 'visible' ||
      mode !== 'chat' ||
      showLauncherTray
    if (!isChatInactiveNow && artifactReadyNotifications.length > 0) {
      clearArtifactReadyNotifications()
    }
  }, [artifactReadyNotifications.length, mode, searchState, showLauncherTray])

  useEffect(() => {
    if (authState !== 'authenticated') {
      resetDesktopVmSessionState('Desktop is signed out from your VM.')
      return
    }

    if (window.cosmic?.requestGatewayResume) {
      lastGatewayResumeRequestAtRef.current = Date.now()
      window.cosmic.requestGatewayResume().catch(() => { })
    }
  }, [authState])

  // --- ACTIONS ---
  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const target = e.target
    const next = target.value.trim().length > 0
    if (next !== inputHasTextRef.current) {
      inputHasTextRef.current = next
      setHasText(next)
    }
    target.style.height = 'auto'
    target.style.height = `${Math.min(target.scrollHeight, 120)}px`
  }

  const clearComposerInput = () => {
    if (inputRef.current) {
      inputRef.current.value = ''
      inputRef.current.style.height = '24px'
    }
    if (inputHasTextRef.current) {
      inputHasTextRef.current = false
      setHasText(false)
    }
  }

  const handlePickDocuments = async () => {
    if (!window.cosmic?.pickGatewayDocuments || isStreaming || authState !== 'authenticated') {
      return
    }
    try {
      const payload = await window.cosmic.pickGatewayDocuments()
      const picked = Array.isArray(payload?.documents) ? payload.documents : []
      if (picked.length === 0) {
        return
      }
      const byPath = new Map(pendingAttachments.map((item) => [item.filePath, item]))
      for (const item of picked) {
        const filePath = String(item?.filePath || '').trim()
        const filename = String(item?.filename || '').trim()
        if (!filePath || !filename) {
          continue
        }
        byPath.set(filePath, {
          filePath,
          filename,
          mimeType: String(item?.mimeType || '').trim(),
          sizeBytes: Number(item?.sizeBytes || 0),
        })
      }
      const next = Array.from(byPath.values())
      const imageCount = countImageAttachments(next)
      if (imageCount > MAX_IMAGE_ATTACHMENTS_PER_MESSAGE) {
        throw new Error(buildImageAttachmentLimitError(imageCount))
      }
      setPendingAttachments(next)
    } catch (error) {
      setMessages((prev) => [
        ...prev,
        {
          ...createAssistantMessage(),
          content: error instanceof Error ? error.message : 'Document selection failed.',
        },
      ])
    }
  }

  const handleRemoveAttachment = (filePath: string) => {
    setPendingAttachments((prev) => prev.filter((item) => item.filePath !== filePath))
  }

  const handleSubmit = () => {
    if (mode === 'meeting') return
    setIsInputFocused(false)
    if (inputRef.current) inputRef.current.blur()

    const textToSend = (inputRef.current?.value ?? '').trim()
    if ((!textToSend && pendingAttachments.length === 0) || isStreaming) return

    const messageAttachments = normalizeMessageAttachments(pendingAttachments)
    const displayText = textToSend || buildPendingAttachmentSummary(pendingAttachments)
    const requestId = `req_${crypto.randomUUID()}`
    const userMessage = createUserMessage(displayText, messageAttachments, { requestId })
    clearComposerInput()
    shouldAutoScrollRef.current = true

    setMessages(prev => [...prev, userMessage])
    if (authState !== 'authenticated') {
      setMessages(prev => [...prev, {
        ...createAssistantMessage(),
        content: "Please sign in to connect this desktop app to your VM."
      }])
      return
    }

    if (!gatewayStatus.connected) {
      setMessages(prev => [...prev, {
        ...createAssistantMessage(),
        content: gatewayStatus.detail || "The desktop app is not connected to your VM yet."
      }])
      return
    }

    const assistantMessageId = createAssistantMessageId()
    activeStreamingRequestIdRef.current = requestId
    activeStreamingTaskIdRef.current = null
    activeAssistantMessageByRequestRef.current.set(requestId, assistantMessageId)

    // Reserve the assistant slot before the first streaming event arrives.
    setMessages(prev => [
      ...prev,
      createAssistantMessage({ id: assistantMessageId }),
    ])

    setIsStreaming(true)
    setStreamingProgress('Working on your request...')
    if (!window.cosmic?.sendGatewayQuery) {
      setIsStreaming(false)
      setStreamingProgress('')
      activeAssistantMessageByRequestRef.current.delete(requestId)
      setMessages(prev => [...prev, {
        ...createAssistantMessage(),
        content: "Gateway chat support is unavailable in this desktop build."
      }])
      return
    }

    const effectiveRouteOverride =
      mode === 'task'
        ? 'opus'
        : selectedModel === 'cosmic'
          ? undefined
          : selectedModel

    window.cosmic.sendGatewayQuery({
      requestId,
      content: textToSend,
      conversationContext: buildConversationContext([...messages, userMessage]),
      routeOverride: effectiveRouteOverride,
      attachments: pendingAttachments,
    }).then((result) => {
      const confirmedRequestId = typeof result?.requestId === 'string' ? result.requestId.trim() : ''
      setPendingAttachments([])
      if (confirmedRequestId && confirmedRequestId !== requestId) {
        activeAssistantMessageByRequestRef.current.delete(requestId)
        activeAssistantMessageByRequestRef.current.set(confirmedRequestId, assistantMessageId)
        activeStreamingRequestIdRef.current = confirmedRequestId
      }
    }).catch((error: any) => {
      setIsStreaming(false)
      setStreamingProgress('')
      clearActiveStreamingRefs()
      activeAssistantMessageByRequestRef.current.delete(requestId)
      setMessages(prev => prev.map((message) => {
        if (message.id !== assistantMessageId) {
          return message
        }
        return {
          ...message,
          content: error?.message || "Unable to send the message to your VM.",
        }
      }))
    })
  }

  const handleTaskInputDraftChange = (inputRequestId: string, value: string) => {
    setTaskInputDrafts((prev) => ({
      ...prev,
      [inputRequestId]: value,
    }))
    setTaskInputErrors((prev) => {
      if (!(inputRequestId in prev)) {
        return prev
      }
      const next = { ...prev }
      delete next[inputRequestId]
      return next
    })
  }

  const submitTaskInputReply = async (taskInput: PendingTaskInput, explicitContent?: string) => {
    const content = String(explicitContent ?? taskInputDrafts[taskInput.inputRequestId] ?? '').trim()
    if (!content) {
      setTaskInputErrors((prev) => ({
        ...prev,
        [taskInput.inputRequestId]: 'Reply required',
      }))
      return
    }
    if (!window.cosmic?.submitGatewayTaskInputReply) {
      setTaskInputErrors((prev) => ({
        ...prev,
        [taskInput.inputRequestId]: 'Task replies are unavailable in this desktop build.',
      }))
      return
    }
    setSubmittingTaskInputs((prev) => ({
      ...prev,
      [taskInput.inputRequestId]: true,
    }))
    setTaskInputErrors((prev) => {
      if (!(taskInput.inputRequestId in prev)) {
        return prev
      }
      const next = { ...prev }
      delete next[taskInput.inputRequestId]
      return next
    })
    try {
      await window.cosmic.submitGatewayTaskInputReply({
        inputRequestId: taskInput.inputRequestId,
        taskId: taskInput.taskId,
        content,
      })
      setTaskInputDrafts((prev) => ({
        ...prev,
        [taskInput.inputRequestId]: content,
      }))
    } catch (error: any) {
      setSubmittingTaskInputs((prev) => {
        const next = { ...prev }
        delete next[taskInput.inputRequestId]
        return next
      })
      setTaskInputErrors((prev) => ({
        ...prev,
        [taskInput.inputRequestId]: error?.message || 'Unable to send task reply.',
      }))
    }
  }

  const handleMoveActiveToBackground = async () => {
    const requestId = String(activeStreamingRequestIdRef.current || '').trim()
    if (!requestId || !window.cosmic?.backgroundGatewayRequest) {
      return
    }
    setBackgroundingRequestId(requestId)
    setBackgroundTaskErrors((prev) => {
      if (!(requestId in prev)) {
        return prev
      }
      const next = { ...prev }
      delete next[requestId]
      return next
    })
    try {
      await window.cosmic.backgroundGatewayRequest({ requestId })
      shouldAutoScrollRef.current = true
      showChatComposer()
    } catch (error: any) {
      setBackgroundingRequestId((current) => (current === requestId ? null : current))
      setBackgroundTaskErrors((prev) => ({
        ...prev,
        [requestId]: error?.message || 'Unable to move this response to the background.',
      }))
    }
  }

  const handleBringTaskToForeground = async (task: BackgroundTask) => {
    const requestId = String(task.requestId || '').trim()
    if (!requestId) {
      return
    }
    if (task.completed) {
      markRequestForegrounded(requestId)
      // Create the assistant message from the background task's stored content
      // (the original assistant message was removed when the task was backgrounded)
      const messageId = createAssistantMessageId()
      setMessages((prev) => {
        const nextMessages = prev.filter((message) => (
          message.role !== 'assistant' || message.requestId !== requestId
        ))
        return [
          ...nextMessages,
          createAssistantMessage({
            id: messageId,
            requestId,
            content: task.partialContent || '',
            thinking: task.partialThinking || '',
            activity: task.activity,
            activityLog: task.activityLog,
            alphaTerminalLog: task.alphaTerminalLog,
            progress: task.progress,
            producedArtifacts: task.producedArtifacts,
            sources: task.sources,
          }),
        ]
      })
      removeBackgroundTask(requestId)
      setSelectedBackgroundRequestId(null)
      patchMessagesForRequest(requestId, (message) => {
        const next = { ...message }
        delete next.backgroundState
        return next
      })
      shouldAutoScrollRef.current = true
      showChatComposer()
      // Fire-and-forget: tell backend to clear the background flag for persistence
      window.cosmic?.foregroundGatewayRequest?.({ requestId })?.catch?.(() => {})
      window.setTimeout(() => {
        responseEndRef.current?.scrollIntoView({ behavior: 'smooth' })
      }, 120)
      return
    }
    if (!window.cosmic?.foregroundGatewayRequest) {
      return
    }
    setForegroundingRequestId(requestId)
    setBackgroundTaskErrors((prev) => {
      if (!(requestId in prev)) {
        return prev
      }
      const next = { ...prev }
      delete next[requestId]
      return next
    })
    try {
      await window.cosmic.foregroundGatewayRequest({ requestId })
      shouldAutoScrollRef.current = true
      showChatComposer()
      setSelectedBackgroundRequestId(null)
    } catch (error: any) {
      setForegroundingRequestId((current) => (current === requestId ? null : current))
      setBackgroundTaskErrors((prev) => ({
        ...prev,
        [requestId]: error?.message || 'Unable to bring this task back to the foreground.',
      }))
    }
  }

  const findActiveAlphaTaskId = () => {
    const activeRequestId = String(activeStreamingRequestIdRef.current || '').trim()
    const activeTaskId = String(activeStreamingTaskIdRef.current || '').trim()
    for (let index = messagesRef.current.length - 1; index >= 0; index -= 1) {
      const message = messagesRef.current[index]
      if (message.role !== 'assistant') {
        continue
      }
      const messageMatchesActiveStream = (
        (activeRequestId && message.requestId === activeRequestId) ||
        (activeTaskId && message.sourceId === activeTaskId) ||
        (!activeRequestId && !activeTaskId)
      )
      if (!messageMatchesActiveStream) {
        continue
      }
      const view = buildAlphaConsoleView(message.activityLog, message.alphaTerminalLog, { stopped: message.stopped })
      if (view?.status === 'running' && view.taskId) {
        return view.taskId
      }
    }
    return null
  }

  const handleStopStreaming = () => {
    const requestId = activeStreamingRequestIdRef.current
    const taskId = activeStreamingTaskIdRef.current
    const activeAlphaTaskId = findActiveAlphaTaskId()
    if (!requestId && !taskId && !activeAlphaTaskId) {
      return
    }
    if (requestId || taskId) {
      const cancelPromise = window.cosmic?.cancelGatewayResponse?.({
        requestId: requestId || undefined,
        taskId: taskId || undefined,
      })
      cancelPromise?.catch(() => { })
    }
    if (activeAlphaTaskId && activeAlphaTaskId !== taskId) {
      window.cosmic?.cancelGatewayResponse?.({ taskId: activeAlphaTaskId })?.catch(() => { })
    }
  }

  const handleStopAlphaAgent = (payload: { requestId?: string; taskId?: string }) => {
    const taskId = String(payload.taskId || '').trim()
    const requestId = String(payload.requestId || '').trim()
    if (!taskId && !requestId) {
      return
    }
    window.cosmic?.cancelGatewayResponse?.({
      taskId: taskId || undefined,
      requestId: taskId ? undefined : requestId || undefined,
    })?.catch(() => { })
  }

  const handleCancelBackgroundTask = (task: BackgroundTask) => {
    const requestId = String(task.requestId || '').trim()
    const taskId = String(task.taskId || '').trim()
    if (!requestId && !taskId) return
    const cancelPromise = window.cosmic?.cancelGatewayResponse?.({
      requestId: requestId || undefined,
      taskId: taskId || undefined,
    })
    cancelPromise?.catch(() => { })
  }

  const handleShowLauncherTray = () => {
    hideHoverTooltip()
    setMode('chat')
    setShowLauncherTray(true)
    setIsInputFocused(false)
    if (inputRef.current) {
      inputRef.current.blur()
    }
  }

  const handleLauncherTileClick = (tile: LauncherTileId, event: React.MouseEvent<HTMLButtonElement>) => {
    hideHoverTooltip()
    const rect = event.currentTarget.getBoundingClientRect()
    const originX = rect.left + rect.width / 2
    const originY = rect.top + rect.height / 2
    startSurfaceLaunch(tile === 'meeting' ? 'meeting' : tile === 'spaces' ? 'spaces' : 'chat', originX, originY)
    if (tile === 'meeting') {
      showMeetingSurface()
      return
    }
    if (tile === 'spaces') {
      showSpacesSurface()
      return
    }
    if (tile === 'task') {
      showTaskSurface({ focusComposer: pendingTaskInputs.length === 0 })
      return
    }
    showChatComposer()
  }

  const handleModelDialScroll = () => {
    hideHoverTooltip()
    if (modelDialSettleTimeoutRef.current !== null) {
      window.clearTimeout(modelDialSettleTimeoutRef.current)
    }
    modelDialSettleTimeoutRef.current = window.setTimeout(() => {
      const nextModel = getCenteredModelFromDial()
      if (!nextModel) {
        return
      }
      commitSelectedModel(nextModel, true, true)
      scrollModelDialTo(nextModel, 'smooth')
    }, 140)
  }

  const handleModelDialWheel = (event: React.WheelEvent<HTMLDivElement>) => {
    const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY
    if (Math.abs(delta) < 4) {
      return
    }
    event.preventDefault()
    hideHoverTooltip()
    const now = Date.now()
    if (now < modelDialWheelLockUntilRef.current) {
      return
    }
    modelDialWheelLockUntilRef.current = now + 180
    stepModelDial(delta > 0 ? 1 : -1)
  }

  const handleModelDialFocus = (model: GatewayModelSelection) => {
    commitSelectedModel(model, true, true)
    scrollModelDialTo(model, 'smooth')
  }

  const handleVmLogout = async () => {
    hideHoverTooltip()
    const requestId = activeStreamingRequestIdRef.current
    const taskId = activeStreamingTaskIdRef.current
    if (requestId || taskId) {
      try {
        await window.cosmic?.cancelGatewayResponse?.({
          requestId: requestId || undefined,
          taskId: taskId || undefined,
        })
      } catch {
        // Best-effort cancellation before transport teardown.
      }
    }

    try {
      await window.cosmic?.logout?.()
    } catch {
      // Local UI still needs to reset even if IPC logout fails.
    }

    setAuthState('unauthenticated')
    setAuthData(null)
    resetDesktopVmSessionState('Signed out from your VM.')
  }

  const handleCopy = async (text: string, id: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopiedId(id)
      setTimeout(() => setCopiedId(null), 2000)
    } catch (err) {
      console.error('Failed to copy:', err)
    }
  }

  const handleDownloadProducedArtifact = async (messageId: string, artifact: ProducedArtifact) => {
    if (!messageId || !artifact?.artifactId || !window.cosmic?.downloadGatewayOutputArtifact) {
      return null
    }
    setDownloadingArtifactId(artifact.artifactId)
    try {
      return await window.cosmic.downloadGatewayOutputArtifact({
        messageId,
        artifactId: artifact.artifactId,
        suggestedFilename: artifact.filename,
        mimeType: artifact.mimeType || undefined,
      })
    } catch (err) {
      console.error('Failed to download produced artifact:', err)
      return {
        cancelled: false,
        error: err instanceof Error ? err.message : String(err || 'Download failed'),
      }
    } finally {
      setDownloadingArtifactId((current) => (current === artifact.artifactId ? null : current))
    }
  }

  const scrollToBottom = () => {
    responseEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }

  const handleScroll = () => {
    if (!responseContainerRef.current) return
    const { scrollTop, scrollHeight, clientHeight } = responseContainerRef.current
    const distanceFromBottom = scrollHeight - scrollTop - clientHeight
    const isNearBottom = distanceFromBottom < 50
    shouldAutoScrollRef.current = isNearBottom
    setShowScrollButton(!isNearBottom && messages.length > 1)
  }

  useEffect(() => {
    if (shouldAutoScrollRef.current) {
      responseEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages, isStreaming])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (searchState === 'visible') {
          window.cosmic?.hide()
        }
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [searchState])

  useEffect(() => {
    const handleResize = () => {
      setViewportSize({
        width: window.innerWidth,
        height: window.innerHeight,
      })
    }
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  // When the OS window moves between displays (different DPI / workArea), the `resize`
  // event alone is unreliable — CSS pixel dimensions can match while scaleFactor differs.
  // The main process emits `cosmic:display-changed` after bounds settle in the new DPI
  // context; we force a viewport re-read and fire a synthetic resize so ResizeObservers
  // and other resize listeners re-measure.
  useEffect(() => {
    const ipc = (window as any).ipcRenderer
    if (!ipc?.on) return
    const handler = () => {
      requestAnimationFrame(() => {
        setViewportSize({ width: window.innerWidth, height: window.innerHeight })
        window.dispatchEvent(new Event('resize'))
      })
    }
    ipc.on('cosmic:display-changed', handler)
    return () => { ipc.off?.('cosmic:display-changed', handler) }
  }, [])

  const activeDocsProgressMessage = useMemo(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index]
      if (message.role !== 'assistant') {
        continue
      }
      if (message.progress?.kind === 'docs_parse' && !String(message.content || '').trim()) {
        return message
      }
    }
    return null
  }, [messages])

  // Render Classes
  const shouldShowTaskInterrupt =
    visibleTaskInterrupts.length > 0 &&
    mode !== 'task' &&
    mode !== 'meeting' &&
    mode !== 'spaces' &&
    !showLauncherTray
  const shouldShowPrimarySurface = mode !== 'meeting' && mode !== 'spaces' && !showLauncherTray
  const shouldShowResponseSurface = shouldShowPrimarySurface && (mode === 'task' || messages.length > 0)
  const taskRailLayout = useMemo(() => {
    if (!shouldShowTaskInterrupt) {
      return null
    }

    const edgePadding = viewportSize.width >= 1440 ? 32 : 24
    const railGap = 20
    const preferredRailWidth = viewportSize.width >= 1380 ? 360 : 328
    const minimumRailWidth = viewportSize.width < 920 ? 228 : 252
    const minimumMainWidth = shouldShowResponseSurface ? 420 : 360

    const railWidth = Math.min(
      preferredRailWidth,
      Math.max(
        minimumRailWidth,
        viewportSize.width - (minimumMainWidth + edgePadding * 2 + railGap),
      ),
    )
    const reserve = railWidth + railGap + edgePadding
    const compact = railWidth < 286
    const top = viewportSize.height < 820 ? 176 : 200

    return {
      edgePadding,
      railWidth,
      reserve,
      compact,
      top,
    }
  }, [shouldShowTaskInterrupt, shouldShowResponseSurface, viewportSize.height, viewportSize.width])
  const shouldShowCronResultSurface =
    orderedCronResultNotifications.length > 0 &&
    (
      searchState !== 'visible' ||
      mode !== 'chat' ||
      showLauncherTray
    )
  const cronResultShellStyle = {
    ['--cron-result-bottom' as string]: searchState === 'visible' ? '112px' : '24px',
  } as React.CSSProperties
  const shouldShowArtifactReadySurface =
    orderedArtifactReadyNotifications.length > 0 &&
    (
      searchState !== 'visible' ||
      mode !== 'chat' ||
      showLauncherTray
    )
  const artifactReadyShellStyle = {
    ['--artifact-ready-bottom' as string]: shouldShowCronResultSurface
      ? (searchState === 'visible' ? '424px' : '336px')
      : (searchState === 'visible' ? '112px' : '24px'),
  } as React.CSSProperties
  const effectivePosition = mode === 'spaces' ? 'bottom' : (messages.length > 0 || mode === 'task') ? 'bottom' : searchPosition
  const overlayClass = [
    searchState === 'hidden' ? '' : 'visible',
    effectivePosition === 'middle' ? 'position-middle' : '',
    shouldShowResponseSurface ? 'has-response' : '',
    mode === 'spaces' ? 'spaces-active' : '',
    (isInputFocused || shouldShowResponseSurface || isStreaming || mode === 'meeting' || mode === 'spaces') ? 'focused' : ''
  ].join(' ')
  const composerLaunchClass = surfaceLaunch?.target === 'chat' ? 'launcher-expand' : ''
  const composerLaunchStyle = surfaceLaunch?.target === 'chat'
    ? ({
      ['--launch-offset-x' as string]: `${surfaceLaunch.composerOffsetX}px`,
      ['--launch-offset-y' as string]: `${surfaceLaunch.composerOffsetY}px`,
    } as React.CSSProperties)
    : undefined
  const responseLaunchClass = surfaceLaunch?.target === 'chat' ? 'launcher-expand' : ''
  const responseLaunchStyle = surfaceLaunch?.target === 'chat'
    ? ({
      ['--launch-offset-x' as string]: `${surfaceLaunch.responseOffsetX}px`,
      ['--launch-offset-y' as string]: `${surfaceLaunch.responseOffsetY}px`,
    } as React.CSSProperties)
    : undefined
  const meetingLaunchClass = surfaceLaunch?.target === 'meeting' ? 'launcher-expand' : ''
  const meetingLaunchStyle = surfaceLaunch?.target === 'meeting'
    ? ({
      ['--launch-offset-x' as string]: `${surfaceLaunch.meetingOffsetX}px`,
      ['--launch-offset-y' as string]: `${surfaceLaunch.meetingOffsetY}px`,
    } as React.CSSProperties)
    : undefined
  const spacesLaunchClass = surfaceLaunch?.target === 'spaces' ? 'launcher-expand' : ''
  const spacesLaunchStyle = surfaceLaunch?.target === 'spaces'
    ? ({
      ['--launch-offset-x' as string]: `${surfaceLaunch.spacesOffsetX}px`,
      ['--launch-offset-y' as string]: `${surfaceLaunch.spacesOffsetY}px`,
    } as React.CSSProperties)
    : undefined
  const overlayStyle = {
    pointerEvents: searchState === 'visible' ? 'auto' : 'none',
    ['--task-rail-reserve' as string]: taskRailLayout ? `${taskRailLayout.reserve}px` : '0px',
  } as React.CSSProperties
  const isMeetingSurfaceActive = mode === 'meeting' && searchState !== 'hidden'
  const isSpacesSurfaceActive = mode === 'spaces' && searchState !== 'hidden'
  const taskInterruptStyle = taskRailLayout
    ? ({
      ['--task-rail-width' as string]: `${taskRailLayout.railWidth}px`,
      ['--task-rail-edge' as string]: `${taskRailLayout.edgePadding}px`,
      ['--task-rail-top' as string]: `${taskRailLayout.top}px`,
    } as React.CSSProperties)
    : undefined
  const hasMultiplePendingTaskInputs = orderedPendingTaskInputs.length > 1
  const composerPlaceholder =
    authState !== 'authenticated'
      ? 'Sign in to connect Cosmic to your VM...'
      : gatewayStatus.connected
        ? 'Ask Cosmic...'
        : gatewayStatus.state === 'connecting' || gatewayStatus.state === 'reconnecting'
          ? 'Connecting to your VM...'
          : gatewayStatus.state === 'error'
            ? 'VM connection needs attention...'
            : 'Connect to your VM...'

  return (
    <>
      <DynamicIsland
        searchActive={searchState === 'visible'}
        hovered={isIslandHovered}
        debug={false}
        searchPosition={searchPosition}
        onPositionChange={(pos) => {
          setSearchPosition(pos)
          window.cosmic?.saveSetting('searchPosition', pos)
        }}
        staybackTime={staybackTime}
        onStaybackChange={(time) => {
          setStaybackTime(time)
          window.cosmic?.saveSetting('staybackTime', time)
        }}
        islandOpacity={islandOpacity}
        onOpacityChange={(val) => {
          setIslandOpacity(val)
          window.cosmic?.saveSetting('islandOpacity', val)
        }}
        keyStatus={keyStatus}
        authData={authData}
        gatewayConnection={gatewayStatus}
        onLogout={handleVmLogout}
        onOpenAgentEmailInbox={openAgentEmailInboxFromIsland}
        onOpenAgentEmailApprovals={openAgentEmailApprovalsFromIsland}
      />

      {authState === 'unauthenticated' && (
        <CosmicLoginModal
          onAuthenticated={(data) => {
            setAuthState('authenticated')
            setAuthData(data)
          }}
        />
      )}

      {shouldShowCronResultSurface && (
        <div className="cron-result-shell" style={cronResultShellStyle}>
          <div
            ref={cronResultStackRef}
            className="cron-result-stack"
            role="list"
            aria-label={`${orderedCronResultNotifications.length} scheduled result${orderedCronResultNotifications.length === 1 ? '' : 's'} ready`}
            onScroll={handleCronResultScroll}
          >
            {orderedCronResultNotifications.map((notification, index) => (
              <LiquidGlass
                key={notification.id}
                disableTilt={true}
                cornerRadius={30}
                className="task-interrupt-glass"
                style={{ width: '100%' }}
              >
                <div className="task-interrupt-card cron-result-card">
                  <div className="task-interrupt-head">
                    <div className="task-interrupt-title-cluster">
                      <div className="task-interrupt-logo-shell" aria-hidden="true">
                        <img
                          src={cosmicBallLogo}
                          alt=""
                          className="task-interrupt-logo"
                          draggable={false}
                        />
                      </div>
                      <div className="task-interrupt-copy">
                        <div className="task-interrupt-kicker">Scheduled result ready</div>
                        <div className="task-interrupt-meta">
                          {orderedCronResultNotifications.length > 1
                            ? `${index + 1} of ${orderedCronResultNotifications.length} waiting`
                            : 'Open chat to review the latest reminder result'}
                        </div>
                      </div>
                    </div>
                    <div className="task-interrupt-chip-row">
                      {orderedCronResultNotifications.length > 1 && (
                        <div className="task-interrupt-chip count">{orderedCronResultNotifications.length} waiting</div>
                      )}
                      <div className="task-interrupt-chip cron-result-chip">Reminder</div>
                    </div>
                  </div>
                  <div className="task-interrupt-preview cron-result-preview">{notification.content}</div>
                  <div className="task-interrupt-actions">
                    <button
                      type="button"
                      className="task-interrupt-btn secondary"
                      onClick={() => dismissCronResultNotification(notification.id)}
                    >
                      Later
                    </button>
                    <button
                      type="button"
                      className="task-interrupt-btn primary"
                      onClick={openChatFromCronNotification}
                    >
                      Open chat
                    </button>
                  </div>
                </div>
              </LiquidGlass>
            ))}
          </div>
          {orderedCronResultNotifications.length > 1 && (
            <div
              className="cron-result-dots"
              aria-label={`Scheduled result card ${cronResultIndex + 1} of ${orderedCronResultNotifications.length}`}
            >
              {orderedCronResultNotifications.map((notification, index) => (
                <button
                  key={notification.id}
                  type="button"
                  className={`task-interrupt-dot ${cronResultIndex === index ? 'active' : ''}`}
                  aria-label={`Show result ${index + 1}`}
                  aria-current={cronResultIndex === index}
                  onClick={() => {
                    const container = cronResultStackRef.current
                    if (!container) {
                      return
                    }
                    container.scrollTo({ left: container.clientWidth * index, behavior: 'smooth' })
                    setCronResultIndex(index)
                  }}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {shouldShowArtifactReadySurface && (
        <div className="artifact-ready-shell" style={artifactReadyShellStyle}>
          <div className="artifact-ready-stack" role="list" aria-label={`${orderedArtifactReadyNotifications.length} file result${orderedArtifactReadyNotifications.length === 1 ? '' : 's'} ready`}>
            {orderedArtifactReadyNotifications.map((notification) => {
              return (
                <LiquidGlass
                  key={notification.id}
                  disableTilt={true}
                  cornerRadius={30}
                  className="task-interrupt-glass"
                  style={{ width: '100%' }}
                >
                  <div className="task-interrupt-card artifact-ready-card">
                    <div className="task-interrupt-head">
                      <div className="task-interrupt-title-cluster">
                        <div className="task-interrupt-logo-shell" aria-hidden="true">
                          <img
                            src={cosmicBallLogo}
                            alt=""
                            className="task-interrupt-logo"
                            draggable={false}
                          />
                        </div>
                        <div className="task-interrupt-copy">
                          <div className="task-interrupt-kicker">File ready</div>
                          <div className="task-interrupt-meta">
                            {notification.artifacts.length === 1
                              ? 'A produced file is ready on your desktop'
                              : `${notification.artifacts.length} produced files are ready on your desktop`}
                          </div>
                        </div>
                      </div>
                      <div className="task-interrupt-chip-row">
                        {notification.channel && channelLabel(notification.channel) && (
                          <div className="task-interrupt-chip artifact-ready-chip">
                            {channelLabel(notification.channel)}
                          </div>
                        )}
                      </div>
                    </div>
                    <div className="task-interrupt-preview artifact-ready-preview">
                      <div className="artifact-ready-preview-title">
                        {formatProducedArtifactNotificationSummary(notification.artifacts)}
                      </div>
                      {notification.artifacts.length > 0 && (
                        <div className="artifact-ready-list" role="list">
                          {notification.artifacts.slice(0, 3).map((artifact) => (
                            <div key={artifact.artifactId} className="artifact-ready-item" role="listitem">
                              <span className="artifact-ready-item-name">{artifact.filename}</span>
                              <span className="artifact-ready-item-meta">
                                {formatProducedArtifactKind(artifact)}
                                {artifact.sizeBytes ? ` · ${formatAttachmentSize(artifact.sizeBytes)}` : ''}
                              </span>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                    <div className="task-interrupt-actions">
                      <button
                        type="button"
                        className="task-interrupt-btn secondary"
                        onClick={() => dismissArtifactReadyNotification(notification.id)}
                      >
                        Later
                      </button>
                      <button
                        type="button"
                        className="task-interrupt-btn primary"
                        onClick={openChatFromArtifactNotification}
                      >
                        Open chat
                      </button>
                    </div>
                  </div>
                </LiquidGlass>
              )
            })}
          </div>
        </div>
      )}

      <div
        className={`overlay ${overlayClass}`}
        onDoubleClick={(e) => {
          if (e.target === e.currentTarget) window.cosmic?.hide()
        }}
        style={overlayStyle}
      >
        <MeetingMode
          active={isMeetingSurfaceActive}
          keyStatus={keyStatus}
          onBackToChat={showChatComposer}
          containerRef={meetingSurfaceRef}
          containerClassName={meetingLaunchClass}
          containerStyle={meetingLaunchStyle}
        />

        <SpacesControlCenter
          active={isSpacesSurfaceActive}
          gatewayState={gatewayStatus.state}
          gatewayConnected={gatewayStatus.connected}
          gatewayDetail={gatewayStatus.detail}
          pendingTaskCount={pendingTaskCount}
          pendingCronCount={orderedCronResultNotifications.length}
          selectedModelLabel={MODEL_OPTIONS.find((item) => item.id === selectedModel)?.label || 'Cosmic'}
          onBackToChat={showChatComposer}
          onMinimize={handleShowLauncherTray}
          onClose={() => window.cosmic?.hide()}
          onShowTooltip={(label, el) => showHoverTooltipForElement(label, el, 'launcher')}
          onHideTooltip={hideHoverTooltip}
          containerRef={spacesSurfaceRef}
          containerClassName={spacesLaunchClass}
          containerStyle={spacesLaunchStyle}
          agentEmailNavigateInboxSignal={agentEmailInboxNavigateSignal}
          agentEmailNavigateInboxMailboxId={agentEmailInboxNavigateMailboxId}
          agentEmailNavigateApprovalsSignal={agentEmailApprovalsNavigateSignal}
          agentEmailNavigateApprovalsId={agentEmailApprovalsNavigateId}
        />

        {shouldShowTaskInterrupt && visibleTaskInterrupts.length > 0 && (
          <div className={`task-interrupt-shell ${taskRailLayout?.compact ? 'compact' : ''}`} style={taskInterruptStyle}>
            <div
              ref={taskInterruptStackRef}
              className="task-interrupt-stack"
              role="list"
              aria-label={`${visibleTaskInterrupts.length} task inputs waiting`}
              onScroll={handleTaskInterruptScroll}
            >
              {visibleTaskInterrupts.map((taskInput, index) => (
                <LiquidGlass
                  key={taskInput.inputRequestId}
                  disableTilt={true}
                  cornerRadius={30}
                  className="task-interrupt-glass"
                  style={{ width: '100%' }}
                >
                  <div className="task-interrupt-card">
                    <div className="task-interrupt-head">
                      <div className="task-interrupt-title-cluster">
                        <div className="task-interrupt-logo-shell" aria-hidden="true">
                          <img
                            src={cosmicBallLogo}
                            alt=""
                            className="task-interrupt-logo"
                            draggable={false}
                          />
                        </div>
                        <div className="task-interrupt-copy">
                          <div className="task-interrupt-kicker">Task needs your input</div>
                          <div className="task-interrupt-meta">
                            {visibleTaskInterrupts.length > 1
                              ? `${index + 1} of ${visibleTaskInterrupts.length} waiting`
                              : taskInput.options.length > 0
                                ? `${taskInput.options.length} quick choices in Task Inbox`
                                : 'Open Task Inbox to continue'}
                          </div>
                        </div>
                      </div>
                      <div className="task-interrupt-chip-row">
                        {visibleTaskInterrupts.length > 1 && (
                          <div className="task-interrupt-chip count">{visibleTaskInterrupts.length} waiting</div>
                        )}
                        <div className="task-interrupt-chip">Opus task</div>
                      </div>
                    </div>
                    <div className="task-interrupt-preview">{taskInput.question}</div>
                    <div className="task-interrupt-actions">
                      <button
                        type="button"
                        className="task-interrupt-btn secondary"
                        onClick={() => dismissTaskInterrupt(taskInput.inputRequestId)}
                      >
                        Later
                      </button>
                      <button
                        type="button"
                        className="task-interrupt-btn primary"
                        onClick={() => {
                          setDismissedTaskInterruptIds([])
                          showTaskSurface({
                            focusComposer: false,
                            focusInputRequestId: taskInput.inputRequestId,
                          })
                        }}
                      >
                        Reply
                      </button>
                    </div>
                  </div>
                </LiquidGlass>
              ))}
            </div>
            {visibleTaskInterrupts.length > 1 && (
              <div
                className="task-interrupt-dots"
                aria-label={`Task card ${taskInterruptIndex + 1} of ${visibleTaskInterrupts.length}`}
              >
                {visibleTaskInterrupts.map((taskInput, index) => (
                  <button
                    key={taskInput.inputRequestId}
                    type="button"
                    className={`task-interrupt-dot ${taskInterruptIndex === index ? 'active' : ''}`}
                    aria-label={`Show task ${index + 1}`}
                    aria-current={taskInterruptIndex === index}
                    onClick={() => {
                      const container = taskInterruptStackRef.current
                      if (!container) {
                        return
                      }
                      container.scrollTo({ left: container.clientWidth * index, behavior: 'smooth' })
                      setTaskInterruptIndex(index)
                    }}
                  />
                ))}
              </div>
            )}
          </div>
        )}

        {/* PRIMARY CONTENT AREA */}
        {shouldShowResponseSurface && (
          <div
            ref={chatResponseSurfaceRef}
            className={`response-container ${searchState === 'visible' ? 'visible' : ''} ${responseLaunchClass}`}
            style={responseLaunchStyle}
          >
            <LiquidGlass disableTilt={true} cornerRadius={32} style={{ width: '100%', height: '100%' }}>
              <div className="response-wrapper">
                <div className="response-content" style={{ paddingTop: 24 }} ref={responseContainerRef} onScroll={handleScroll}>
                  {mode === 'task' && (
                    <div className="task-hub">
                      <div className="task-hub-header">
                        <div>
                          <div className="task-hub-kicker">Task Inbox</div>
                          <h2 className="task-hub-title">
                            {taskDashboardCount > 0
                              ? `${taskDashboardCount} task item${taskDashboardCount === 1 ? '' : 's'} active`
                              : 'No task activity right now'}
                          </h2>
                          <div className="task-hub-subtitle">
                            {orderedBackgroundTasks.length > 0
                              ? `${orderedBackgroundTasks.length} background task${orderedBackgroundTasks.length === 1 ? '' : 's'} running or ready`
                              : 'Background work you move out of chat will collect here.'}
                          </div>
                        </div>
                        <button type="button" className="task-hub-chat-btn" onClick={showChatComposer}>
                          Return to chat
                        </button>
                      </div>

                      {taskDashboardCount > 0 ? (
                        <div className="task-hub-stack">
                          {orderedBackgroundTasks.length > 0 && selectedBackgroundTask && (
                            <div className={`task-hub-section ${orderedBackgroundTasks.length > 1 ? '' : 'single'}`}>
                                <div className="task-hub-section-header">
                                  <div className="task-hub-section-kicker">Background tasks</div>
                                  <div className="task-hub-section-meta">
                                  {orderedBackgroundTasks.length} running or completed
                                  </div>
                                </div>
                              <div
                                className={`task-hub-body ${orderedBackgroundTasks.length > 1 ? '' : 'single'} ${
                                  orderedBackgroundTasks.length > 1 && backgroundTaskListRetracted ? 'task-list-retracted' : ''
                                }`}
                              >
                                {orderedBackgroundTasks.length > 1 && (
                                  <div className="task-list-pane">
                                    <div className="task-list">
                                      {orderedBackgroundTasks.map((task) => (
                                        <button
                                          key={task.requestId}
                                          type="button"
                                          className={`task-list-item ${selectedBackgroundTask.requestId === task.requestId ? 'active' : ''}`}
                                          onClick={() => setSelectedBackgroundRequestId(task.requestId)}
                                        >
                                          <div className="task-list-item-label-row">
                                            <span className="task-list-item-label">{task.route || 'Background'}</span>
                                            <span className="task-list-item-status">
                                              {task.completed ? (task.failed ? 'Failed' : 'Ready') : 'Streaming'}
                                            </span>
                                          </div>
                                          <div className="task-list-item-question">
                                            {task.userQueryExcerpt || 'Background task'}
                                          </div>
                                          <div className="task-list-item-meta">
                                            {task.completed
                                              ? (task.failed ? 'Needs attention' : 'Completed · ready to move to chat')
                                              : (task.activity || task.progress?.label || 'Working in the background...')}
                                          </div>
                                        </button>
                                      ))}
                                    </div>
                                  </div>
                                )}
                                <div className="task-detail-pane">
                                  {orderedBackgroundTasks.length > 1 && (
                                    <button
                                      type="button"
                                      className={`task-inbox-list-toggle ${backgroundTaskListRetracted ? 'is-retracted' : ''}`}
                                      onClick={() => setBackgroundTaskListRetracted((v) => !v)}
                                      aria-expanded={!backgroundTaskListRetracted}
                                      aria-label={backgroundTaskListRetracted ? 'Show background task list' : 'Hide background task list'}
                                    >
                                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                                        {backgroundTaskListRetracted ? (
                                          <path d="M9 18l6-6-6-6" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
                                        ) : (
                                          <path d="M15 18l-6-6 6-6" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
                                        )}
                                      </svg>
                                    </button>
                                  )}
                                  {(() => {
                                    const task = selectedBackgroundTask
                                    const errorText = backgroundTaskErrors[task.requestId]
                                    const backgroundMessageId = messages.find((message) => (
                                      message.role === 'assistant' &&
                                      message.requestId === task.requestId &&
                                      Array.isArray(message.producedArtifacts) &&
                                      message.producedArtifacts.length > 0
                                    ))?.id || ''
                                    return (
                                      <div className="task-card task-detail-card">
                                        <div className="task-card-topline">
                                          <div className="task-card-topline-meta">
                                            <span className="task-card-badge background">
                                              {task.completed ? (task.failed ? 'Failed' : 'Ready') : 'Background'}
                                            </span>
                                            <span className="task-card-meta">{task.taskId || task.requestId}</span>
                                          </div>
                                          <div className="task-card-top-actions">
                                            {!task.completed && (
                                              <button
                                                type="button"
                                                className="task-action-btn task-cancel-btn"
                                                onClick={() => handleCancelBackgroundTask(task)}
                                                aria-label="Cancel background task"
                                              >
                                                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                                                  <path d="M18 6L6 18M6 6l12 12" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
                                                </svg>
                                                <span>Cancel</span>
                                              </button>
                                            )}
                                            {!task.failed && (
                                              <button
                                                type="button"
                                                className="task-submit-btn task-foreground-btn task-top-foreground-btn"
                                                disabled={!task.completed && (!canBringBackgroundTaskToForeground || foregroundingRequestId === task.requestId)}
                                                onClick={() => void handleBringTaskToForeground(task)}
                                              >
                                                <img src={bringToForegroundIcon} alt="" aria-hidden="true" className="task-icon-image invert" />
                                                <span>{task.completed ? 'Move to chat' : foregroundingRequestId === task.requestId ? 'Bringing back...' : 'Bring to foreground'}</span>
                                              </button>
                                            )}
                                          </div>
                                        </div>
                                        <div className="task-card-question task-detail-question">
                                          {task.userQueryExcerpt || 'Background task'}
                                        </div>
                                        <div className="task-detail-copy">
                                          {task.completed
                                            ? 'This task finished outside the main chat surface. You can review it here or bring future background work back to the foreground.'
                                            : 'Streaming continues here while you work on something else in chat.'}
                                        </div>
                                        {task.progress?.kind === 'docs_parse' && !String(task.partialContent || '').trim() && (
                                          <DocsProgressCard progress={task.progress} />
                                        )}
                                        {task.progress?.kind === 'tabular_parse' && !String(task.partialContent || '').trim() && (
                                          <TabularProgressCard progress={task.progress} />
                                        )}
                                        {task.activity && task.progress?.kind !== 'docs_parse' && task.progress?.kind !== 'tabular_parse' && (
                                          <div className="assistant-activity task-background-activity">{task.activity}</div>
                                        )}
                                        {task.partialThinking && (
                                          <div className="thinking-block task-thinking-block">
                                            <div className="thinking-label">Thinking</div>
                                            <div className="thinking-text">{task.partialThinking}</div>
                                          </div>
                                        )}
                                        <AssistantFlowTimeline entries={task.activityLog} />
                                        {String(task.partialContent || '').trim() ? (
                                          <div className="task-background-preview">
                                            <ReactMarkdown
                                              remarkPlugins={[remarkGfm, remarkMath]}
                                              rehypePlugins={[rehypeKatex]}
                                              components={assistantMarkdownComponents}
                                            >
                                              {task.partialContent}
                                            </ReactMarkdown>
                                          </div>
                                        ) : (
                                          <div className="task-background-placeholder">
                                            {task.failed
                                              ? (task.error || 'This background task failed.')
                                              : 'Streaming will appear here as this background task progresses.'}
                                          </div>
                                        )}
                                        {backgroundMessageId && (
                                          <AssistantProducedArtifacts
                                            messageId={backgroundMessageId}
                                            artifacts={task.producedArtifacts}
                                            downloadingArtifactId={downloadingArtifactId}
                                            onDownload={handleDownloadProducedArtifact}
                                          />
                                        )}
                                        <div className="task-card-footer">
                                          <div className="task-card-status">
                                            {errorText ? (
                                              <span className="task-card-error">{errorText}</span>
                                            ) : task.failed ? (
                                              <span className="task-card-error">{task.error || 'Background task failed.'}</span>
                                            ) : task.completed ? (
                                              <span className="task-card-hint">Ready · Bring it back to chat to see the result in context.</span>
                                            ) : canBringBackgroundTaskToForeground ? (
                                              <span className="task-card-hint">Streaming in the background — bring it back to watch live.</span>
                                            ) : (
                                              <span className="task-card-hint">Finish or background the current foreground stream before bringing another task back.</span>
                                            )}
                                          </div>
                                        </div>
                                      </div>
                                    )
                                  })()}
                                </div>
                              </div>
                            </div>
                          )}

                          {pendingTaskCount > 0 && selectedTaskInput && (
                            <div className={`task-hub-section ${hasMultiplePendingTaskInputs ? '' : 'single'}`}>
                              <div className="task-hub-section-header">
                                <div className="task-hub-section-kicker">Needs your input</div>
                                <div className="task-hub-section-meta">
                                  {pendingTaskCount} waiting
                                </div>
                              </div>
                              <div
                                className={`task-hub-body ${hasMultiplePendingTaskInputs ? '' : 'single'} ${
                                  hasMultiplePendingTaskInputs && pendingTaskListRetracted ? 'task-list-retracted' : ''
                                }`}
                              >
                                {hasMultiplePendingTaskInputs && (
                                  <div className="task-list-pane">
                                    <div className="task-list">
                                      {orderedPendingTaskInputs.map((taskInput) => (
                                        <button
                                          key={taskInput.inputRequestId}
                                          type="button"
                                          className={`task-list-item ${selectedTaskInput.inputRequestId === taskInput.inputRequestId ? 'active' : ''}`}
                                          onClick={() => setSelectedTaskInputId(taskInput.inputRequestId)}
                                        >
                                          <div className="task-list-item-label-row">
                                            <span className="task-list-item-label">Task</span>
                                            <span className="task-list-item-status">
                                              {taskInput.options.length > 0
                                                ? `${taskInput.options.length} choice${taskInput.options.length === 1 ? '' : 's'}`
                                                : 'Reply needed'}
                                            </span>
                                          </div>
                                          <div className="task-list-item-question">{taskInput.question}</div>
                                        </button>
                                      ))}
                                    </div>
                                  </div>
                                )}

                                <div className="task-detail-pane">
                                  {hasMultiplePendingTaskInputs && (
                                    <button
                                      type="button"
                                      className={`task-inbox-list-toggle ${pendingTaskListRetracted ? 'is-retracted' : ''}`}
                                      onClick={() => setPendingTaskListRetracted((v) => !v)}
                                      aria-expanded={!pendingTaskListRetracted}
                                      aria-label={pendingTaskListRetracted ? 'Show task input list' : 'Hide task input list'}
                                    >
                                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                                        {pendingTaskListRetracted ? (
                                          <path d="M9 18l6-6-6-6" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
                                        ) : (
                                          <path d="M15 18l-6-6 6-6" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
                                        )}
                                      </svg>
                                    </button>
                                  )}
                                  {(() => {
                                    const taskInput = selectedTaskInput
                                    const isSubmitting = Boolean(submittingTaskInputs[taskInput.inputRequestId])
                                    const draftValue = taskInputDrafts[taskInput.inputRequestId] || ''
                                    const errorText = taskInputErrors[taskInput.inputRequestId]

                                    return (
                                      <div className="task-card task-detail-card">
                                        <div className="task-card-topline">
                                          <span className="task-card-badge">Awaiting input</span>
                                          <span className="task-card-meta">{taskInput.taskId}</span>
                                        </div>
                                        <div className="task-card-question task-detail-question">{taskInput.question}</div>
                                        <div className="task-detail-copy">
                                          Choose a quick option or send a custom reply to resume this task without disturbing your current conversation.
                                        </div>
                                        {taskInput.options.length > 0 && (
                                          <div className="task-card-options">
                                            {taskInput.options.map((option) => (
                                              <button
                                                key={option}
                                                type="button"
                                                className="task-option-chip"
                                                disabled={isSubmitting}
                                                onClick={() => void submitTaskInputReply(taskInput, option)}
                                              >
                                                {option}
                                              </button>
                                            ))}
                                          </div>
                                        )}
                                        <textarea
                                          className="task-reply-input"
                                          value={draftValue}
                                          placeholder="Add a reply for this task..."
                                          disabled={isSubmitting}
                                          onChange={(event) => handleTaskInputDraftChange(taskInput.inputRequestId, event.target.value)}
                                        />
                                        <div className="task-card-footer">
                                          <div className="task-card-status">
                                            {errorText ? (
                                              <span className="task-card-error">{errorText}</span>
                                            ) : isSubmitting ? (
                                              <span className="task-card-waiting">Submitting...</span>
                                            ) : (
                                              <span className="task-card-hint">Reply keeps the task moving without interrupting your current chat.</span>
                                            )}
                                          </div>
                                          <button
                                            type="button"
                                            className="task-submit-btn"
                                            disabled={isSubmitting}
                                            onClick={() => void submitTaskInputReply(taskInput)}
                                          >
                                            Send reply
                                          </button>
                                        </div>
                                      </div>
                                    )
                                  })()}
                                </div>
                              </div>
                            </div>
                          )}
                        </div>
                      ) : (
                        <div className="task-empty-state">
                          <div className="task-empty-icon">◌</div>
                          <div className="task-empty-title">Tasks will collect here when they need you.</div>
                          <div className="task-empty-copy">
                            Long-running Opus work can pause for clarification or keep streaming in the background without interrupting your active chat screen.
                          </div>
                        </div>
                      )}
                    </div>
                  )}

                  {/* MESSAGES */}
                  {mode !== 'task' && messages.map((msg, idx) => {
                    // Session rollover divider
                    if (msg.channel === '__session_rollover__') {
                      return (
                        <div key={msg.id} style={{
                          display: 'flex', alignItems: 'center', gap: 12,
                          margin: '20px 0', opacity: 0.35,
                        }}>
                          <div style={{ flex: 1, height: 1, background: 'linear-gradient(90deg, transparent, rgba(255,255,255,0.3), transparent)' }} />
                          <span style={{ fontSize: 11, fontWeight: 500, letterSpacing: 0.5, color: 'rgba(255,255,255,0.5)', textTransform: 'uppercase' }}>
                            New day
                          </span>
                          <div style={{ flex: 1, height: 1, background: 'linear-gradient(90deg, transparent, rgba(255,255,255,0.3), transparent)' }} />
                        </div>
                      )
                    }

                    // Cross-channel messages: show as collapsible row with channel badge
                    const extLabel = channelLabel(msg.channel)
                    if (extLabel && msg.role === 'user') {
                      const isExpanded = expandedCrossChannelIds.has(msg.id)
                      // Find the assistant response paired with this user message (next message)
                      const pairedResponse = messages[idx + 1]?.role === 'assistant' && isExternalChannel(messages[idx + 1])
                        ? messages[idx + 1] : null
                      return (
                        <div key={msg.id} className="cross-channel-group" style={{ marginBottom: 16 }}>
                          <button
                            className="cross-channel-bar"
                            onClick={() => setExpandedCrossChannelIds((prev) => {
                              const next = new Set(prev)
                              if (next.has(msg.id)) next.delete(msg.id)
                              else next.add(msg.id)
                              return next
                            })}
                            style={{
                              width: '100%', display: 'flex', alignItems: 'center', gap: 10,
                              padding: '10px 14px', borderRadius: 12,
                              background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.08)',
                              color: 'rgba(255,255,255,0.6)', cursor: 'pointer', fontSize: 13,
                              transition: 'background 0.15s',
                            }}
                          >
                            <span style={{
                              padding: '2px 8px', borderRadius: 6, fontSize: 11, fontWeight: 600,
                              background:
                                extLabel === 'WhatsApp'
                                  ? 'rgba(37,211,102,0.15)'
                                  : extLabel === 'Telegram'
                                    ? 'rgba(0,136,204,0.15)'
                                    : 'rgba(121,201,255,0.15)',
                              color:
                                extLabel === 'WhatsApp'
                                  ? '#25d366'
                                  : extLabel === 'Telegram'
                                    ? '#0088cc'
                                    : '#79c9ff',
                            }}>
                              {extLabel}
                            </span>
                            <span style={{ flex: 1, textAlign: 'left', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {cleanText(msg.content).slice(0, 80)}{msg.content.length > 80 ? '...' : ''}
                            </span>
                            <span style={{ fontSize: 11, opacity: 0.5 }}>
                              {isExpanded ? 'collapse' : 'expand'}
                            </span>
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"
                              style={{ transform: isExpanded ? 'rotate(180deg)' : 'rotate(0deg)', transition: 'transform 0.2s' }}>
                              <path d="M7 10l5 5 5-5z" />
                            </svg>
                          </button>
                          {isExpanded && (
                            <div style={{ padding: '12px 14px 4px', borderLeft: '2px solid rgba(255,255,255,0.06)', marginLeft: 16 }}>
                              <div className="message-row user" style={{ marginBottom: 12, display: 'flex', flexDirection: 'column', alignItems: 'flex-end' }}>
                                <div className="query-pill" style={{ maxWidth: '70%', alignSelf: 'flex-end', position: 'relative' }}>
                                  <span style={{ display: 'inline-block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '100%' }}>
                                    {cleanText(msg.content)}
                                  </span>
                                </div>
                                <UserMessageAttachments attachments={msg.attachments} />
                              </div>
                              {pairedResponse && (
                                <div className="message-row assistant" style={{ marginBottom: 8, display: 'flex', flexDirection: 'column', alignItems: 'flex-start' }}>
                                  {pairedResponse.responseBlocks && pairedResponse.responseBlocks.length > 0 ? (
                                    <AssistantResponseBlocks blocks={pairedResponse.responseBlocks} />
                                  ) : (
                                    <AssistantMarkdownBlock content={pairedResponse.content} />
                                  )}
                                  <AssistantProducedArtifacts
                                    messageId={pairedResponse.id}
                                    artifacts={pairedResponse.producedArtifacts}
                                    downloadingArtifactId={downloadingArtifactId}
                                    onDownload={handleDownloadProducedArtifact}
                                  />
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      )
                    }
                    // Skip assistant messages that belong to a cross-channel pair (rendered above with their user message)
                    if (extLabel && msg.role === 'assistant') {
                      const prevMsg = idx > 0 ? messages[idx - 1] : null
                      if (prevMsg && prevMsg.role === 'user' && isExternalChannel(prevMsg)) {
                        return null // Already rendered as part of the collapsed group
                      }
                    }

                    return (
                    <div key={msg.id} className={`message-row ${msg.role}`} style={{ marginBottom: 24, display: 'flex', flexDirection: 'column', alignItems: msg.role === 'user' ? 'flex-end' : 'flex-start' }}>

                    {msg.role === 'user' ? (
                        <>
                          <div className="query-pill" style={{ maxWidth: '70%', alignSelf: 'flex-end', position: 'relative' }}>
                            <span style={{
                              display: 'inline-block',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              whiteSpace: 'nowrap',
                              maxWidth: '100%'
                            }}>
                              {cleanText(msg.content)}
                            </span>
                            <button
                              className="copy-btn"
                              onClick={() => handleCopy(msg.content, `user-${idx}`)}
                            >
                              {copiedId === `user-${idx}` ? (
                                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                                  <path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z" />
                                </svg>
                              ) : (
                                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                                  <path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z" />
                                </svg>
                              )}
                            </button>
                          </div>
                          {msg.backgroundState && (
                            <div className={`query-background-status ${msg.backgroundState}`}>
                              {msg.backgroundState === 'working'
                                ? 'Working in background'
                                : msg.backgroundState === 'ready'
                                  ? 'Ready in Tasks'
                                  : 'Background task failed'}
                            </div>
                          )}
                          <UserMessageAttachments attachments={msg.attachments} />
                        </>
                      ) : (
                        <>
                          {msg.progress?.kind === 'docs_parse' && !String(msg.content || '').trim() && (
                            <DocsProgressCard progress={msg.progress} />
                          )}
                          {msg.progress?.kind === 'tabular_parse' && !String(msg.content || '').trim() && (
                            <TabularProgressCard progress={msg.progress} />
                          )}
                          {msg.activity && msg.progress?.kind !== 'docs_parse' && msg.progress?.kind !== 'tabular_parse' && (
                            <div className="assistant-activity" title="Live activity from Opus tool orchestration">
                              {msg.activity}
                            </div>
                          )}
                          {msg.thinking && (
                            <div className="thinking-block">
                              <div className="thinking-label">Thinking</div>
                              <div className="thinking-text">{msg.thinking}</div>
                            </div>
                          )}
                          <AssistantFlowTimeline entries={msg.activityLog} />
                          <AlphaAgentConsole
                            entries={msg.activityLog}
                            terminalLog={msg.alphaTerminalLog}
                            requestId={msg.requestId}
                            stopped={msg.stopped}
                            onStop={handleStopAlphaAgent}
                          />
                          {msg.responseBlocks && msg.responseBlocks.length > 0 ? (
                            <AssistantResponseBlocks blocks={msg.responseBlocks} />
                          ) : (
                            <AssistantMarkdownBlock content={msg.content} />
                          )}
                          <AssistantProducedArtifacts
                            messageId={msg.id}
                            artifacts={msg.producedArtifacts}
                            downloadingArtifactId={downloadingArtifactId}
                            onDownload={handleDownloadProducedArtifact}
                          />

                          {/* Copy Button for AI Response (Bottom) */}
                          <button
                            className="copy-btn-ai"
                            onClick={() => handleCopy(msg.content, `ai-${idx}`)}
                            style={{ marginTop: 12, alignSelf: 'flex-start' }}
                          >
                            {copiedId === `ai-${idx}` ? (
                              <>
                                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style={{ marginRight: 6 }}>
                                  <path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z" />
                                </svg>
                                Copied
                              </>
                            ) : (
                              <>
                                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style={{ marginRight: 6 }}>
                                  <path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z" />
                                </svg>
                                Copy
                              </>
                            )}
                          </button>

                          {/* Sources for Assistant Messages (Bottom) */}
                          {msg.role === 'assistant' && msg.sources && msg.sources.length > 0 && (
                            <div className="sources-section" style={{ marginTop: 16, marginBottom: 4, width: '100%' }}>
                              <div className="sources-header">SOURCES</div>
                              <div className="sources-grid">
                                {msg.sources.map((src: any, sIdx: number) => {
                                  // Handle both old string format and new object format
                                  const url = typeof src === 'string' ? src : src.url;
                                  const title = typeof src === 'object' ? src.title : null;

                                  let domain = "Unknown";
                                  try {
                                    domain = new URL(url).hostname.replace('www.', '');
                                  } catch (e) { }

                                  return (
                                    <a
                                      key={sIdx}
                                      href={url}
                                      onClick={(e) => {
                                        e.preventDefault()
                                        window.cosmic?.openExternal(url)
                                      }}
                                      className="source-card"
                                    >
                                      <div className="source-header-row" style={{ display: 'flex', alignItems: 'center', marginBottom: 6 }}>
                                        <img
                                          src={`https://www.google.com/s2/favicons?domain=${domain}&sz=64`}
                                          alt=""
                                          style={{ width: 16, height: 16, marginRight: 8, borderRadius: 2 }}
                                        />
                                        <div className="source-title" style={{ fontSize: '11px', fontWeight: 600, opacity: 0.9 }}>
                                          {title || domain}
                                        </div>
                                      </div>
                                      <div className="source-footer">
                                        <span className="source-idx">{sIdx + 1}</span>
                                        <span style={{ fontSize: '10px', opacity: 0.7, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: '80%' }}>{url}</span>
                                      </div>
                                    </a>
                                  )
                                })}
                              </div>
                            </div>
                          )}
                          {msg.role === 'assistant' && msg.stopped && (String(msg.content || '').trim() || String(msg.thinking || '').trim()) && (
                            <div className="response-stopped-label">Stopped</div>
                          )}
                        </>
                      )}
                    </div>
                    )
                  })}

                  {mode !== 'task' && isStreaming && !activeDocsProgressMessage && (
                    <div className="streaming-indicator">
                      {streamingProgress && <div className="streaming-status">{streamingProgress}</div>}
                      <div className="streaming-dots" aria-hidden>
                        {[0, 1, 2, 3, 4].map((i) => (
                          <span key={i} className="streaming-dot-pix" style={{ animationDelay: `${i * 0.09}s` }} />
                        ))}
                      </div>
                    </div>
                  )}
                  <div ref={responseEndRef} />
                </div>
              </div>
            </LiquidGlass>
          </div>
        )}

        {/* SCROLL TO BOTTOM BUTTON */}
        {shouldShowPrimarySurface && mode !== 'task' && showScrollButton && (
          <button className="scroll-to-bottom" onClick={scrollToBottom}>
            <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
              <path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6 1.41-1.41z" />
            </svg>
          </button>
        )}

        {/* INPUT BAR / LAUNCHER */}
        {mode !== 'meeting' && mode !== 'spaces' && <div
          ref={composerSurfaceRef}
          className={`cosmic ${searchState === 'visible' ? 'visible' : searchState === 'hiding' ? 'hiding' : ''} ${showLauncherTray ? 'launchpad-open' : ''} ${composerLaunchClass}`}
          style={composerLaunchStyle}
        >
          <LiquidGlass cornerRadius={24} style={{ width: '100%', height: '100%' }}>
            <div className="glass-content">
              {showLauncherTray ? (
                <div className="launchpad-row" role="toolbar" aria-label="COSMIC modes">
                  {LAUNCHPAD_TILES.map((tile) => (
                    <button
                      key={tile.id}
                      className={`launchpad-tile ${tile.id} ${tile.locked ? 'locked' : ''}`}
                      onClick={(event) => handleLauncherTileClick(tile.id, event)}
                      onMouseEnter={(event) => showHoverTooltipForElement(tile.locked ? `${tile.label} locked` : tile.label, event.currentTarget, 'launcher')}
                      onMouseLeave={hideHoverTooltip}
                      onFocus={(event) => showHoverTooltipForElement(tile.locked ? `${tile.label} locked` : tile.label, event.currentTarget, 'launcher')}
                      onBlur={hideHoverTooltip}
                      type="button"
                      disabled={tile.locked}
                      aria-label={tile.locked ? `${tile.label} locked` : tile.label}
                    >
                      {tile.id === 'task' && taskDashboardCount > 0 && (
                        <span className="launchpad-badge">{taskDashboardCount}</span>
                      )}
                      <div className="launchpad-icon-shell">
                        <LaunchpadIcon tile={tile.id} />
                      </div>
                      <span className="launchpad-label">{tile.label}</span>
                    </button>
                  ))}
                </div>
              ) : (
                mode === 'task' ? (
                  <div className="task-toolbar">
                    <button
                      className="back-btn"
                      onClick={handleShowLauncherTray}
                      onMouseEnter={(event) => showHoverTooltipForElement('Modes', event.currentTarget, 'control')}
                      onMouseLeave={hideHoverTooltip}
                      onFocus={(event) => showHoverTooltipForElement('Modes', event.currentTarget, 'control')}
                      onBlur={hideHoverTooltip}
                      aria-label="Modes"
                      style={{ marginRight: 8 }}
                      type="button"
                    >
                      <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M14.71 6.71a1 1 0 0 1 0 1.41L10.83 12l3.88 3.88a1 1 0 0 1-1.41 1.41l-4.59-4.59a1 1 0 0 1 0-1.41l4.59-4.59a1 1 0 0 1 1.41 0z" />
                      </svg>
                    </button>
                    <div className="task-toolbar-copy">
                      <div className="task-toolbar-title">Task Inbox</div>
                      <div className="task-toolbar-subtitle">
                        {taskDashboardCount > 0
                          ? `${backgroundTaskCount} background · ${pendingTaskCount} waiting for input`
                          : 'No task is waiting right now'}
                      </div>
                    </div>
                    <div className="task-mode-pill" aria-label="Task mode uses Opus">
                      <span className="task-mode-pill-kicker">Task</span>
                      <span className="task-mode-pill-model">OPUS</span>
                    </div>
                    <button
                      className="task-toolbar-chat-btn"
                      onClick={showChatComposer}
                      type="button"
                    >
                      Open chat
                    </button>
                  </div>
                ) : (
                  <>
                    {pendingAttachments.length > 0 && (
                      <div className="composer-attachment-bar">
                        {pendingAttachments.map((attachment) => (
                          <div key={attachment.filePath} className="composer-attachment-chip">
                            <span className="composer-attachment-icon" aria-hidden="true">
                              <DocumentAttachmentIcon />
                            </span>
                            <div className="composer-attachment-copy">
                              <span className="composer-attachment-name">{attachment.filename}</span>
                              <span className="composer-attachment-meta">
                                {formatAttachmentSize(attachment.sizeBytes) || attachment.mimeType || 'Attachment'}
                              </span>
                            </div>
                            <button
                              type="button"
                              className="composer-attachment-remove"
                              onClick={() => handleRemoveAttachment(attachment.filePath)}
                              aria-label={`Remove ${attachment.filename}`}
                            >
                              <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
                                <path d="M1 1L9 9M9 1L1 9" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
                              </svg>
                            </button>
                          </div>
                        ))}
                      </div>
                    )}
                  <div className="input-row">
                    <button
                      className="back-btn"
                      onClick={handleShowLauncherTray}
                      onMouseEnter={(event) => showHoverTooltipForElement('Modes', event.currentTarget, 'control')}
                      onMouseLeave={hideHoverTooltip}
                      onFocus={(event) => showHoverTooltipForElement('Modes', event.currentTarget, 'control')}
                      onBlur={hideHoverTooltip}
                      aria-label="Modes"
                      style={{ marginRight: 8 }}
                      type="button"
                    >
                      {taskDashboardCount > 0 && <span className="back-btn-badge">{taskDashboardCount}</span>}
                      <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M14.71 6.71a1 1 0 0 1 0 1.41L10.83 12l3.88 3.88a1 1 0 0 1-1.41 1.41l-4.59-4.59a1 1 0 0 1 0-1.41l4.59-4.59a1 1 0 0 1 1.41 0z" />
                      </svg>
                    </button>

                    <button
                      className={`attach-btn ${pendingAttachments.length > 0 ? 'active' : ''}`}
                      onClick={handlePickDocuments}
                      onMouseEnter={(event) => showHoverTooltipForElement('Attach files', event.currentTarget, 'control')}
                      onMouseLeave={hideHoverTooltip}
                      onFocus={(event) => showHoverTooltipForElement('Attach files', event.currentTarget, 'control')}
                      onBlur={hideHoverTooltip}
                      aria-label="Attach files"
                      disabled={isStreaming || authState !== 'authenticated'}
                      type="button"
                    >
                      {pendingAttachments.length > 0 && <span className="attach-btn-badge">{pendingAttachments.length}</span>}
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
                        <path
                          d="M8.5 12.5L13.7 7.3a3 3 0 1 1 4.24 4.24l-7.07 7.07a5 5 0 1 1-7.07-7.07l8.48-8.49"
                          stroke="currentColor"
                          strokeWidth="1.9"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </button>

                    <textarea
                      ref={inputRef}
                      className="input"
                      rows={1}
                      defaultValue=""
                      onChange={handleInput}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && !e.shiftKey) {
                          e.preventDefault()
                          handleSubmit()
                        }
                      }}
                      onFocus={() => setIsInputFocused(true)}
                      onBlur={() => setIsInputFocused(false)}
                      placeholder={composerPlaceholder}
                      spellCheck={false}
                      autoComplete="off"
                      disabled={isStreaming || authState !== 'authenticated'}
                    />

                    {hasText && (
                      <button
                        className="clear-btn"
                        onClick={() => {
                          clearComposerInput()
                          inputRef.current?.focus()
                        }}
                        type="button"
                      >
                        ✕
                      </button>
                    )}

                    <div className="model-dial" role="tablist" aria-label="Model selection">
                      <div className="model-dial-window">
                        <div
                          className="model-dial-viewport"
                          ref={modelDialRef}
                          onScroll={handleModelDialScroll}
                          onWheel={handleModelDialWheel}
                        >
                          {MODEL_OPTIONS.map((item) => (
                            <button
                              key={item.id}
                              data-model={item.id}
                              className={`model-dial-btn ${selectedModel === item.id ? 'active' : ''} ${modelPulseModel === item.id ? 'pulse' : ''}`}
                              onClick={() => handleModelDialFocus(item.id)}
                              onMouseEnter={(event) => showHoverTooltipForElement(item.label, event.currentTarget, 'model')}
                              onMouseLeave={hideHoverTooltip}
                              onFocus={(event) => {
                                handleModelDialFocus(item.id)
                                showHoverTooltipForElement(item.label, event.currentTarget, 'model')
                              }}
                              onBlur={hideHoverTooltip}
                              type="button"
                              aria-label={item.label}
                              aria-selected={selectedModel === item.id}
                            >
                              <span className="model-dial-knob">
                                <ModelDialIcon model={item.id} />
                              </span>
                            </button>
                          ))}
                        </div>
                      </div>
                    </div>

                    {isStreaming && activeStreamingRequestIdRef.current && (
                      <button
                        className={`background-task-btn ${backgroundingRequestId === activeStreamingRequestIdRef.current ? 'busy' : ''}`}
                        onClick={() => void handleMoveActiveToBackground()}
                        onMouseEnter={(event) => showHoverTooltipForElement('Move to background', event.currentTarget, 'control')}
                        onMouseLeave={hideHoverTooltip}
                        onFocus={(event) => showHoverTooltipForElement('Move to background', event.currentTarget, 'control')}
                        onBlur={hideHoverTooltip}
                        type="button"
                        aria-label="Move to background"
                        disabled={backgroundingRequestId === activeStreamingRequestIdRef.current}
                      >
                        <img src={moveToBackgroundIcon} alt="" aria-hidden="true" className="background-task-icon-image invert" />
                      </button>
                    )}

                    {isStreaming ? (
                      <button
                        className="stream-stop-btn"
                        onClick={handleStopStreaming}
                        onMouseEnter={(event) => showHoverTooltipForElement('Stop response', event.currentTarget, 'control')}
                        onMouseLeave={hideHoverTooltip}
                        onFocus={(event) => showHoverTooltipForElement('Stop response', event.currentTarget, 'control')}
                        onBlur={hideHoverTooltip}
                        type="button"
                        aria-label="Stop response"
                      >
                        <LiquidGlassLoader />
                      </button>
                    ) : (
                      <button
                        className={`send-btn ${(hasText || pendingAttachments.length > 0) ? 'active' : ''}`}
                        onClick={handleSubmit}
                        disabled={!hasText && pendingAttachments.length === 0}
                        type="button"
                      >
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
                          <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z" />
                        </svg>
                      </button>
                    )}
                  </div>
                  </>
                )
              )}
            </div>
          </LiquidGlass>
        </div>}
      </div>
      {hoverTooltip && createPortal(
        <div
          className="cosmic-hover-tooltip"
          style={{
            left: `${hoverTooltip.x}px`,
            top: `${hoverTooltip.y}px`,
          }}
          role="tooltip"
        >
          {hoverTooltip.label}
        </div>,
        document.body,
      )}
    </>
  )
}

function LaunchpadIcon({ tile }: { tile: LauncherTileId }) {
  if (tile === 'chat') {
    return (
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <path d="M6.25 6.75h11.5A1.75 1.75 0 0 1 19.5 8.5v6a1.75 1.75 0 0 1-1.75 1.75H11l-3.9 3.08A.65.65 0 0 1 6 18.82v-2.57A1.75 1.75 0 0 1 4.5 14.5v-6A1.75 1.75 0 0 1 6.25 6.75Z" />
        <path d="M9 11.5h6" />
        <path d="M9 14h3.5" />
      </svg>
    )
  }
  if (tile === 'meeting') {
    return (
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <rect x="4.75" y="7.25" width="11.5" height="9.5" rx="2.1" />
        <path d="M16.5 10.25 20 8.5v7l-3.5-1.75" />
        <circle cx="9.75" cy="12" r="2" />
      </svg>
    )
  }
  if (tile === 'task') {
    return (
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <rect x="6" y="4.75" width="12" height="14.5" rx="2.3" />
        <path d="M9 4.75h6" />
        <path d="m9.4 11.8 1.7 1.7 3.5-3.5" />
        <path d="M9 16.4h6" />
      </svg>
    )
  }
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <rect x="5.25" y="5.25" width="5.5" height="5.5" rx="1.4" />
      <rect x="13.25" y="5.25" width="5.5" height="5.5" rx="1.4" />
      <rect x="5.25" y="13.25" width="5.5" height="5.5" rx="1.4" />
      <path d="M16 13.75v3.5" />
      <path d="M14.25 15.5H17.75" />
    </svg>
  )
}

function ModelDialIcon({ model }: { model: GatewayModelSelection }) {
  const label = model === 'perplexity' ? 'P' : model === 'cosmic' ? 'C' : model === 'haiku' ? 'H' : 'O'
  return <span className="model-dial-glyph">{label}</span>
}

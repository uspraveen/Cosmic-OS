export interface AlphaConsoleAnchor {
  taskId: string | null
  offset: number
}

export interface AlphaTerminalAnchorSource {
  taskId?: string | null
  streamOffset?: number | null
}

export interface ResponseBlockLike {
  id: string
  type: string
  text?: string
  code?: string
}

export type AlphaStreamSegment =
  | { kind: 'content'; content?: string; blocks?: ResponseBlockLike[] }
  | { kind: 'alpha_console'; taskId: string | null }

const DEFAULT_ALPHA_TASK_KEY = '_default'

export const alphaTaskKey = (taskId?: string | null) => {
  const normalized = String(taskId || '').trim()
  return normalized || DEFAULT_ALPHA_TASK_KEY
}

export const normalizeAlphaConsoleAnchors = (value: unknown): AlphaConsoleAnchor[] | undefined => {
  if (!Array.isArray(value)) {
    return undefined
  }
  const anchors: AlphaConsoleAnchor[] = []
  const seen = new Set<string>()
  for (const item of value) {
    if (!item || typeof item !== 'object') {
      continue
    }
    const offsetRaw = (item as { offset?: unknown; stream_offset?: unknown }).offset
      ?? (item as { stream_offset?: unknown }).stream_offset
    const offset = typeof offsetRaw === 'number' && Number.isFinite(offsetRaw)
      ? Math.max(0, Math.floor(offsetRaw))
      : null
    if (offset === null) {
      continue
    }
    const taskId = typeof (item as { task_id?: unknown }).task_id === 'string'
      && (item as { task_id: string }).task_id.trim()
      ? (item as { task_id: string }).task_id.trim()
      : typeof (item as { taskId?: unknown }).taskId === 'string'
        && (item as { taskId: string }).taskId.trim()
        ? (item as { taskId: string }).taskId.trim()
        : null
    const key = alphaTaskKey(taskId)
    if (seen.has(key)) {
      continue
    }
    seen.add(key)
    anchors.push({ taskId, offset })
  }
  return anchors.length > 0 ? anchors.sort((a, b) => a.offset - b.offset) : undefined
}

export const resolveAlphaConsoleAnchors = (
  anchors: AlphaConsoleAnchor[] | undefined,
  terminalLog: AlphaTerminalAnchorSource[] | undefined,
): AlphaConsoleAnchor[] => {
  const resolved = new Map<string, AlphaConsoleAnchor>()

  for (const anchor of anchors || []) {
    const key = alphaTaskKey(anchor.taskId)
    if (!resolved.has(key)) {
      resolved.set(key, anchor)
    }
  }

  for (const entry of terminalLog || []) {
    const key = alphaTaskKey(entry.taskId)
    if (resolved.has(key)) {
      continue
    }
    if (typeof entry.streamOffset === 'number' && Number.isFinite(entry.streamOffset) && entry.streamOffset >= 0) {
      resolved.set(key, {
        taskId: entry.taskId ?? null,
        offset: Math.floor(entry.streamOffset),
      })
    }
  }

  return Array.from(resolved.values()).sort((a, b) => a.offset - b.offset)
}

export const measureAssistantStreamLength = (
  content?: string,
  responseBlocks?: ResponseBlockLike[],
): number => {
  if (responseBlocks && responseBlocks.length > 0) {
    return responseBlocks.reduce((sum, block) => sum + measureResponseBlockLength(block), 0)
  }
  return String(content || '').length
}

export const ensureAlphaConsoleAnchor = (
  current: AlphaConsoleAnchor[] | undefined,
  taskId: string | null | undefined,
  offset: number,
): AlphaConsoleAnchor[] => {
  const safeOffset = Math.max(0, Math.floor(offset))
  const key = alphaTaskKey(taskId)
  const existing = Array.isArray(current) ? current : []
  if (existing.some((item) => alphaTaskKey(item.taskId) === key)) {
    return existing
  }
  return [...existing, { taskId: taskId ?? null, offset: safeOffset }].sort((a, b) => a.offset - b.offset)
}

export const buildAlphaStreamSegments = (options: {
  content?: string
  responseBlocks?: ResponseBlockLike[]
  alphaConsoleAnchors?: AlphaConsoleAnchor[]
  alphaTerminalLog?: AlphaTerminalAnchorSource[]
}): { segments: AlphaStreamSegment[]; hasAnchors: boolean } => {
  const anchors = resolveAlphaConsoleAnchors(options.alphaConsoleAnchors, options.alphaTerminalLog)
  if (anchors.length <= 0) {
    return {
      hasAnchors: false,
      segments: [{
        kind: 'content',
        content: options.content,
        blocks: options.responseBlocks,
      }],
    }
  }

  const segments: AlphaStreamSegment[] = []
  const usesBlocks = Boolean(options.responseBlocks && options.responseBlocks.length > 0)
  let remainingContent = options.content || ''
  let remainingBlocks = usesBlocks ? [...(options.responseBlocks || [])] : []

  anchors.forEach((anchor, index) => {
    if (usesBlocks) {
      const [before, after] = splitResponseBlocksAtOffset(remainingBlocks, anchor.offset)
      if (before.length > 0) {
        segments.push({ kind: 'content', blocks: before })
      }
      remainingBlocks = after
    } else {
      const [before, after] = splitContentAtOffset(remainingContent, anchor.offset)
      if (before) {
        segments.push({ kind: 'content', content: before })
      }
      remainingContent = after
    }

    segments.push({ kind: 'alpha_console', taskId: anchor.taskId })

    const isLast = index === anchors.length - 1
    if (isLast) {
      if (usesBlocks) {
        if (remainingBlocks.length > 0) {
          segments.push({ kind: 'content', blocks: remainingBlocks })
        }
      } else if (remainingContent) {
        segments.push({ kind: 'content', content: remainingContent })
      }
    }
  })

  return { segments, hasAnchors: true }
}

const measureResponseBlockLength = (block: ResponseBlockLike): number => {
  if (block.type === 'markdown') {
    return String(block.text || '').length
  }
  if (block.type === 'code') {
    return String(block.code || '').length
  }
  return 1
}

const MAX_ANCHOR_SNAP_LOOKBACK = 600

export const snapAlphaAnchorOffset = (text: string, offset: number): number => {
  const safe = Math.max(0, Math.min(Math.floor(offset), text.length))
  if (safe <= 0 || safe >= text.length) {
    return safe
  }
  if (/\s/.test(text[safe]) || /\s/.test(text[safe - 1])) {
    return safe
  }
  const windowStart = Math.max(0, safe - MAX_ANCHOR_SNAP_LOOKBACK)
  const window = text.slice(windowStart, safe)
  const paragraphBreak = window.lastIndexOf('\n\n')
  if (paragraphBreak >= 0) {
    return windowStart + paragraphBreak + 2
  }
  const sentenceBreak = Math.max(
    window.lastIndexOf('. '),
    window.lastIndexOf('! '),
    window.lastIndexOf('? '),
  )
  if (sentenceBreak >= 0) {
    return windowStart + sentenceBreak + 1
  }
  const newline = window.lastIndexOf('\n')
  if (newline >= 0) {
    return windowStart + newline + 1
  }
  const space = window.lastIndexOf(' ')
  if (space >= 0) {
    return windowStart + space
  }
  return safe
}

const splitContentAtOffset = (content: string, offset: number): [string, string] => {
  const safeOffset = snapAlphaAnchorOffset(content, offset)
  return [content.slice(0, safeOffset), content.slice(safeOffset)]
}

const splitResponseBlocksAtOffset = (
  blocks: ResponseBlockLike[],
  offset: number,
): [ResponseBlockLike[], ResponseBlockLike[]] => {
  const safeOffset = Math.max(0, Math.floor(offset))
  if (safeOffset <= 0) {
    return [[], blocks]
  }

  const before: ResponseBlockLike[] = []
  const after: ResponseBlockLike[] = []
  let cursor = 0
  let splitDone = false

  for (const block of blocks) {
    if (splitDone) {
      after.push(block)
      continue
    }

    const blockLength = measureResponseBlockLength(block)
    if (cursor + blockLength < safeOffset) {
      before.push(block)
      cursor += blockLength
      continue
    }

    if (cursor + blockLength === safeOffset) {
      before.push(block)
      splitDone = true
      continue
    }

    if (block.type === 'markdown') {
      const text = String(block.text || '')
      const splitAt = snapAlphaAnchorOffset(text, safeOffset - cursor)
      const head = text.slice(0, splitAt)
      const tail = text.slice(splitAt)
      if (head) {
        before.push({ ...block, text: head })
      }
      if (tail) {
        after.push({ ...block, id: `${block.id}_tail`, text: tail })
      }
      splitDone = true
      continue
    }

    after.push(block)
    splitDone = true
  }

  return [before, after]
}

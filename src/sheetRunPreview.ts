/**
 * The sheets specialist mirrors every write it makes onto the event stream as
 * a `sheets_progress` payload — the delta it just wrote, positioned absolutely
 * by its A1 range. The live card wants the sheet as it stands now, so this
 * module folds those deltas into one grid, the same way browserRunTrail
 * accumulates the steps a browser run has already taken.
 *
 * It lives outside App.tsx because the fold has to survive being replayed:
 * resume snapshots can re-deliver a reading already folded in, so merging
 * must be idempotent — which is why every value lands at an absolute
 * position parsed from its range rather than being "appended".
 */

export interface SheetRunTrailEntry {
  op: string
  range: string
  rows: number
}

/** Where a delta landed, 1-based and inclusive. Open ends stay null. */
export interface SheetRangeSpan {
  tab: string
  startRow: number
  startCol: number
  endRow: number | null
  endCol: number | null
}

export interface SheetProgressHeader {
  formatted?: boolean
  color?: string
  frozenRows?: number
  frozenCols?: number
}

export interface SheetProgressState {
  kind: 'sheet_run'
  spreadsheetId?: string
  title?: string
  url?: string
  accountEmail?: string
  op?: string
  phase?: 'creating' | 'writing' | 'verifying' | 'done' | 'failed'
  tab?: string
  range?: string
  /** The sheet as accumulated so far — built here, never sent by the agent. */
  grid?: string[][]
  /** The delta that just arrived, straight from the agent. Folded into `grid`
   * by mergeSheetRunProgress and dropped from the merged state — the grid is
   * the only thing worth persisting. */
  values?: string[][]
  rows?: number
  cols?: number
  header?: SheetProgressHeader | null
  /** The span written by the most recent value-carrying delta, for the
   * "just landed" highlight on the card. */
  lastSpan?: SheetRangeSpan | null
  trail?: SheetRunTrailEntry[]
  error?: string
  truncated?: boolean
}

/** The parts of a sheets_progress reading this merge actually reads. */
export interface SheetRunProgressLike {
  spreadsheetId?: string
  spreadsheet_id?: string
  title?: string
  url?: string
  accountEmail?: string
  account_email?: string
  op?: string
  phase?: string
  tab?: string
  range?: string
  values?: unknown
  header?: SheetProgressHeader | null
  error?: string
  grid?: string[][]
  lastSpan?: SheetRangeSpan | null
  trail?: SheetRunTrailEntry[]
}

/** Bounds so one runaway payload can't balloon the message state. Mirrors the
 * agent-side and gateway-side caps; this is the last line of defense. */
export const SHEET_GRID_MAX_ROWS = 400
export const SHEET_GRID_MAX_COLS = 40
export const SHEET_PAYLOAD_MAX_ROWS = 250
export const SHEET_PAYLOAD_MAX_COLS = 40
const SHEET_CELL_MAX_CHARS = 160
const TRAIL_LIMIT = 5

const cleanText = (value: unknown): string => (typeof value === 'string' ? value.trim() : '')

const a1ColumnIndex = (letters: string): number => {
  let index = 0
  for (const char of letters.toUpperCase()) {
    index = index * 26 + (char.charCodeAt(0) - 64)
  }
  return index
}

/**
 * Parse the A1 tail of a range like `Jobs!A1:I10`, `'Jobs 2'!B2`, `A1:C` or
 * `B3`. Tab-less ranges happen when the agent's range carried no tab; those
 * land on the card's current tab.
 */
export const parseSheetRange = (range: unknown): SheetRangeSpan | null => {
  const raw = cleanText(range)
  if (!raw) {
    return null
  }
  let tab = ''
  let a1 = raw
  const bang = raw.lastIndexOf('!')
  if (bang >= 0) {
    tab = raw.slice(0, bang).replace(/^'|'$/g, '').trim()
    a1 = raw.slice(bang + 1).trim()
  }
  const match = /^([A-Za-z]{0,3})(\d{0,7})(?::([A-Za-z]{0,3})(\d{0,7}))?$/.exec(a1)
  if (!match || (!match[1] && !match[2])) {
    return null
  }
  const startRow = match[2] ? parseInt(match[2], 10) : 1
  const startCol = match[1] ? a1ColumnIndex(match[1]) : 1
  if (startRow < 1 || startCol < 1) {
    return null
  }
  const endRow = match[4] ? parseInt(match[4], 10) : null
  const endCol = match[3] ? a1ColumnIndex(match[3]) : null
  return {
    tab,
    startRow,
    startCol,
    endRow: endRow && endRow >= startRow ? endRow : null,
    endCol: endCol && endCol >= startCol ? endCol : null,
  }
}

const normalizeCell = (value: unknown): string => {
  if (value === null || value === undefined) {
    return ''
  }
  return String(value).slice(0, SHEET_CELL_MAX_CHARS)
}

/** Sanitize a values payload that rode in on a raw event. */
export const normalizeSheetValues = (value: unknown): string[][] | undefined => {
  if (!Array.isArray(value)) {
    return undefined
  }
  const rows: string[][] = []
  for (const row of value.slice(0, SHEET_PAYLOAD_MAX_ROWS)) {
    if (!Array.isArray(row)) {
      continue
    }
    rows.push(row.slice(0, SHEET_PAYLOAD_MAX_COLS).map(normalizeCell))
  }
  return rows.length > 0 ? rows : undefined
}

const normalizeHeader = (value: unknown): SheetProgressHeader | null => {
  if (!value || typeof value !== 'object') {
    return null
  }
  const raw = value as Record<string, unknown>
  const header: SheetProgressHeader = {}
  if (raw.formatted !== undefined) {
    header.formatted = Boolean(raw.formatted)
  }
  const color = cleanText(raw.color)
  if (color) {
    header.color = color
  }
  for (const key of ['frozenRows', 'frozenCols'] as const) {
    const nested = key === 'frozenRows' ? 'frozen_rows' : 'frozen_cols'
    const num = Number(raw[key] ?? raw[nested])
    if (Number.isFinite(num) && num >= 0) {
      header[key] = num
    }
  }
  return Object.keys(header).length > 0 ? header : null
}

export const normalizeSheetProgress = (value: unknown): SheetProgressState | undefined => {
  if (!value || typeof value !== 'object') {
    return undefined
  }
  const raw = value as Record<string, unknown>
  const phase = cleanText(raw.phase) as SheetProgressState['phase']
  const grid = normalizeSheetValues(raw.grid)
  const trail = normalizeSheetTrail(raw.trail)
  const state: SheetProgressState = {
    kind: 'sheet_run',
    spreadsheetId: cleanText(raw.spreadsheet_id ?? raw.spreadsheetId) || undefined,
    title: cleanText(raw.title) || undefined,
    url: cleanText(raw.url) || undefined,
    accountEmail: cleanText(raw.account_email ?? raw.accountEmail) || undefined,
    op: cleanText(raw.op) || undefined,
    phase: phase || undefined,
    tab: cleanText(raw.tab) || undefined,
    range: cleanText(raw.range) || undefined,
    grid: grid && grid.length > 0 ? grid : undefined,
    values: normalizeSheetValues(raw.values),
    // Absent means "this reading says nothing about the header" — only an
    // explicit null/payload clears or changes it.
    header: raw.header === undefined ? undefined : normalizeHeader(raw.header),
    lastSpan: (raw.lastSpan as SheetRangeSpan | null | undefined) ?? null,
    trail: trail && trail.length > 0 ? trail : undefined,
    error: cleanText(raw.error) || undefined,
    truncated: Boolean(raw.truncated),
  }
  if (grid && grid.length > 0) {
    state.rows = grid.length
    state.cols = grid.reduce((max, row) => Math.max(max, row.length), 0)
  }
  return state
}

/** Normalize a trail that rode in on a raw payload (mirror round trips). */
export const normalizeSheetTrail = (value: unknown): SheetRunTrailEntry[] | undefined => {
  if (!Array.isArray(value)) {
    return undefined
  }
  const entries = value
    .map((item) => {
      const entry = (item && typeof item === 'object' ? item : {}) as Record<string, unknown>
      return {
        op: cleanText(entry.op),
        range: cleanText(entry.range),
        rows: Number.isFinite(Number(entry.rows)) ? Number(entry.rows) : 0,
      }
    })
    .filter((entry) => Boolean(entry.op))
    .slice(-TRAIL_LIMIT)
  return entries.length > 0 ? entries : undefined
}

const cloneGrid = (grid: string[][] | undefined): string[][] => (grid || []).map((row) => [...row])

/** A trail entry per value-changing delta, so the card can narrate the run. */
const pushTrail = (trail: SheetRunTrailEntry[], entry: SheetRunTrailEntry): SheetRunTrailEntry[] => {
  const last = trail[trail.length - 1]
  if (last && last.op === entry.op && last.range === entry.range && last.rows === entry.rows) {
    return trail
  }
  return [...trail, entry].slice(-TRAIL_LIMIT)
}

/**
 * Fold a fresh reading into the one already on screen. Values land at the
 * absolute position their range names, so a re-delivered reading overwrites
 * itself with itself instead of duplicating rows.
 */
export const mergeSheetRunProgress = <T extends SheetRunProgressLike>(
  previous: T | undefined,
  incoming: T | undefined,
): T | undefined => {
  if (!incoming) {
    return previous
  }
  if (!previous) {
    const incomingState = incoming as T & Partial<SheetProgressState>
    const values = normalizeSheetValues(incomingState.values)
    const span = parseSheetRange(incomingState.range)
    let grid = cloneGrid(undefined)
    let lastSpan: SheetRangeSpan | null = null
    let trail: SheetRunTrailEntry[] = []
    if (values && span) {
      const applied = applyValues(grid, span, values)
      grid = applied.grid
      lastSpan = applied.span
    } else if (values) {
      const applied = applyValues(grid, { tab: '', startRow: 1, startCol: 1, endRow: null, endCol: null }, values)
      grid = applied.grid
      lastSpan = applied.span
    }
    if (values && (lastSpan || incomingState.range)) {
      trail = pushTrail(trail, { op: cleanText(incomingState.op) || 'write', range: cleanText(incomingState.range), rows: values.length })
    }
    // `values` has been folded into the grid — drop it so the merged state
    // doesn't carry the delta twice.
    const rest = { ...incomingState } as Record<string, unknown>
    delete rest.values
    return {
      ...rest,
      grid: grid.length > 0 ? grid : undefined,
      rows: grid.length > 0 ? grid.length : undefined,
      cols: grid.length > 0 ? grid.reduce((max, row) => Math.max(max, row.length), 0) : undefined,
      lastSpan,
      trail: trail.length > 0 ? trail : undefined,
    } as unknown as T
  }

  const prevState = previous as T & Partial<SheetProgressState>
  const incomingState = incoming as T & Partial<SheetProgressState>
  const values = normalizeSheetValues(incomingState.values)
  const grid = cloneGrid(prevState.grid)
  const span = parseSheetRange(incomingState.range)
  let lastSpan = prevState.lastSpan ?? null
  let trail = incomingState.trail ?? prevState.trail ?? []
  const op = cleanText(incomingState.op)

  if (values && span) {
    const applied = applyValues(grid, span, values, op === 'clear_range')
    lastSpan = applied.span
    trail = pushTrail(trail, { op: op || 'write', range: cleanText(incomingState.range), rows: values.length })
  } else if (values) {
    // No parseable range: fall back to appending below the existing grid
    // rather than dropping the delta on the floor.
    const startRow = grid.length + 1
    const applied = applyValues(grid, { tab: '', startRow, startCol: 1, endRow: null, endCol: null }, values)
    lastSpan = applied.span
    trail = pushTrail(trail, { op: op || 'write', range: cleanText(incomingState.range), rows: values.length })
  } else if (op === 'clear_range' && span) {
    applyValues(grid, span, [], true)
    trail = pushTrail(trail, { op, range: cleanText(incomingState.range), rows: 0 })
  }

  const header = incomingState.header !== undefined ? incomingState.header : prevState.header ?? null
  const rest = { ...incomingState } as Record<string, unknown>
  delete rest.values
  return {
    ...rest,
    grid: grid.length > 0 ? grid : undefined,
    rows: grid.length > 0 ? grid.length : undefined,
    cols: grid.length > 0 ? grid.reduce((max, row) => Math.max(max, row.length), 0) : undefined,
    lastSpan,
    trail: trail.length > 0 ? trail : undefined,
    header,
  } as unknown as T
}

/** Write `values` into `grid` at `span`, growing it within the bounds. With
 * `clear=true` the span is blanked instead (values ignored). Mutates grid. */
const applyValues = (
  grid: string[][],
  span: SheetRangeSpan,
  values: string[][],
  clear = false,
): { grid: string[][]; span: SheetRangeSpan } => {
  const rowCount = clear ? Math.max(1, (span.endRow ?? span.startRow) - span.startRow + 1) : values.length
  const colCount = clear ? Math.max(1, (span.endCol ?? span.startCol) - span.startCol + 1) : Math.max(0, ...values.map((row) => row.length))
  const endRow = Math.min(SHEET_GRID_MAX_ROWS, span.startRow + rowCount - 1)
  const endCol = Math.min(SHEET_GRID_MAX_COLS, span.startCol + Math.max(0, colCount - 1))
  while (grid.length < endRow) {
    grid.push([])
  }
  for (let row = span.startRow; row <= endRow; row += 1) {
    const gridRow = grid[row - 1]
    const valueRow = clear ? [] : values[row - span.startRow] || []
    for (let col = span.startCol; col <= endCol; col += 1) {
      gridRow[col - 1] = clear ? '' : normalizeCell(valueRow[col - span.startCol])
    }
  }
  return {
    grid,
    span: { ...span, endRow, endCol },
  }
}

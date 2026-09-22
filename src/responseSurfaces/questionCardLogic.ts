// Pure decision logic for the ask-user question card, kept out of the
// component so it stays unit-testable in this repo's vitest setup (no DOM).

export const QUESTION_OPTION_KEYS = 'ABCDEF'
export const MAX_QUESTION_OPTIONS = 6

export interface QuestionRows {
  /** Listed answer choices, trimmed, deduped, capped at six. */
  rows: string[]
  /** A custom free-form row always exists when no options survived. */
  customAllowed: boolean
}

/**
 * The selected row, as `rows` index — or `rows.length` for the custom row
 * (`customRow(rows)`), or null when nothing is picked yet.
 */
export type QuestionSelection = number | null

export const customRow = (rowCount: number) => rowCount

export function normalizeQuestionRows(options: unknown): QuestionRows {
  const raw = Array.isArray(options) ? options : []
  const rows: string[] = []
  for (const item of raw) {
    const text = String(item ?? '').trim()
    if (!text || rows.includes(text)) continue
    rows.push(text)
    if (rows.length >= MAX_QUESTION_OPTIONS) break
  }
  return { rows, customAllowed: true }
}

export function questionCustomAllowed(rows: QuestionRows, allowCustom: boolean): boolean {
  return allowCustom || rows.rows.length === 0
}

export interface ResolveQuestionInput {
  rowsInfo: QuestionRows
  customAllowed: boolean
  selected: QuestionSelection
  customValue: string
}

export type ResolveQuestionResult =
  | { ok: true; answer: string }
  | { ok: false; error: string }

export function resolveQuestionAnswer({
  rowsInfo,
  customAllowed,
  selected,
  customValue,
}: ResolveQuestionInput): ResolveQuestionResult {
  const custom = customRow(rowsInfo.rows.length)
  if (selected !== null && selected >= 0 && selected < rowsInfo.rows.length) {
    return { ok: true, answer: rowsInfo.rows[selected] }
  }
  if (customAllowed && selected === custom) {
    const answer = customValue.trim()
    if (!answer) {
      return {
        ok: false,
        error: rowsInfo.rows.length === 0
          ? 'Type an answer first.'
          : 'Type an answer or pick a listed option.',
      }
    }
    return { ok: true, answer }
  }
  return { ok: false, error: 'Pick an option or type an answer.' }
}

/**
 * Map an unmodified keyboard key to a selectable row: A–F / 1–6 pick the
 * listed options, and the key just past the last option picks the custom
 * row. Null when the key maps to nothing selectable.
 */
export function questionKeyRow(
  key: string,
  rowCount: number,
  customAllowed: boolean,
): number | null {
  if (key.length !== 1) return null
  const upper = key.toUpperCase()
  const letter = QUESTION_OPTION_KEYS.indexOf(upper)
  const digit = /^[1-6]$/.test(key) ? Number(key) - 1 : -1
  const row = letter >= 0 ? letter : digit
  if (row < 0) return null
  if (row < rowCount) return row
  if (customAllowed && row === rowCount) return customRow(rowCount)
  return null
}

/**
 * Split `Lead — tail` option copy into a bold lead and a dim tail so listed
 * choices read like the reference card (name, then description). The tail keeps
 * its separator; rows without one stay all-lead. Purely presentational — the
 * answer submitted is always the untouched row string.
 */
export function splitOptionLabel(label: string): { lead: string; tail: string | null } {
  const text = String(label ?? '').trim()
  const match = /\s[—–-]\s/.exec(text)
  if (!match || match.index <= 0) return { lead: text, tail: null }
  const lead = text.slice(0, match.index).trim()
  const tail = text.slice(match.index).trim()
  if (!lead || !tail) return { lead: text, tail: null }
  return { lead, tail }
}

// ── Form mode: the user fills in one line per field ──────────

export type QuestionFieldKind = 'text' | 'single' | 'multi'

export interface QuestionField {
  label: string
  placeholder: string | null
  kind: QuestionFieldKind
  /** single/multi rows: the selectable choices for this row. */
  options: string[]
}

export function normalizeQuestionFields(raw: unknown): QuestionField[] {
  if (!Array.isArray(raw)) return []
  const fields: QuestionField[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const record = item as Record<string, unknown>
    const label = String(record.label ?? '').trim()
    if (!label) continue
    const placeholder = String(record.placeholder ?? '').trim()
    let kind = String(record.kind ?? '').trim().toLowerCase()
    if (kind !== 'single' && kind !== 'multi') kind = 'text'
    const options: string[] = []
    const rawOptions = record.options
    if (Array.isArray(rawOptions)) {
      for (const option of rawOptions) {
        const text = String(option ?? '').trim()
        if (!text || options.includes(text)) continue
        options.push(text)
        if (options.length >= MAX_QUESTION_OPTIONS) break
      }
    }
    if (kind !== 'text' && options.length === 0) kind = 'text'
    fields.push({
      label,
      placeholder: placeholder || null,
      kind: kind as QuestionFieldKind,
      options,
    })
    if (fields.length >= MAX_QUESTION_OPTIONS) break
  }
  return fields
}

/** Per-row live values: typed text for text rows, picked option indexes otherwise. */
export interface RowAnswers {
  texts: Readonly<Record<number, string>>
  picks: Readonly<Record<number, number[]>>
}

const rowAnswerLine = (
  field: QuestionField,
  answers: RowAnswers,
  index: number,
): { line: string; filled: boolean } => {
  if (field.kind === 'text') {
    const value = String(answers.texts[index] ?? '').trim()
    return { line: `${field.label}: ${value || '(left blank)'}`, filled: Boolean(value) }
  }
  const picked = (answers.picks[index] || [])
    .map((optionIndex) => field.options[optionIndex])
    .filter((option): option is string => Boolean(option))
  if (field.kind === 'single') {
    return { line: `${field.label}: ${picked[0] || '(left blank)'}`, filled: picked.length > 0 }
  }
  return {
    line: `${field.label}: ${picked.length > 0 ? picked.join(', ') : '(none checked)'}`,
    filled: picked.length > 0,
  }
}

/**
 * Assemble the reply text from filled rows: `Label: value` per line,
 * unfilled rows marked honestly (`(left blank)` / `(none checked)`) so the
 * model can see what was skipped. At least one row must be filled to
 * continue. Works across mixed row kinds in the same card.
 */
export function resolveRowsAnswers(
  fields: QuestionField[],
  answers: RowAnswers,
): { ok: true; answer: string } | { ok: false; error: string } {
  const lines: string[] = []
  let filled = 0
  fields.forEach((field, index) => {
    const row = rowAnswerLine(field, answers, index)
    if (row.filled) filled += 1
    lines.push(row.line)
  })
  if (filled === 0) {
    return { ok: false, error: 'Fill in at least one row, or Skip.' }
  }
  return { ok: true, answer: lines.join('\n') }
}

/**
 * Text-only assembly kept as the compatibility entry for plain fill-in
 * cards: values map straight onto rows as free lines.
 */
export function resolveFormAnswers(
  fields: QuestionField[],
  values: Readonly<Record<number, string>>,
): { ok: true; answer: string } | { ok: false; error: string } {
  return resolveRowsAnswers(fields, { texts: values, picks: {} })
}

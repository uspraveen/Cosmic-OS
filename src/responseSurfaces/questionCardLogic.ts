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

// ── Form mode: the user fills in one line per field ──────────

export interface QuestionField {
  label: string
  placeholder: string | null
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
    fields.push({ label, placeholder: placeholder || null })
    if (fields.length >= MAX_QUESTION_OPTIONS) break
  }
  return fields
}

/**
 * Assemble the reply text from filled form rows: `Label: value` per line,
 * unfilled rows marked honestly so the model can see what was skipped.
 * At least one row must be filled to continue.
 */
export function resolveFormAnswers(
  fields: QuestionField[],
  values: Readonly<Record<number, string>>,
): { ok: true; answer: string } | { ok: false; error: string } {
  const lines: string[] = []
  let filled = 0
  fields.forEach((field, index) => {
    const value = String(values[index] ?? '').trim()
    if (value) filled += 1
    lines.push(`${field.label}: ${value || '(left blank)'}`)
  })
  if (filled === 0) {
    return { ok: false, error: 'Fill in at least one line, or Skip.' }
  }
  return { ok: true, answer: lines.join('\n') }
}

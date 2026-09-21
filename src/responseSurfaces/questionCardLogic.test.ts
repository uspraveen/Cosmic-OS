import { describe, expect, it } from 'vitest'
import {
  customRow,
  normalizeQuestionFields,
  normalizeQuestionRows,
  questionCustomAllowed,
  questionKeyRow,
  resolveFormAnswers,
  resolveQuestionAnswer,
} from './questionCardLogic'

describe('question rows', () => {
  it('trims, dedupes, and caps listed options at six', () => {
    const rowsInfo = normalizeQuestionRows([
      '  Shipped feature ',
      'Shipped feature',
      '',
      null,
      'Traction number',
      'A', 'B', 'C', 'D', 'E', 'F',
    ])
    expect(rowsInfo.rows).toHaveLength(6)
    expect(rowsInfo.rows[0]).toBe('Shipped feature')
    expect(rowsInfo.customAllowed).toBe(true)
  })

  it('tolerates non-array input', () => {
    // The gateway payload always carries `options` as a list (same contract
    // as normalizePendingTaskInput); anything else degrades to no options.
    expect(normalizeQuestionRows(undefined).rows).toEqual([])
    expect(normalizeQuestionRows({ a: 1 }).rows).toEqual([])
  })

  it('keeps the custom row even when the asker tried to forbid it with no options', () => {
    const rowsInfo = normalizeQuestionRows([])
    expect(questionCustomAllowed(rowsInfo, false)).toBe(true)
    expect(questionCustomAllowed(normalizeQuestionRows(['A', 'B']), false)).toBe(false)
  })
})

describe('resolveQuestionAnswer', () => {
  const rowsInfo = normalizeQuestionRows(['Shipped feature', 'Traction number'])

  it('resolves a listed option', () => {
    expect(resolveQuestionAnswer({ rowsInfo, customAllowed: true, selected: 1, customValue: '' }))
      .toEqual({ ok: true, answer: 'Traction number' })
  })

  it('resolves a trimmed custom answer on the custom row', () => {
    const result = resolveQuestionAnswer({
      rowsInfo,
      customAllowed: true,
      selected: customRow(2),
      customValue: '  Design run happened  ',
    })
    expect(result).toEqual({ ok: true, answer: 'Design run happened' })
  })

  it('refuses to continue with nothing picked', () => {
    expect(resolveQuestionAnswer({ rowsInfo, customAllowed: true, selected: null, customValue: '' }))
      .toEqual({ ok: false, error: 'Pick an option or type an answer.' })
  })

  it('refuses an empty custom answer with a message that fits the card', () => {
    const withOptions = resolveQuestionAnswer({
      rowsInfo, customAllowed: true, selected: customRow(2), customValue: '   ',
    })
    expect(withOptions.ok).toBe(false)
    expect(withOptions.ok === false && withOptions.error).toContain('pick a listed option')

    const openOnly = resolveQuestionAnswer({
      rowsInfo: normalizeQuestionRows([]), customAllowed: true, selected: customRow(0), customValue: '',
    })
    expect(openOnly.ok).toBe(false)
    expect(openOnly.ok === false && openOnly.error).toContain('Type an answer first.')
  })

  it('ignores an out-of-range selection', () => {
    expect(resolveQuestionAnswer({ rowsInfo, customAllowed: false, selected: 9, customValue: '' }).ok)
      .toBe(false)
  })
})

describe('questionKeyRow', () => {
  const rowCount = 3

  it('maps letters and digits onto listed options', () => {
    expect(questionKeyRow('a', rowCount, true)).toBe(0)
    expect(questionKeyRow('C', rowCount, true)).toBe(2)
    expect(questionKeyRow('2', rowCount, true)).toBe(1)
  })

  it('maps the key past the last option onto the custom row', () => {
    expect(questionKeyRow('d', rowCount, true)).toBe(customRow(rowCount))
    expect(questionKeyRow('4', rowCount, true)).toBe(customRow(rowCount))
  })

  it('maps nothing when the row does not exist or the key is not selectable', () => {
    expect(questionKeyRow('e', rowCount, false)).toBeNull()
    expect(questionKeyRow('f', rowCount, true)).toBeNull()
    expect(questionKeyRow('g', rowCount, true)).toBeNull()
    expect(questionKeyRow('0', rowCount, true)).toBeNull()
    expect(questionKeyRow('Enter', rowCount, true)).toBeNull()
    expect(questionKeyRow('F1', rowCount, true)).toBeNull()
  })
})

describe('form mode', () => {
  it('normalizes form fields, dropping empties and capping at six', () => {
    const fields = normalizeQuestionFields([
      { label: '  Shipped ', placeholder: ' one feature or fix ' },
      { label: '' },
      null,
      { placeholder: 'no label' },
      { label: 'Traction' },
      { label: 'a' }, { label: 'b' }, { label: 'c' }, { label: 'd' }, { label: 'e' },
    ])
    expect(fields).toHaveLength(6)
    expect(fields[0]).toEqual({ label: 'Shipped', placeholder: 'one feature or fix' })
    expect(fields[1]).toEqual({ label: 'Traction', placeholder: null })
    expect(normalizeQuestionFields(undefined)).toEqual([])
  })

  it('assembles labeled lines from filled rows', () => {
    const fields = normalizeQuestionFields([
      { label: 'Shipped', placeholder: null },
      { label: 'Demo', placeholder: null },
    ])
    const result = resolveFormAnswers(fields, { 0: ' intent-to-Gerbers clip ', 1: '' })
    expect(result).toEqual({
      ok: true,
      answer: 'Shipped: intent-to-Gerbers clip\nDemo: (left blank)',
    })
  })

  it('refuses to continue with every row blank', () => {
    const fields = normalizeQuestionFields([{ label: 'Shipped', placeholder: null }])
    const result = resolveFormAnswers(fields, { 0: '   ' })
    expect(result.ok).toBe(false)
    expect(!result.ok && result.error).toContain('at least one line')
  })
})

import { describe, expect, it } from 'vitest'
import {
  mergeSheetRunProgress,
  normalizeSheetProgress,
  parseSheetRange,
  type SheetProgressState,
} from './sheetRunPreview'

const reading = (overrides: Record<string, unknown>) =>
  normalizeSheetProgress({ kind: 'sheet_run', ...overrides })!

describe('parseSheetRange', () => {
  it('parses tab-qualified bounded ranges', () => {
    expect(parseSheetRange('Jobs!A1:I10')).toEqual({
      tab: 'Jobs',
      startRow: 1,
      startCol: 1,
      endRow: 10,
      endCol: 9,
    })
  })

  it('parses quoted tab names', () => {
    expect(parseSheetRange("'My Sheet'!B2:D4")?.tab).toBe('My Sheet')
    expect(parseSheetRange("'My Sheet'!B2:D4")?.startCol).toBe(2)
  })

  it('parses open-ended and tab-less ranges', () => {
    expect(parseSheetRange('A1:C')).toEqual({ tab: '', startRow: 1, startCol: 1, endRow: null, endCol: 3 })
    expect(parseSheetRange('B3')).toEqual({ tab: '', startRow: 3, startCol: 2, endRow: null, endCol: null })
  })

  it('rejects garbage', () => {
    expect(parseSheetRange('')).toBeNull()
    expect(parseSheetRange('Jobs!')).toBeNull()
    expect(parseSheetRange('hello world')).toBeNull()
  })
})

describe('mergeSheetRunProgress', () => {
  it('applies the first write at its absolute position', () => {
    const merged = mergeSheetRunProgress(
      undefined,
      reading({
        op: 'update_cells',
        phase: 'writing',
        range: 'Jobs!A1:C2',
        values: [['Role', 'Comp', 'Why'], ['MTS', '$210K', 'Infra']],
      }),
    ) as SheetProgressState
    expect(merged.grid).toEqual([
      ['Role', 'Comp', 'Why'],
      ['MTS', '$210K', 'Infra'],
    ])
    expect(merged.rows).toBe(2)
    expect(merged.cols).toBe(3)
  })

  it('appends below existing rows when the range says so', () => {
    const first = mergeSheetRunProgress(
      undefined,
      reading({ op: 'update_cells', range: 'A1:B2', values: [['A', 'B'], ['1', '2']] }),
    )
    const second = mergeSheetRunProgress(
      first,
      reading({ op: 'append_rows', range: 'A3:B4', values: [['3', '4'], ['5', '6']] }),
    ) as SheetProgressState
    expect(second.grid).toEqual([
      ['A', 'B'],
      ['1', '2'],
      ['3', '4'],
      ['5', '6'],
    ])
    expect(second.trail?.map((entry) => entry.op)).toEqual(['update_cells', 'append_rows'])
  })

  it('is idempotent when the same reading is folded again', () => {
    const first = mergeSheetRunProgress(
      undefined,
      reading({ op: 'append_rows', range: 'A1:B1', values: [['x', 'y']] }),
    )
    const replayed = mergeSheetRunProgress(first, reading({ op: 'append_rows', range: 'A1:B1', values: [['x', 'y']] }))
    expect((replayed as SheetProgressState).grid).toEqual([['x', 'y']])
  })

  it('clears a range without losing the rest of the grid', () => {
    const first = mergeSheetRunProgress(
      undefined,
      reading({ op: 'update_cells', range: 'A1:B2', values: [['A', 'B'], ['1', '2']] }),
    )
    const cleared = mergeSheetRunProgress(first, reading({ op: 'clear_range', range: 'A2:B2' })) as SheetProgressState
    expect(cleared.grid).toEqual([
      ['A', 'B'],
      ['', ''],
    ])
  })

  it('keeps header state and grid across formatting and done events', () => {
    const first = mergeSheetRunProgress(
      undefined,
      reading({ op: 'update_cells', range: 'A1:B1', values: [['H1', 'H2']] }),
    )
    const formatted = mergeSheetRunProgress(
      first,
      reading({ op: 'format_header_row', header: { formatted: true, color: '#E8F0FE', frozen_rows: 1 } }),
    ) as SheetProgressState
    expect(formatted.header).toEqual({ formatted: true, color: '#E8F0FE', frozenRows: 1 })
    expect(formatted.grid).toEqual([['H1', 'H2']])
    const done = mergeSheetRunProgress(formatted, reading({ op: 'verify', phase: 'done' })) as SheetProgressState
    expect(done.phase).toBe('done')
    expect(done.grid).toEqual([['H1', 'H2']])
    expect(done.header).toEqual({ formatted: true, color: '#E8F0FE', frozenRows: 1 })
  })

  it('narrates a native table wrap without touching the grid', () => {
    const first = mergeSheetRunProgress(
      undefined,
      reading({
        op: 'update_cells',
        range: 'Jobs!A1:C2',
        values: [['Role', 'Comp', 'Status'], ['MTS', '$210K', 'To apply']],
      }),
    )
    const tabled = mergeSheetRunProgress(
      first,
      reading({
        op: 'create_table',
        range: 'Jobs!A1:C2',
        header: { formatted: true, table: true, table_name: 'Jobs', frozen_rows: 1 },
      }),
    ) as SheetProgressState
    expect(tabled.grid).toEqual([
      ['Role', 'Comp', 'Status'],
      ['MTS', '$210K', 'To apply'],
    ])
    expect(tabled.header).toEqual({ formatted: true, table: true, tableName: 'Jobs', frozenRows: 1 })
    expect(tabled.trail?.map((entry) => entry.op)).toEqual(['update_cells', 'create_table'])
    expect(tabled.trail?.[1].rows).toBe(0)
  })

  it('keeps a create_table trail entry even with no prior grid', () => {
    const tabled = mergeSheetRunProgress(
      undefined,
      reading({ op: 'create_table', range: 'Jobs!A1:C2', header: { formatted: true, table: true, table_name: 'Jobs' } }),
    ) as SheetProgressState
    expect(tabled.grid).toBeUndefined()
    expect(tabled.trail?.map((entry) => entry.op)).toEqual(['create_table'])
    expect(tabled.header?.table).toBe(true)
  })

  it('caps the grid and the trail', () => {
    let merged: SheetProgressState | undefined
    for (let batch = 0; batch < 10; batch += 1) {
      const startRow = batch * 60 + 1
      merged = mergeSheetRunProgress(
        merged,
        reading({
          op: 'append_rows',
          range: `A${startRow}:A${startRow + 59}`,
          values: Array.from({ length: 60 }, (_, row) => [`r${batch}-${row}`]),
        }),
      ) as SheetProgressState
    }
    expect(merged?.grid?.length).toBeLessThanOrEqual(400)
    expect(merged?.trail?.length).toBeLessThanOrEqual(5)
  })
})

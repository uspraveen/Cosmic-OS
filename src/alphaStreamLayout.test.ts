import { describe, expect, it } from 'vitest'
import { buildAlphaStreamSegments, ensureAlphaConsoleAnchor, measureAssistantStreamLength } from './alphaStreamLayout'

describe('alphaStreamLayout', () => {
  it('splits markdown content at the alpha anchor offset', () => {
    const { segments, hasAnchors } = buildAlphaStreamSegments({
      content: 'Before Alpha runs.\n\nAfter Alpha finishes.',
      alphaConsoleAnchors: [{ taskId: 'tsk_alpha', offset: 18 }],
    })

    expect(hasAnchors).toBe(true)
    expect(segments).toEqual([
      { kind: 'content', content: 'Before Alpha runs.' },
      { kind: 'alpha_console', taskId: 'tsk_alpha' },
      { kind: 'content', content: '\n\nAfter Alpha finishes.' },
    ])
  })

  it('measures response block length for anchor placement', () => {
    expect(measureAssistantStreamLength('', [
      { id: 'markdown_1', type: 'markdown', text: 'hello' },
      { id: 'code_1', type: 'code', code: 'print(1)' },
    ])).toBe(13)
  })

  it('dedupes alpha anchors per task id', () => {
    const anchors = ensureAlphaConsoleAnchor(undefined, 'tsk_alpha', 12)
    expect(ensureAlphaConsoleAnchor(anchors, 'tsk_alpha', 99)).toEqual(anchors)
  })
})

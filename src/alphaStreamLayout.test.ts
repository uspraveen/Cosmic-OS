import { describe, expect, it } from 'vitest'
import { buildAlphaStreamSegments, ensureAlphaConsoleAnchor, measureAssistantStreamLength, snapAlphaAnchorOffset } from './alphaStreamLayout'

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

  it('snaps mid-word anchors back to the preceding word boundary', () => {
    const text = 'Let me read the blog post Alpha wrote.'
    const offset = text.indexOf('blog') + 2
    expect(snapAlphaAnchorOffset(text, offset)).toBe(text.indexOf(' blog'))
  })

  it('keeps anchors that already sit at paragraph boundaries', () => {
    const text = 'Before Alpha runs.\n\nAfter Alpha finishes.'
    expect(snapAlphaAnchorOffset(text, 18)).toBe(18)
    expect(snapAlphaAnchorOffset(text, 20)).toBe(20)
  })

  it('never snaps past the lookback window', () => {
    const text = `${'word '.repeat(300)}tail`
    const offset = text.length - 2
    expect(snapAlphaAnchorOffset(text, offset)).toBe(text.lastIndexOf(' '))
  })

  it('snaps glued-sentence anchors so no segment tears a word', () => {
    const text = 'I am gonna fire X search.Fine X results came back.'
    const offset = text.indexOf('Fine') + 2
    const snapped = snapAlphaAnchorOffset(text, offset)
    expect(snapped).toBeLessThan(offset)
    expect(snapAlphaAnchorOffset(text, snapped)).toBe(snapped)
  })

  it('splits markdown blocks at snapped boundaries without tearing words', () => {
    const content = 'Read the blog post now. Then report back.'
    const { segments } = buildAlphaStreamSegments({
      content,
      alphaConsoleAnchors: [{ taskId: 'tsk_alpha', offset: 11 }],
    })
    const before = segments[0]
    const after = segments[2]
    if (
      !after
      || before.kind !== 'content'
      || typeof before.content !== 'string'
      || after.kind !== 'content'
      || typeof after.content !== 'string'
    ) {
      throw new Error('expected content segments around the console')
    }
    expect(before.content).toBe('Read the')
    expect(after.content).toBe(' blog post now. Then report back.')
  })
})

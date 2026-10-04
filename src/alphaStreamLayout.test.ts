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

  it('rebases the second and later anchors against what is already split off', () => {
    const { segments } = buildAlphaStreamSegments({
      content: 'One two three four five six.',
      alphaConsoleAnchors: [
        { taskId: 'tsk_one', offset: 8 },
        { taskId: 'tsk_two', offset: 19 },
      ],
    })

    expect(segments).toEqual([
      { kind: 'content', content: 'One two ' },
      { kind: 'alpha_console', taskId: 'tsk_one' },
      { kind: 'content', content: 'three four ' },
      { kind: 'alpha_console', taskId: 'tsk_two' },
      { kind: 'content', content: 'five six.' },
    ])
  })

  it('rebases later anchors in response-blocks mode too', () => {
    const { segments } = buildAlphaStreamSegments({
      responseBlocks: [
        { id: 'markdown_1', type: 'markdown', text: 'One two ' },
        { id: 'markdown_2', type: 'markdown', text: 'three four five six.' },
      ],
      alphaConsoleAnchors: [
        { taskId: 'tsk_one', offset: 8 },
        { taskId: 'tsk_two', offset: 19 },
      ],
    })

    expect(segments).toEqual([
      { kind: 'content', blocks: [{ id: 'markdown_1', type: 'markdown', text: 'One two ' }] },
      { kind: 'alpha_console', taskId: 'tsk_one' },
      { kind: 'content', blocks: [{ id: 'markdown_2', type: 'markdown', text: 'three four ' }] },
      { kind: 'alpha_console', taskId: 'tsk_two' },
      { kind: 'content', blocks: [{ id: 'markdown_2_tail', type: 'markdown', text: 'five six.' }] },
    ])
  })

  it('emits one activity segment per anchor, preserving same-offset arrival order', () => {
    const { segments, hasAnchors } = buildAlphaStreamSegments({
      content: 'Hello world.',
      activityAnchors: [
        { id: 'activity_a', offset: 0 },
        { id: 'activity_b', offset: 0 },
      ],
    })

    expect(hasAnchors).toBe(true)
    expect(segments).toEqual([
      { kind: 'activity', anchorId: 'activity_a' },
      { kind: 'activity', anchorId: 'activity_b' },
      { kind: 'content', content: 'Hello world.' },
    ])
  })

  it('interleaves activity anchors with console anchors at their own offsets', () => {
    const { segments } = buildAlphaStreamSegments({
      content: 'One. Two. Three.',
      alphaConsoleAnchors: [{ taskId: 'tsk_alpha', offset: 10 }],
      activityAnchors: [{ id: 'activity_a', offset: 5 }],
    })

    expect(segments).toEqual([
      { kind: 'content', content: 'One. ' },
      { kind: 'activity', anchorId: 'activity_a' },
      { kind: 'content', content: 'Two. ' },
      { kind: 'alpha_console', taskId: 'tsk_alpha' },
      { kind: 'content', content: 'Three.' },
    ])
  })

  it('places an activity row before a card anchored at the same offset', () => {
    const { segments } = buildAlphaStreamSegments({
      content: 'One. Two three.',
      alphaConsoleAnchors: [{ taskId: 'tsk_alpha', offset: 5 }],
      activityAnchors: [{ id: 'activity_a', offset: 5 }],
    })

    expect(segments).toEqual([
      { kind: 'content', content: 'One. ' },
      { kind: 'activity', anchorId: 'activity_a' },
      { kind: 'alpha_console', taskId: 'tsk_alpha' },
      { kind: 'content', content: 'Two three.' },
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


describe('approval card positions', () => {
  it('keeps a card at its invocation position through final snapshots and reload', () => {
    const card = { id: 'vault:one', type: 'vault_permission_request', streamOffset: 9 }
    const responseBlocks = [{ id: 'text', type: 'markdown', text: 'Before.\n\nAfter.' }, card]
    for (const blocks of [responseBlocks, JSON.parse(JSON.stringify(responseBlocks))]) {
      const result = buildAlphaStreamSegments({ responseBlocks: blocks })
      expect(result.segments.map((segment) => segment.kind)).toEqual(['content', 'action', 'content'])
      expect(result.segments[0]).toMatchObject({ blocks: [{ text: 'Before.\n\n' }] })
      expect(result.segments[1]).toMatchObject({ block: card })
      expect(result.segments[2]).toMatchObject({ blocks: [{ text: 'After.' }] })
    }
  })
  it('preserves live prose before the first block snapshot', () => {
    const result = buildAlphaStreamSegments({ content: 'Before.\n\nAfter.', responseBlocks: [
      { id: 'vault:one', type: 'vault_permission_request', streamOffset: 9 },
    ] })
    expect(result.segments.map((segment) => segment.kind)).toEqual(['content', 'action', 'content'])
    expect(result.segments[2]).toMatchObject({ content: 'After.' })
  })
})


describe('progress rows preserve complete sentences', () => {
  const content = "Here's the package.\n\nThat's the complete launch package - both cards are filled in."
  const offset = content.indexOf("That's the") + "That's the".length
  it('puts a tool completion before the sentence it arrived during', () => {
    const { segments } = buildAlphaStreamSegments({ content, activityAnchors: [{ id: 'tool_done', offset }] })
    expect(segments).toEqual([
      { kind: 'content', content: "Here's the package.\n\n" },
      { kind: 'activity', anchorId: 'tool_done' },
      { kind: 'content', content: "That's the complete launch package - both cards are filled in." },
    ])
  })
  it('uses the same placement before completion, in final blocks, and after reload', () => {
    const live = buildAlphaStreamSegments({ content: content.slice(0, offset), activityAnchors: [{ id: 'tool_done', offset }] })
    expect(live.segments[live.segments.length - 1]).toEqual({ kind: 'content', content: "That's the" })
    const blocks = [{ id: 'answer', type: 'markdown', text: content }]
    for (const responseBlocks of [blocks, JSON.parse(JSON.stringify(blocks))]) {
      const result = buildAlphaStreamSegments({ responseBlocks, activityAnchors: [{ id: 'tool_done', offset }] })
      expect(result.segments[0]).toMatchObject({ blocks: [{ text: "Here's the package.\n\n" }] })
      expect(result.segments[1]).toEqual({ kind: 'activity', anchorId: 'tool_done' })
      expect(result.segments[2]).toMatchObject({ blocks: [{ text: "That's the complete launch package - both cards are filled in." }] })
    }
  })
})

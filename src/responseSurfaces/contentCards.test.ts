import { describe, expect, it } from 'vitest'
import { contentCardKicker, groupContentCardBlocks, normalizeContentCard } from './contentCards'

describe('content cards', () => {
  it('normalizes a social post and ignores privileged actions', () => {
    const card = normalizeContentCard({
      id: 'content_card_1',
      type: 'content_card',
      preset: 'social_post',
      brand: 'x',
      title: 'Draft 1',
      body: 'Ship it tonight.',
      tags: ['#cosmic'],
      actions: [{ type: 'post' }, { type: 'copy' }, { type: 'open_url', url: 'http://x.com' }],
      group_id: 'x_drafts',
      variant_index: 1,
      variant_total: 2,
    }, 'fallback')

    expect(card?.brand).toBe('x')
    expect(card?.characterLimit).toBe(280)
    expect(card?.actions).toEqual([{ type: 'copy', label: 'Copy', text: 'Ship it tonight.' }])
    expect(card?.group).toEqual({ id: 'x_drafts', index: 1, total: 2 })
    expect(contentCardKicker(card!)).toBe('X draft')
  })

  it('rejects javascript urls and stacks grouped cards', () => {
    const first = normalizeContentCard({
      id: 'a',
      type: 'content_card',
      title: 'One',
      body: 'alpha',
      group_id: 'g',
      actions: [{ type: 'open_url', url: 'javascript:alert(1)' }],
    }, 'a')
    const second = normalizeContentCard({
      id: 'b',
      type: 'content_card',
      title: 'Two',
      body: 'beta',
      group_id: 'g',
    }, 'b')
    expect(first?.actions.some((item) => item.type === 'open_url')).toBe(false)
    const grouped = groupContentCardBlocks([first!, second!])
    expect(grouped).toHaveLength(1)
    expect(grouped[0].kind).toBe('stack')
  })

  it('normalizes stat, metrics, flow, note and concept sections', () => {
    const card = normalizeContentCard({
      id: 'm1',
      type: 'content_card',
      title: 'Reported results',
      sections: [
        { type: 'metrics', stats: [{ label: 'Optimized decode', value: '191 tok/s', highlight: true }, { label: 'No value' }] },
        { type: 'note', text: 'Reported measurements, not independently reproduced.' },
        { type: 'flow', nodes: [{ title: 'Source ruleset' }, { name: 'Constraint IR', subtitle: 'Typed rules' }], connectors: ['translates to'], layout: 'grid' },
        { type: 'concepts', items: [{ icon: 'Network', title: 'Expert parallelism', body: 'Assign MoE experts to GPUs.' }] },
        { type: 'stat', label: 'Total GPU memory', value: '192 GB', qualifier: '4 x 48 GB' },
      ],
    }, 'm1')

    expect(card?.sections.map((section) => section.type)).toEqual(['metrics', 'note', 'flow', 'concepts', 'stat'])
    expect(contentCardKicker(card!)).toBe('Results')
    const metrics = card!.sections.find((section) => section.type === 'metrics') as any
    expect(metrics.stats).toEqual([{ label: 'Optimized decode', value: '191 tok/s', highlight: true }])
    const flow = card!.sections.find((section) => section.type === 'flow') as any
    expect(flow.nodes).toEqual([
      { title: 'Source ruleset', subtitle: null },
      { title: 'Constraint IR', subtitle: 'Typed rules' },
    ])
    expect(flow.connectors).toEqual(['translates to'])
    expect(flow.layout).toBe('grid')
    const concepts = card!.sections.find((section) => section.type === 'concepts') as any
    expect(concepts.items[0].icon).toBe('network')
    const stat = card!.sections.find((section) => section.type === 'stat') as any
    expect(stat.qualifier).toBe('4 x 48 GB')
  })

  it('drops a flow with a single node and a stat without a value', () => {
    const card = normalizeContentCard({
      id: 'f1',
      type: 'content_card',
      title: 'Broken shapes',
      sections: [
        { type: 'flow', nodes: [{ title: 'Only one' }] },
        { type: 'stat', label: 'No value' },
      ],
    }, 'f1')

    expect(card?.sections).toHaveLength(0)
  })
})

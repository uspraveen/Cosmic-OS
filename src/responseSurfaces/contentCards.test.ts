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
})

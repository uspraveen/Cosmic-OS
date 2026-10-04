import { describe, expect, it } from 'vitest'
import { appendStreamText, mergeCompletedStreamText } from './streamText'

describe('appendStreamText', () => {
  it('joins token fragments without inventing word boundaries', () => {
    const chunks = ["That's", ' the', ' comp', 'lete', ' launch', ' package', '.']
    expect(chunks.reduce((text, chunk) => appendStreamText(text, chunk), '')).toBe(
      "That's the complete launch package.",
    )
  })
  it('preserves decimals and URLs split between token chunks', () => {
    expect(['0.', '051', ' https://example.', 'com'].reduce((text, chunk) => appendStreamText(text, chunk), '')).toBe(
      '0.051 https://example.com',
    )
  })
  it('preserves explicit separators emitted between model turns', () => {
    expect(['Before.', '\n\n', 'After.'].reduce((text, chunk) => appendStreamText(text, chunk), '')).toBe(
      'Before.\n\nAfter.',
    )
  })
  it('preserves whitespace and empty chunks exactly', () => {
    expect(appendStreamText('Hello ', 'world')).toBe('Hello world')
    expect(appendStreamText('Hello', '')).toBe('Hello')
  })
})

describe('mergeCompletedStreamText', () => {
  it('prefers the completed text when it has more content after tool turns', () => {
    expect(
      mergeCompletedStreamText(
        'Let me search it.',
        'Let me search it.\n\nFound it!',
      ),
    ).toBe('Let me search it.\n\nFound it!')
  })

  it('keeps richer paragraph breaks when normalized text matches', () => {
    expect(
      mergeCompletedStreamText(
        'Let me search it. Found it!',
        'Let me search it.\n\nFound it!',
      ),
    ).toBe('Let me search it.\n\nFound it!')
  })

  it('does not truncate when the streamed text is only a prefix', () => {
    expect(
      mergeCompletedStreamText(
        'Let me search it.',
        'Let me search it.\n\nFound it!',
      ),
    ).toBe('Let me search it.\n\nFound it!')
  })
})

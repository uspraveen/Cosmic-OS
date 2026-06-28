import { describe, expect, it } from 'vitest'
import { appendStreamText, mergeCompletedStreamText } from './streamText'

describe('appendStreamText', () => {
  it('inserts a paragraph break after sentence-ending punctuation before a new sentence', () => {
    expect(appendStreamText('Let me grab the artifact!', 'Got it - firing Alpha now.')).toBe(
      'Let me grab the artifact!\n\nGot it - firing Alpha now.',
    )
    expect(appendStreamText('Let me search it.', 'Found it!')).toBe(
      'Let me search it.\n\nFound it!',
    )
  })

  it('inserts a paragraph break when the next sentence starts lowercase', () => {
    expect(appendStreamText('Let me search it.', 'found the repo.')).toBe(
      'Let me search it.\n\nfound the repo.',
    )
  })

  it('inserts a word boundary between alphanumeric characters', () => {
    expect(appendStreamText('Hello', 'world')).toBe('Hello world')
    expect(appendStreamText('Let me search it', 'Found')).toBe('Let me search it Found')
  })

  it('preserves existing whitespace', () => {
    expect(appendStreamText('Hello ', 'world')).toBe('Hello world')
    expect(appendStreamText('Let me search it.', ' Found it!')).toBe('Let me search it. Found it!')
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

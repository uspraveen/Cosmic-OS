/**
 * Cosmic replies are product copy, not a chatbot dump. Models still sprinkle
 * status emoji (trailing ✅, ✨ bullets, 🚀 emphasis). Strip them from prose
 * while leaving fenced/inline code alone.
 */

const FENCE_OR_INLINE_CODE = /(```[\s\S]*?```|`[^`]+`)/g
const PICTOGRAPHIC = /\p{Extended_Pictographic}/gu
const STATUS_MARKS = /[✓✔☑❌⚠]/g
const VARIATION_SELECTORS = /[\uFE0E\uFE0F]/g
const ZWJ = /\u200D/g

const scrubChunk = (chunk: string): string =>
  chunk
    .replace(PICTOGRAPHIC, '')
    .replace(STATUS_MARKS, '')
    .replace(VARIATION_SELECTORS, '')
    .replace(ZWJ, '')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/[ \t]+([,.;:!?])/g, '$1')
    .replace(/[ \t]+$/gm, '')
    .replace(/^ +(?=[A-Za-z0-9])/gm, '')

export const scrubAssistantProse = (text: string): string => {
  if (!text) return text
  const parts = text.split(FENCE_OR_INLINE_CODE)
  return parts
    .map((part, index) => {
      if (index % 2 === 1) return part
      const original = part
      const scrubbed = scrubChunk(part)
      // scrubChunk trims line-leading/trailing whitespace, but chunk edges
      // adjacent to a code span are mid-line, not line edges. If the source
      // had a space separating prose from code, keep exactly one so words
      // don't glue to pills (e.g. `at` + `/opt/...` -> `at/opt/...`).
      if (parts.length === 1) return scrubbed
      const touchesCodeBefore = index > 0
      const touchesCodeAfter = index + 1 < parts.length
      const hadLeadingSpace = /^[ \t]/.test(original)
      const hadTrailingSpace = /[ \t]$/.test(original)
      let restored = scrubbed
      const needsLeading =
        touchesCodeBefore && hadLeadingSpace && !/^\s/.test(restored)
      const needsTrailing =
        touchesCodeAfter && hadTrailingSpace && !/\s$/.test(restored)
      if (needsLeading && needsTrailing && restored === '') return ' '
      if (needsLeading) restored = ` ${restored}`
      if (needsTrailing) restored = `${restored} `
      return restored
    })
    .join('')
}

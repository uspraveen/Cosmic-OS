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
  return text
    .split(FENCE_OR_INLINE_CODE)
    .map((part, index) => (index % 2 === 1 ? part : scrubChunk(part)))
    .join('')
}

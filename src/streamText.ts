// Response chunks are token deltas, not complete words or sentences. Turn
// separators arrive explicitly from the orchestrator; guessing here changes
// the text and invalidates every progress offset measured against it.
export const appendStreamText = (current: string | undefined, incoming: unknown): string =>
  String(current || '') + String(incoming || '')

export const mergeCompletedStreamText = (current: string | undefined, completed: unknown): string => {
  const prev = String(current || '')
  const finalText = String(completed || '')
  if (!prev) {
    return finalText
  }
  if (!finalText) {
    return prev
  }

  const normalizedPrev = prev.replace(/\s+/g, ' ').trim()
  const normalizedFinal = finalText.replace(/\s+/g, ' ').trim()
  if (!normalizedPrev || !normalizedFinal) {
    return finalText || prev
  }

  if (normalizedPrev === normalizedFinal) {
    const prevParagraphs = (prev.match(/\n{2,}/g) || []).length
    const finalParagraphs = (finalText.match(/\n{2,}/g) || []).length
    if (finalParagraphs > prevParagraphs) {
      return finalText
    }
    if (prevParagraphs > finalParagraphs) {
      return prev
    }
    return finalText.length >= prev.length ? finalText : prev
  }

  if (normalizedFinal.startsWith(normalizedPrev)) {
    return finalText
  }

  if (normalizedPrev.startsWith(normalizedFinal) && normalizedPrev.length > normalizedFinal.length) {
    return prev
  }

  return finalText
}

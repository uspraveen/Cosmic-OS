export const appendStreamText = (current: string | undefined, incoming: unknown): string => {
  const prev = String(current || '')
  const next = String(incoming || '')
  if (!next) {
    return prev
  }
  if (!prev) {
    return next
  }

  const prevEnd = prev.slice(-1)
  const nextStart = next.slice(0, 1)
  if (!prevEnd || !nextStart || /\s/.test(prevEnd) || /\s/.test(nextStart)) {
    return `${prev}${next}`
  }
  if (/[\.\!\?\:\u2026]/.test(prevEnd) && /[A-Za-z0-9"'`(\[]/.test(nextStart)) {
    return `${prev}\n\n${next}`
  }
  if (/[A-Za-z0-9]/.test(prevEnd) && /[A-Za-z0-9]/.test(nextStart)) {
    return `${prev} ${next}`
  }
  return `${prev}${next}`
}

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

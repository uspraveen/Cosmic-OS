/**
 * What a stopped turn must keep on screen.
 *
 * Stopping a response removes its empty placeholder bubble — a turn that never
 * produced anything should not leave a blank card behind. But "no prose" is
 * not "nothing to show": a browser run, a deck build, a sheet, an Alpha console
 * or the inline progress rows can all live in a message whose text is still
 * empty. Treating those as placeholders is how the Stop button made a live
 * browser card vanish mid-run (2026-10-08).
 */
export type LiveWorkMessage = {
  browserProgress?: unknown
  slideProgress?: unknown
  sheetsProgress?: unknown
  alphaTerminalLog?: unknown[] | null
  activityLog?: unknown[] | null
  browserConsoleAnchors?: unknown[] | null
  producedArtifacts?: unknown[] | null
  responseBlocks?: unknown[] | null
}

const nonEmpty = (value: unknown[] | null | undefined): boolean => Array.isArray(value) && value.length > 0

export const messageCarriesLiveWork = (message: LiveWorkMessage | null | undefined): boolean => {
  if (!message) return false
  return Boolean(
    message.browserProgress
    || message.slideProgress
    || message.sheetsProgress
    || nonEmpty(message.alphaTerminalLog)
    || nonEmpty(message.activityLog)
    || nonEmpty(message.browserConsoleAnchors)
    || nonEmpty(message.producedArtifacts)
    || nonEmpty(message.responseBlocks),
  )
}

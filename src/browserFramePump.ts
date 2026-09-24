/** Decode only the newest requested browser frame before replacing painted pixels.
 * A slow JPEG must never put an older page back on screen after a newer one.
 */
export const createLatestFramePump = <Frame>(
  decode: (source: string) => Promise<Frame>,
  paint: (frame: Frame, source: string) => void,
  onError: (source: string) => void,
) => {
  let requested = ''
  let settled = ''
  let running = false
  let stopped = false

  const run = async () => {
    if (running || stopped) return
    running = true
    try {
      while (!stopped && requested && requested !== settled) {
        const source = requested
        try {
          const frame = await decode(source)
          if (stopped) break
          if (source !== requested) continue
          paint(frame, source)
        } catch {
          if (stopped) break
          if (source !== requested) continue
          onError(source)
        }
        settled = source
      }
    } finally {
      running = false
    }
  }

  return {
    offer(source: string) {
      requested = source
      if (!running) void run()
    },
    stop() {
      stopped = true
      requested = ''
    },
  }
}

export const decodeBrowserFrame = async (source: string): Promise<HTMLImageElement> => {
  const image = new Image()
  image.decoding = 'async'
  image.src = source
  await image.decode()
  if (!(image.naturalWidth > 0) || !(image.naturalHeight > 0)) {
    throw new Error('Browser frame has no dimensions')
  }
  return image
}

export const paintBrowserFrame = (
  canvas: HTMLCanvasElement | null,
  frame: HTMLImageElement,
  maxWidth = 0,
): void => {
  if (!canvas) return
  const context = canvas.getContext('2d', { alpha: false })
  if (!context) throw new Error('Browser frame canvas is unavailable')
  // The inline card is only 224 CSS pixels wide. Keep its persistent bitmap
  // at 2× that width; the expanded takeover canvas retains native resolution.
  const scale = maxWidth > 0 ? Math.min(1, maxWidth / frame.naturalWidth) : 1
  const width = Math.max(1, Math.round(frame.naturalWidth * scale))
  const height = Math.max(1, Math.round(frame.naturalHeight * scale))
  if (canvas.width !== width) canvas.width = width
  if (canvas.height !== height) canvas.height = height
  context.drawImage(frame, 0, 0, width, height)
}

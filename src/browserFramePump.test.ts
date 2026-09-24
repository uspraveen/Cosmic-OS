import { describe, expect, it } from 'vitest'
import { createLatestFramePump, paintBrowserFrame } from './browserFramePump'

const deferred = <T>() => {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

const settle = async () => { await Promise.resolve(); await Promise.resolve() }

describe('createLatestFramePump', () => {
  it('keeps the painted frame while decoding and skips frames superseded in flight', async () => {
    const pending = new Map<string, ReturnType<typeof deferred<string>>>()
    const painted: string[] = []
    const pump = createLatestFramePump(
      (source) => {
        const next = deferred<string>()
        pending.set(source, next)
        return next.promise
      },
      (frame) => painted.push(frame),
      () => { throw new Error('unexpected decode failure') },
    )
    pump.offer('first')
    pending.get('first')!.resolve('first')
    await settle()
    expect(painted).toEqual(['first'])

    pump.offer('stale')
    pump.offer('newest')
    expect(painted).toEqual(['first'])
    pending.get('stale')!.resolve('stale')
    await settle()
    expect(painted).toEqual(['first'])
    expect(pending.has('newest')).toBe(true)
    pending.get('newest')!.resolve('newest')
    await settle()
    expect(painted).toEqual(['first', 'newest'])
    pump.stop()
  })

  it('reports a failed current frame and continues with the next one', async () => {
    const failed: string[] = []
    const painted: string[] = []
    const pump = createLatestFramePump(
      async (source) => {
        if (source === 'expired-preview') throw new Error('expired')
        return source
      },
      (frame) => painted.push(frame),
      (source) => failed.push(source),
    )
    pump.offer('expired-preview')
    await settle()
    expect(failed).toEqual(['expired-preview'])
    pump.offer('fresh-frame')
    await settle()
    expect(painted).toEqual(['fresh-frame'])
    pump.stop()
  })

  it('does not resize and clear a canvas between same-sized frames', () => {
    let sizeWrites = 0
    let width = 1280
    let height = 720
    const draws: string[] = []
    const canvas = {
      get width() { return width },
      set width(value: number) { sizeWrites += 1; width = value },
      get height() { return height },
      set height(value: number) { sizeWrites += 1; height = value },
      getContext: () => ({ drawImage: (frame: { id: string }) => draws.push(frame.id) }),
    } as unknown as HTMLCanvasElement
    const frame = (id: string) => ({ id, naturalWidth: 1280, naturalHeight: 720 }) as unknown as HTMLImageElement
    paintBrowserFrame(canvas, frame('first'))
    paintBrowserFrame(canvas, frame('second'))
    expect(sizeWrites).toBe(0)
    expect(draws).toEqual(['first', 'second'])
  })

  it('uses a smaller persistent bitmap for the inline card', () => {
    const canvas = {
      width: 1280,
      height: 720,
      getContext: () => ({ drawImage: () => {} }),
    } as unknown as HTMLCanvasElement
    const frame = { naturalWidth: 1280, naturalHeight: 720 } as HTMLImageElement
    paintBrowserFrame(canvas, frame, 448)
    expect([canvas.width, canvas.height]).toEqual([448, 252])
  })

  it('never paints a decode that finishes after the card unmounts', async () => {
    const next = deferred<string>()
    const painted: string[] = []
    const pump = createLatestFramePump(() => next.promise, (frame) => painted.push(frame), () => {})
    pump.offer('frame')
    pump.stop()
    next.resolve('frame')
    await settle()
    expect(painted).toEqual([])
  })
})

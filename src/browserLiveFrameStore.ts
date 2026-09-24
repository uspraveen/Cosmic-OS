/** Ephemeral browser video state. A frame changes only its subscribed run card,
 * never the chat transcript or the root App state. */
export interface BrowserLiveFrameSnapshot {
  frame: string
  changedAt: number
  receivedAt: number
}

const EMPTY_FRAME: BrowserLiveFrameSnapshot = Object.freeze({ frame: '', changedAt: 0, receivedAt: 0 })
const MAX_FRAME_KEYS = 32 // task id + request id for at most 16 recent runs
const MAX_ENDED_TASKS = 128

type SchedulePaint = (callback: () => void) => void

export const createBrowserLiveFrameStore = (
  schedulePaint: SchedulePaint = (callback) => { requestAnimationFrame(callback) },
  now: () => number = () => Date.now(),
) => {
  const frames = new Map<string, BrowserLiveFrameSnapshot>()
  const listeners = new Map<string, Set<() => void>>()
  const pending = new Set<string>()
  const endedTasks = new Set<string>()
  let flushQueued = false

  const queue = (key: string) => {
    pending.add(key)
    if (flushQueued) return
    flushQueued = true
    schedulePaint(() => {
      flushQueued = false
      const changed = [...pending]
      pending.clear()
      for (const entry of changed) {
        for (const listener of listeners.get(entry) || []) listener()
      }
    })
  }

  const remove = (key: string) => {
    if (key && frames.delete(key)) queue(key)
  }

  const put = (key: string, frame: string, at: number) => {
    if (!key) return
    const previous = frames.get(key)
    frames.set(key, {
      frame,
      changedAt: previous?.frame === frame ? previous.changedAt : at,
      receivedAt: at,
    })
    queue(key)
  }

  return {
    getSnapshot(key: string): BrowserLiveFrameSnapshot {
      return frames.get(key) || EMPTY_FRAME
    },
    subscribe(key: string, listener: () => void): () => void {
      if (!key) return () => {}
      const subscribers = listeners.get(key) || new Set<() => void>()
      subscribers.add(listener)
      listeners.set(key, subscribers)
      return () => {
        subscribers.delete(listener)
        if (!subscribers.size) listeners.delete(key)
      }
    },
    offer(taskId: string, requestId: string, frame: string): void {
      if (!frame || (!taskId && !requestId) || (taskId && endedTasks.has(taskId))) return
      const at = now()
      if (taskId) put(taskId, frame, at)
      if (requestId) {
        if (requestId !== taskId) put(requestId, frame, at)
      }
      while (frames.size > MAX_FRAME_KEYS) remove(frames.keys().next().value || '')
    },
    finish(taskId: string): void {
      if (!taskId) return
      endedTasks.add(taskId)
      while (endedTasks.size > MAX_ENDED_TASKS) endedTasks.delete(endedTasks.values().next().value || '')
    },
  }
}

export const browserLiveFrameStore = createBrowserLiveFrameStore()

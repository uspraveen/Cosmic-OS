declare module 'ssh2' {
  export class Client {
    once(event: string, listener: (...args: any[]) => void): this
    off(event: string, listener: (...args: any[]) => void): this
    connect(config: {
      host: string
      port?: number
      username: string
      privateKey: string | Buffer
      readyTimeout?: number
      keepaliveInterval?: number
      keepaliveCountMax?: number
    }): this
    end(): void
    removeAllListeners(): this
    forwardOut(
      srcIP: string,
      srcPort: number,
      dstIP: string,
      dstPort: number,
      callback: (error?: Error, stream?: NodeJS.ReadWriteStream & { destroy: () => void }) => void,
    ): void
  }
}

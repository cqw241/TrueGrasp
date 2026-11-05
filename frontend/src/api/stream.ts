import { queryClient } from './client'
import type {
  ClockAdvanced,
  EvidenceAdded,
  HypothesisUpdated,
  MasteryChanged,
  MessageDone,
  TaskChanged,
  TokenDelta,
} from './schema'

/** GET /api/events 推送的事件，按 type 区分（backend/app/contracts/events.py）。 */
export type StreamEvent =
  | TokenDelta
  | MessageDone
  | HypothesisUpdated
  | EvidenceAdded
  | MasteryChanged
  | TaskChanged
  | ClockAdvanced

export type StreamEventType = StreamEvent['type']
export type StreamEventOf<T extends StreamEventType> = Extract<
  StreamEvent,
  { type: T }
>

/** connecting：首次连接或浏览器正在自动重连；closed：连接被拒绝，等待手动重连。 */
export type StreamStatus = 'connecting' | 'open' | 'closed'

type Listener = (event: StreamEvent) => void

const URL = '/api/events'
const RETRY_MS = 3000
const CLOSE_DELAY_MS = 1000

/**
 * 全站共用一条 SSE 连接：有订阅者时建立，最后一个订阅者离开时关闭。
 * 后端不回放历史事件，所以出错后重新连上时让所有查询重新拉取，补上期间的变化和失败的请求。
 */
class EventStream {
  private source: EventSource | null = null
  private retryTimer: number | undefined
  private closeTimer: number | undefined
  // 出过错（断线或连接失败）后再连上时，期间的事件已经丢失，需要重新拉取。
  private needsResync = false
  private listeners = new Set<Listener>()
  private statusListeners = new Set<() => void>()
  private refs = 0
  status: StreamStatus = 'connecting'

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener)
    const release = this.retain()
    return () => {
      this.listeners.delete(listener)
      release()
    }
  }

  // 箭头函数保持引用不变：useSyncExternalStore 的 subscribe 每次变化都会重新订阅。
  subscribeStatus = (listener: () => void): (() => void) => {
    this.statusListeners.add(listener)
    const release = this.retain()
    return () => {
      this.statusListeners.delete(listener)
      release()
    }
  }

  getStatus = (): StreamStatus => this.status

  private retain(): () => void {
    if (this.refs++ === 0) {
      window.clearTimeout(this.closeTimer)
      if (!this.source && this.retryTimer === undefined) this.connect()
    }
    let released = false
    return () => {
      if (released) return
      released = true
      // 切换页面时旧页面先退订、新页面后订阅，延迟关闭避免中间断开丢事件。
      if (--this.refs === 0) {
        this.closeTimer = window.setTimeout(
          () => this.disconnect(),
          CLOSE_DELAY_MS,
        )
      }
    }
  }

  private connect() {
    const source = new EventSource(URL)
    this.source = source
    this.setStatus('connecting')

    source.onopen = () => {
      if (this.needsResync) void queryClient.invalidateQueries()
      this.needsResync = false
      this.setStatus('open')
    }
    source.onmessage = (message: MessageEvent<string>) => {
      let event: StreamEvent
      try {
        event = JSON.parse(message.data) as StreamEvent
      } catch {
        console.warn('无法解析的 SSE 消息', message.data)
        return
      }
      for (const listener of this.listeners) listener(event)
    }
    source.onerror = () => {
      this.needsResync = true
      // 网络中断时浏览器会自动重连（CONNECTING）；后端返回错误状态时连接直接关闭（CLOSED），需要自己重连。
      if (source.readyState === EventSource.CLOSED) {
        this.source = null
        this.setStatus('closed')
        this.retryTimer = window.setTimeout(() => {
          this.retryTimer = undefined
          this.connect()
        }, RETRY_MS)
      } else {
        this.setStatus('connecting')
      }
    }
  }

  private disconnect() {
    window.clearTimeout(this.retryTimer)
    this.retryTimer = undefined
    this.source?.close()
    this.source = null
    this.needsResync = false
    this.setStatus('connecting')
  }

  private setStatus(status: StreamStatus) {
    if (this.status === status) return
    this.status = status
    for (const listener of this.statusListeners) listener()
  }
}

export const eventStream = new EventStream()

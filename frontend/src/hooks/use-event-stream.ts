import { useEffect, useEffectEvent, useSyncExternalStore } from 'react'
import {
  eventStream,
  type StreamEventOf,
  type StreamEventType,
  type StreamStatus,
} from '@/api/stream'

/**
 * 订阅一种 SSE 事件。handler 总能读到最新的 props 和 state，不需要放进依赖。
 *
 *   useStreamEvent('task_changed', (e) => {
 *     if (e.student_id === studentId) void refetch()
 *   })
 */
export function useStreamEvent<T extends StreamEventType>(
  type: T,
  handler: (event: StreamEventOf<T>) => void,
) {
  const onEvent = useEffectEvent(handler)
  useEffect(
    () =>
      eventStream.subscribe((event) => {
        if (event.type === type) onEvent(event as StreamEventOf<T>)
      }),
    [type],
  )
}

/** SSE 连接状态，用于界面上的连接提示。 */
export function useStreamStatus(): StreamStatus {
  return useSyncExternalStore(
    eventStream.subscribeStatus,
    eventStream.getStatus,
  )
}

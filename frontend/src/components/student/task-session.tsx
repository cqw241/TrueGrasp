import { Bot, Lightbulb, UserRound } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { api } from '@/api/client'
import type { ChatTurn } from '@/api/schema'
import { TaskAnswer } from '@/components/math/task-answer'
import { MathText, Tex } from '@/components/math/tex'
import { ErrorState, LoadingState } from '@/components/states'
import { Badge } from '@/components/ui/badge'
import { useStreamEvent, useStreamStatus } from '@/hooks/use-event-stream'
import { hintLevelLabel } from '@/lib/labels'
import { cn } from '@/lib/utils'

/** 一个任务的辅导会话：对话记录、正在流式生成的回复，以及下方的作答区。 */
export function TaskSession({ taskId }: { taskId: number }) {
  const params = { path: { task_id: taskId } }
  const messages = api.useQuery('get', '/api/tasks/{task_id}/messages', {
    params,
  })
  // 辅导 Agent 正在生成的回复，由 token 事件逐段拼接；message_done 后改由对话记录展示。
  const [streaming, setStreaming] = useState('')
  const end = useRef<HTMLDivElement>(null)

  // 连接断开期间的 token 和 message_done 会丢失，后端不回放；重连后对话由 REST 重新拉取，
  // 半截回复必须丢掉，否则下一轮 token 会接在旧内容后面。
  const status = useStreamStatus()
  if (status !== 'open' && streaming) setStreaming('')

  useStreamEvent('token', (event) => {
    if (event.task_id === taskId) setStreaming((text) => text + event.text)
  })
  useStreamEvent('message_done', (event) => {
    if (event.task_id !== taskId) return
    void messages.refetch().finally(() => setStreaming(''))
  })
  useStreamEvent('task_changed', (event) => {
    if (event.task_id === taskId) void messages.refetch()
  })

  const count = messages.data?.length ?? 0
  useEffect(() => {
    end.current?.scrollIntoView({ block: 'nearest' })
  }, [count, streaming])

  return (
    <div className="space-y-4">
      <div
        className="max-h-[60vh] space-y-3 overflow-y-auto pr-1"
        aria-live="polite"
        aria-label="辅导对话"
      >
        {messages.isPending ? (
          <LoadingState rows={2} />
        ) : messages.isError ? (
          <ErrorState
            error={messages.error}
            onRetry={() => void messages.refetch()}
          />
        ) : count === 0 && !streaming ? (
          <p className="text-muted-foreground text-sm">
            打开任务后，辅导老师会先给出题目或问题。
          </p>
        ) : (
          messages.data.map((turn, index) => <Turn key={index} turn={turn} />)
        )}
        {streaming && (
          <Turn
            turn={{ role: 'assistant', content: streaming, solution: false }}
            streaming
          />
        )}
        <div ref={end} />
      </div>
      <TaskAnswer taskId={taskId} />
    </div>
  )
}

// 学生作答提交的是 LaTeX，文字回复则是普通文本；含中文或 $ 时按混排文本显示。
const looksLikeText = /[㐀-鿿$]/

function Turn({ turn, streaming }: { turn: ChatTurn; streaming?: boolean }) {
  const mine = turn.role === 'user'
  const level = turn.hint_level as 1 | 2 | 3 | null | undefined
  return (
    <div className={cn('flex gap-2', mine && 'flex-row-reverse')}>
      <div
        className="bg-muted text-muted-foreground flex size-7 shrink-0 items-center justify-center rounded-full"
        aria-hidden
      >
        {mine ? <UserRound className="size-4" /> : <Bot className="size-4" />}
      </div>
      <div
        className={cn(
          'max-w-[85%] space-y-2 rounded-lg px-3 py-2 text-sm',
          mine ? 'bg-primary text-primary-foreground' : 'bg-muted',
          turn.solution && 'ring-1 ring-amber-500/50',
        )}
      >
        <span className="sr-only">{mine ? '我：' : '辅导老师：'}</span>
        {(level || turn.solution) && (
          <Badge variant="outline" className="bg-background">
            <Lightbulb data-icon="inline-start" />
            {turn.solution
              ? '完整解答'
              : `提示 ${level}/3 · ${hintLevelLabel[level!]}`}
          </Badge>
        )}
        {mine && !looksLikeText.test(turn.content) ? (
          <Tex latex={turn.content} />
        ) : (
          <MathText text={turn.content} />
        )}
        {streaming && (
          <span
            className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-current align-middle"
            aria-hidden
          />
        )}
        {turn.item && turn.item.stem.trim() !== turn.content.trim() && (
          <div className="bg-background text-foreground rounded-md border p-3">
            <p className="text-muted-foreground mb-1 text-xs">题目</p>
            <MathText text={turn.item.stem} />
          </div>
        )}
      </div>
    </div>
  )
}

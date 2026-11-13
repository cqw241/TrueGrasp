import { useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { api } from '@/api/client'
import type { HomeworkTaskProgress } from '@/api/schema'
import { EvidenceReference } from '@/components/decision/evidence-reference'
import { MathText } from '@/components/math/tex'
import { ErrorState, LoadingState } from '@/components/states'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useStreamEvent } from '@/hooks/use-event-stream'
import {
  dimensionLabel,
  judgedByLabel,
  masteryStateLabel,
  taskStatusLabel,
  taskTypeLabel,
} from '@/lib/labels'
import { MutationError } from './mutation-error'
import { useInvalidate } from './shared'

export function HomeworkFollowup({
  publicationId,
  index,
  latest,
}: {
  publicationId: string
  index: number
  latest: boolean
}) {
  const [open, setOpen] = useState(latest)
  return (
    <details
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
      className="rounded-lg border border-primary/25 bg-background"
    >
      <summary className="min-h-11 cursor-pointer px-4 py-3 text-sm font-medium">
        教学跟进 · 第 {index + 1} 次发布{latest ? '（最新）' : ''}
      </summary>
      {open && <WorkflowPanel publicationId={publicationId} />}
    </details>
  )
}

function WorkflowPanel({ publicationId }: { publicationId: string }) {
  const params = { path: { publication_id: publicationId } }
  const invalidate = useInvalidate()
  const publication = api.useQuery(
    'get',
    '/api/homework-publications/{publication_id}',
    { params },
    {
      refetchInterval: (q) =>
        q.state.data?.status === 'publishing' ? 2000 : false,
    },
  )
  const progress = api.useQuery(
    'get',
    '/api/homework-publications/{publication_id}/progress',
    { params },
    {
      enabled: publication.data?.status === 'published',
      refetchInterval: (q) => {
        const p = q.state.data
        return p &&
          (p.workflow.state !== 'planned' ||
            p.tasks.some(
              ({ task }) =>
                task.status === 'active' || task.status === 'pending',
            ))
          ? 4000
          : false
      },
    },
  )
  function refresh() {
    void publication.refetch()
    if (publication.data?.status === 'published') void progress.refetch()
  }
  useStreamEvent('task_changed', refresh)
  useStreamEvent('evidence_added', refresh)
  useStreamEvent('mastery_changed', refresh)
  const retry = api.useMutation(
    'post',
    '/api/homework-publications/{publication_id}/workflow-retries',
    {
      onSuccess: () => {
        refresh()
        invalidate('/api/tasks', '/api/class/overview', '/api/escalations')
      },
    },
  )
  const p = publication.data
  const workflow = progress.data?.workflow ?? p?.workflow
  return (
    <div className="space-y-4 border-t px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p role="status" className="text-sm font-medium">
          {!p
            ? '正在读取发布结果…'
            : p.status === 'published'
              ? `已导入 ${p.import_result?.evidence ?? 0} 份作答，关联 ${p.import_result?.students ?? 0} 名学生`
              : p.status === 'publishing'
                ? '正在正式导入…'
                : '本次发布失败'}
        </p>
        <Button
          variant="ghost"
          size="sm"
          onClick={refresh}
          disabled={publication.isFetching || progress.isFetching}
        >
          <RefreshCw className="size-3.5" aria-hidden />
          刷新进度
        </Button>
      </div>
      {publication.error && (
        <ErrorState error={publication.error} onRetry={refresh} />
      )}
      {p?.error && (
        <p role="alert" className="text-sm text-destructive">
          {p.error.message}
        </p>
      )}
      {p?.status === 'published' && workflow && (
        <>
          <ol
            aria-label="本次作业教学流程"
            className="grid gap-2 border-y py-3 text-sm sm:grid-cols-3"
          >
            <li>
              <strong className="block">作业已入账</strong>
              <span className="text-xs text-muted-foreground">
                仅含教师确认的记录
              </span>
            </li>
            <li>
              <strong className="block">
                {workflow.state === 'pending'
                  ? '等待教学规划'
                  : workflow.state === 'failed'
                    ? '教学处理需恢复'
                    : '教学规划已完成'}
              </strong>
              <span className="text-xs text-muted-foreground">
                关联 {workflow.task_ids.length} 个任务，含复用和后续任务
              </span>
            </li>
            <li>
              <strong className="block">以独立验证判断进展</strong>
              <span className="text-xs text-muted-foreground">
                完成辅导不等于已经掌握
              </span>
            </li>
          </ol>
          {workflow.error && (
            <div
              role="alert"
              className="space-y-2 border-l-2 border-destructive pl-3 text-sm"
            >
              <p>{workflow.error.message}</p>
              {workflow.error.retryable && (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={retry.isPending}
                  onClick={() => retry.mutate({ params })}
                >
                  {retry.isPending ? '正在恢复…' : '恢复教学流程'}
                </Button>
              )}
            </div>
          )}
          <MutationError error={retry.error} />
          {progress.isPending && <LoadingState rows={2} />}
          {progress.error && (
            <ErrorState
              error={progress.error}
              onRetry={() => void progress.refetch()}
            />
          )}
          {progress.data?.tasks.map((task) => (
            <TaskProgress key={task.task.id} value={task} />
          ))}
          {progress.data?.tasks.length === 0 &&
            workflow.state === 'planned' && (
              <p className="text-sm text-muted-foreground">
                本次规划没有安排教学任务；可结合原始作答和教学目标查看原因。
              </p>
            )}
        </>
      )}
      <p className="break-all text-xs text-muted-foreground">
        回执：{publicationId}
      </p>
    </div>
  )
}

function TaskProgress({ value }: { value: HomeworkTaskProgress }) {
  const { task } = value
  const evidenceIds = task.evidence_ids ?? []
  const assessed = value.verification.filter((v) => v.evidence_id !== null)
  return (
    <details className="border-b pb-3 last:border-b-0">
      <summary className="min-h-11 cursor-pointer text-sm">
        <span className="font-medium">
          {value.student_name ?? '全班'}
          {value.student_number ? `（${value.student_number}）` : ''}
        </span>
        <span className="ml-2">
          {value.knowledge_point_name} · {taskTypeLabel[task.type]}
        </span>
        <Badge variant="outline" className="ml-2">
          {value.failed
            ? '处理失败'
            : value.teacher_takeover
              ? '教师接管中'
              : !value.can_work
                ? '已退班，保留历史'
                : taskStatusLabel[task.status]}
        </Badge>
      </summary>
      <div className="space-y-3 pt-2 text-sm">
        <p className="break-words text-xs text-muted-foreground">
          任务 #{task.id}
          {task.student_id ? ` · 学生 ID：${task.student_id}` : ''}
          {task.due_date ? ` · 教学到期日：${task.due_date}` : ''}
        </p>
        <p>
          <MathText text={task.reason} />
        </p>
        <p>
          当前掌握状态：
          {value.mastery_state
            ? masteryStateLabel[value.mastery_state]
            : '尚无评价'}
          。本任务有 {assessed.length} 项独立验证记录。
        </p>
        {assessed.map((result) => (
          <div
            key={result.dimension}
            className="space-y-1 border-l-2 border-primary/30 pl-3"
          >
            <p>
              {dimensionLabel[result.dimension]}：
              {result.passed === null
                ? '暂不能判定'
                : result.passed
                  ? '通过'
                  : '未通过'}
              {result.judged_by ? `（${judgedByLabel[result.judged_by]}）` : ''}
            </p>
            {result.evidence_id !== null && (
              <EvidenceReference id={result.evidence_id} />
            )}
          </div>
        ))}
        {evidenceIds.length > 0 && (
          <div className="space-y-1">
            <p className="text-xs text-muted-foreground">安排任务的依据</p>
            {evidenceIds.map((id) => (
              <EvidenceReference key={id} id={id} />
            ))}
          </div>
        )}
      </div>
    </details>
  )
}

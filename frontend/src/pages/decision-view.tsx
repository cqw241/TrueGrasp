import { ChevronRight, Columns2 } from 'lucide-react'
import type { UseQueryResult } from '@tanstack/react-query'
import { Fragment, useMemo, useState } from 'react'
import { api } from '@/api/client'
import type { KnowledgePoint, Task } from '@/api/schema'
import { DiagnosisPanel } from '@/components/decision/diagnosis-panel'
import {
  TimelinePanel,
  VerificationPanel,
} from '@/components/decision/history-panels'
import { PageHeader } from '@/components/layout/app-layout'
import {
  EmptyState,
  ErrorState,
  LoadingState,
  QueryView,
} from '@/components/states'
import { TaskSession } from '@/components/student/task-session'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { useStreamEvent } from '@/hooks/use-event-stream'
import { lectureLabel, taskStatusLabel } from '@/lib/labels'

const PATHS = [
  { source: 'calc.ch1.composition', target: 'calc.ch9.multivar_chain' },
  { source: 'calc.ch4.substitution', target: 'calc.ch10.polar' },
]

function KnowledgePath({ source, target }: (typeof PATHS)[number]) {
  const path = api.useQuery('get', '/api/knowledge/path', {
    params: { query: { source, target } },
  })
  return (
    <QueryView
      query={path}
      empty={<EmptyState title="两个知识点之间没有先修路径" />}
    >
      {(points) => (
        <ol className="flex flex-wrap items-center gap-2">
          {points.map((point, index) => (
            <Fragment key={point.id}>
              {index > 0 && (
                <ChevronRight
                  className="text-muted-foreground size-4"
                  aria-hidden
                />
              )}
              <li>
                <Badge variant="outline" className="h-auto gap-2">
                  <span className="text-muted-foreground">
                    {lectureLabel(point.lecture)}
                  </span>
                  {point.name}
                </Badge>
              </li>
            </Fragment>
          ))}
        </ol>
      )}
    </QueryView>
  )
}

export default function DecisionView() {
  const students = api.useQuery('get', '/api/students')
  const knowledge = api.useQuery('get', '/api/knowledge/points')
  const points = useMemo(
    () => new Map((knowledge.data ?? []).map((point) => [point.id, point])),
    [knowledge.data],
  )
  const [studentId, setStudentId] = useState('')

  return (
    <>
      <PageHeader
        title="决策证据视图"
        description="查看错因假设、学习时间线与独立验证依据"
      />
      <div className="space-y-4">
        <QueryView query={students} empty={<EmptyState title="暂时没有学生" />}>
          {(data) => {
            const selected =
              data.find((student) => student.id === studentId) ?? data[0]
            return (
              <>
                <label className="flex flex-wrap items-center gap-3 text-sm font-medium">
                  学生
                  <select
                    className="bg-background rounded-md border px-3 py-2"
                    value={selected.id}
                    onChange={(event) => setStudentId(event.target.value)}
                  >
                    {data.map((student) => (
                      <option key={student.id} value={student.id}>
                        {student.name}（{student.id}）
                      </option>
                    ))}
                  </select>
                </label>
                <QueryView
                  query={knowledge}
                  empty={<EmptyState title="暂无知识点" />}
                >
                  {() => (
                    <StudentDecision
                      key={selected.id}
                      studentId={selected.id}
                      points={points}
                    />
                  )}
                </QueryView>
              </>
            )
          }}
        </QueryView>
        <Card>
          <CardHeader>
            <CardTitle>跨章节知识路径</CardTitle>
            <CardDescription>精修的两条先修路径</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {PATHS.map((path) => (
              <KnowledgePath key={path.source} {...path} />
            ))}
          </CardContent>
        </Card>
      </div>
    </>
  )
}

function initialTask(tasks: Task[]) {
  return (
    tasks.find((task) => task.status === 'active') ??
    tasks.find((task) => task.status === 'pending') ??
    tasks[0]
  )
}

function StudentDecision({
  studentId,
  points,
}: {
  studentId: string
  points: Map<string, KnowledgePoint>
}) {
  const tasks = api.useQuery('get', '/api/tasks', {
    params: { query: { student_id: studentId } },
  })
  useStreamEvent('task_changed', (event) => {
    if (event.student_id !== studentId) return
    if (tasks.data) {
      void tasks.refetch()
    } else {
      // A request already in flight can precede the event; fetch again after it settles.
      void tasks.refetch().then(() => tasks.refetch())
    }
  })
  if (tasks.isPending) return <LoadingState />
  if (!tasks.data)
    return (
      <ErrorState error={tasks.error} onRetry={() => void tasks.refetch()} />
    )
  return (
    <StudentDecisionContent
      studentId={studentId}
      points={points}
      tasks={tasks}
    />
  )
}

function StudentDecisionContent({
  studentId,
  points,
  tasks,
}: {
  studentId: string
  points: Map<string, KnowledgePoint>
  tasks: UseQueryResult<Task[], unknown>
}) {
  const tutoringTasks = (tasks.data ?? []).filter(
    (task) => task.type === 'tutoring' && task.id != null,
  )
  const [selectedTaskId, setSelectedTaskId] = useState<number | null>(
    () => initialTask(tutoringTasks)?.id ?? null,
  )
  const [previousTasks, setPreviousTasks] = useState(tasks.data)
  const [chosenPoint, setChosenPoint] = useState('')
  const [showSession, setShowSession] = useState(false)
  // Keep the selection stable across every data update, including SSE reconnection.
  if (tasks.data !== previousTasks) {
    setPreviousTasks(tasks.data)
    if (!tutoringTasks.some((task) => task.id === selectedTaskId)) {
      setSelectedTaskId(initialTask(tutoringTasks)?.id ?? null)
    }
  }
  const task =
    tutoringTasks.find((item) => item.id === selectedTaskId) ??
    initialTask(tutoringTasks)
  const pointId = points.has(chosenPoint)
    ? chosenPoint
    : (task?.knowledge_point_id ?? points.keys().next().value ?? '')

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="flex flex-wrap items-end gap-4">
          <label className="flex min-w-52 flex-col gap-2 text-sm font-medium">
            辅导任务
            {tasks.isError ? (
              <button
                className="text-destructive text-left underline"
                onClick={() => void tasks.refetch()}
              >
                任务加载失败，点击重试
              </button>
            ) : tasks.isPending ? (
              <span>加载中…</span>
            ) : (
              <select
                className="bg-background rounded-md border px-3 py-2"
                value={task?.id ?? ''}
                disabled={!task}
                onChange={(event) => {
                  setSelectedTaskId(Number(event.target.value))
                  setChosenPoint('')
                }}
              >
                {!task && <option value="">暂无辅导任务</option>}
                {tutoringTasks.map((item) => (
                  <option key={item.id} value={item.id ?? ''}>
                    {points.get(item.knowledge_point_id)?.name ??
                      item.knowledge_point_id}{' '}
                    · {taskStatusLabel[item.status]} · #{item.id}
                  </option>
                ))}
              </select>
            )}
          </label>
          <label className="flex min-w-52 flex-col gap-2 text-sm font-medium">
            验证知识点
            <select
              className="bg-background rounded-md border px-3 py-2"
              value={pointId}
              onChange={(event) => setChosenPoint(event.target.value)}
            >
              {[...points.values()].map((point) => (
                <option key={point.id} value={point.id}>
                  {lectureLabel(point.lecture)} · {point.name}
                </option>
              ))}
            </select>
          </label>
          <Button
            variant="outline"
            disabled={!task}
            aria-pressed={showSession}
            onClick={() => setShowSession(!showSession)}
          >
            <Columns2 data-icon="inline-start" />
            {showSession ? '收起辅导会话' : '并排显示辅导会话'}
          </Button>
        </CardContent>
      </Card>
      <div
        className={
          showSession && task
            ? 'grid items-start gap-4 lg:grid-cols-2'
            : 'space-y-4'
        }
      >
        <div className="space-y-4">
          {task?.id != null ? (
            <DiagnosisPanel
              key={task.id}
              taskId={task.id}
              studentId={studentId}
            />
          ) : (
            <Card>
              <CardHeader>
                <CardTitle>诊断与假设</CardTitle>
              </CardHeader>
              <CardContent>
                <EmptyState title="暂无辅导任务" />
              </CardContent>
            </Card>
          )}
          <TimelinePanel studentId={studentId} points={points} />
          {pointId && (
            <VerificationPanel
              key={pointId}
              studentId={studentId}
              pointId={pointId}
            />
          )}
        </div>
        {showSession && task?.id != null && (
          <Card>
            <CardHeader>
              <CardTitle>学生辅导会话</CardTitle>
              <CardDescription>当前选择的辅导任务</CardDescription>
            </CardHeader>
            <CardContent>
              <TaskSession key={task.id} taskId={task.id} />
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  )
}

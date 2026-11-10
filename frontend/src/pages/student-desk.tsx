import { CalendarClock, MessageCircleQuestion } from 'lucide-react'
import { useMemo, useState } from 'react'
import { api } from '@/api/client'
import type { KnowledgePoint, Task, TaskStatus, TaskType } from '@/api/schema'
import { PageHeader } from '@/components/layout/app-layout'
import { EmptyState, QueryView } from '@/components/states'
import { AskQuestion } from '@/components/student/ask-question'
import { StudentProgress } from '@/components/student/progress'
import { TaskSession } from '@/components/student/task-session'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useStreamEvent } from '@/hooks/use-event-stream'
import { lectureLabel, taskStatusLabel, taskTypeLabel } from '@/lib/labels'
import { cn } from '@/lib/utils'
import { useAuth } from '@/auth'

// 学生能在辅导台上作答的任务；转交教师、课上讲解是教师端的任务。
const studentTaskTypes = new Set<TaskType>([
  'tutoring',
  'verification',
  'retest',
  'precheck',
  'question',
])

export default function StudentDesk() {
  const { user } = useAuth()
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
        title="学生辅导台"
        description="完成老师安排的任务，遇到不会的题可以随时提问"
      />
      <QueryView query={students} empty={<EmptyState title="暂时没有学生" />}>
        {(data) => {
          const selected =
            data.find((student) => student.id === studentId) ?? data[0]
          return (
            <Tabs key={selected.id} defaultValue="tasks" className="gap-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                {user?.role === 'teacher' ? (
                  <label className="flex items-center gap-3 text-sm">
                    学生
                    <select
                      className="bg-background rounded-md border px-3 py-2"
                      value={selected.id}
                      onChange={(event) => setStudentId(event.target.value)}
                    >
                      {data.map((student) => (
                        <option key={student.id} value={student.id}>
                          {student.name}
                        </option>
                      ))}
                    </select>
                  </label>
                ) : (
                  <span className="text-sm">{selected.name}</span>
                )}
                <TabsList>
                  <TabsTrigger value="tasks">任务</TabsTrigger>
                  <TabsTrigger value="progress">我的进度</TabsTrigger>
                </TabsList>
              </div>
              <TabsContent value="tasks">
                <StudentTasks studentId={selected.id} points={points} />
              </TabsContent>
              <TabsContent value="progress">
                <StudentProgress studentId={selected.id} points={points} />
              </TabsContent>
            </Tabs>
          )
        }}
      </QueryView>
    </>
  )
}

// 进行中、待开始的任务排在前面，已结束的放在最后。
const statusOrder: Record<TaskStatus, number> = {
  active: 0,
  pending: 1,
  done: 2,
  cancelled: 3,
}

/** 默认打开的任务：先找进行中的，再找待开始的。 */
function defaultTask(tasks: Task[]) {
  return (
    tasks.find((task) => task.status === 'active') ??
    tasks.find((task) => task.status === 'pending')
  )
}

function StudentTasks({
  studentId,
  points,
}: {
  studentId: string
  points: Map<string, KnowledgePoint>
}) {
  const tasks = api.useQuery('get', '/api/tasks', {
    params: { query: { student_id: studentId } },
  })
  const [chosen, setChosen] = useState<number | null>(null)
  const [asking, setAsking] = useState(false)
  useStreamEvent('task_changed', (event) => {
    if (event.student_id === studentId) void tasks.refetch()
  })

  const list = (tasks.data ?? [])
    .filter((task) => task.id != null && studentTaskTypes.has(task.type))
    .sort((a, b) => statusOrder[a.status] - statusOrder[b.status])
  const task =
    list.find((item) => item.id === chosen) ??
    (chosen === null ? defaultTask(list) : undefined)

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between gap-2">
          <CardTitle>我的任务</CardTitle>
          <Button
            variant={asking ? 'secondary' : 'outline'}
            size="sm"
            aria-pressed={asking}
            onClick={() => setAsking(true)}
          >
            <MessageCircleQuestion data-icon="inline-start" />
            提问
          </Button>
        </CardHeader>
        <CardContent>
          <QueryView
            query={tasks}
            isEmpty={() => list.length === 0}
            empty={
              <EmptyState
                title="暂时没有任务"
                description="老师安排的辅导、验证、复测会出现在这里。"
              />
            }
          >
            {() => (
              <ul className="space-y-2">
                {list.map((item) => (
                  <li key={item.id}>
                    <TaskCard
                      task={item}
                      point={points.get(item.knowledge_point_id)}
                      selected={!asking && item.id === task?.id}
                      onOpen={() => {
                        setChosen(item.id ?? null)
                        setAsking(false)
                      }}
                    />
                  </li>
                ))}
              </ul>
            )}
          </QueryView>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>
            {asking
              ? '提问'
              : task
                ? `${taskTypeLabel[task.type]} · ${
                    points.get(task.knowledge_point_id)?.name ??
                    task.knowledge_point_id
                  }`
                : '辅导'}
          </CardTitle>
        </CardHeader>
        <CardContent>
          {asking ? (
            <AskQuestion
              studentId={studentId}
              onCancel={() => setAsking(false)}
              onCreated={(created) => {
                setAsking(false)
                setChosen(created.id ?? null)
                void tasks.refetch()
              }}
            />
          ) : task?.id != null ? (
            <TaskSession key={task.id} taskId={task.id} />
          ) : (
            <EmptyState
              title="请选择任务"
              description="打开左侧的任务开始作答，也可以点“提问”问一道没做出来的题。"
            />
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function TaskCard({
  task,
  point,
  selected,
  onOpen,
}: {
  task: Task
  point: KnowledgePoint | undefined
  selected: boolean
  onOpen: () => void
}) {
  const finished = task.status === 'done' || task.status === 'cancelled'
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-current={selected ? 'true' : undefined}
      className={cn(
        'hover:bg-muted/60 focus-visible:ring-ring/50 w-full space-y-1.5 rounded-md border p-3 text-left text-sm transition-colors outline-none focus-visible:ring-3',
        selected && 'border-primary bg-muted/60',
        finished && 'opacity-70',
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={task.type === 'question' ? 'secondary' : 'default'}>
          {taskTypeLabel[task.type]}
        </Badge>
        <span className="font-medium">
          {point?.name ?? task.knowledge_point_id}
        </span>
        <Badge variant="outline" className="ml-auto">
          {taskStatusLabel[task.status]}
        </Badge>
      </div>
      <div className="text-muted-foreground flex flex-wrap gap-x-3 text-xs">
        {point && <span>{lectureLabel(point.lecture)}</span>}
        {task.due_date && (
          <span className="inline-flex items-center gap-1">
            <CalendarClock className="size-3" aria-hidden />
            {task.due_date} 前
          </span>
        )}
        {(task.round ?? 1) > 1 && <span>第 {task.round} 轮</span>}
      </div>
    </button>
  )
}

import { useState, type FormEvent } from 'react'
import { api } from '@/api/client'
import type { Task, TaskControl, TaskStatus, TaskType } from '@/api/schema'
import { EmptyState, QueryView } from '@/components/states'
import { MathText } from '@/components/math/tex'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import { taskStatusLabel, taskTypeLabel } from '@/lib/labels'
import { MutationError } from './mutation-error'
import { useInvalidate, usePointNames, useStudentNames } from './shared'

const ALL = 'all'
const OPEN: TaskStatus[] = ['pending', 'active']
// 转交和课上讲解另有面板，计划列表默认只看面向学生的教学任务。
const PLAN_TYPES: TaskType[] = [
  'question',
  'tutoring',
  'verification',
  'retest',
  'precheck',
]

export function PlanPanel({ classId }: { classId: string }) {
  const [type, setType] = useState<TaskType | typeof ALL>(ALL)
  const [status, setStatus] = useState<TaskStatus | 'open' | typeof ALL>('open')
  const tasks = api.useQuery('get', '/api/tasks', {
    params: {
      query: {
        class_id: classId,
        type: type === ALL ? undefined : type,
        status: status === ALL || status === 'open' ? undefined : status,
      },
    },
  })
  // 接管与覆盖记录由 T24 提供；接口未实现时任务列表照常显示，只是没有标记。
  const controls = api.useQuery(
    'get',
    '/api/tasks/controls',
    { params: { query: { class_id: classId } } },
    { retry: false },
  )
  const controlOf = new Map(controls.data?.map((c) => [c.task_id, c]))
  const [editing, setEditing] = useState<Task | null>(null)

  function visible(task: Task) {
    if (type === ALL && !PLAN_TYPES.includes(task.type)) return false
    if (status === 'open') return OPEN.includes(task.status)
    return true
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>干预计划</CardTitle>
        <CardDescription>
          学生主动提问，以及规划 Agent
          安排的辅导、验证、复测和预检；教师的修改优先于 Agent 计划
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap gap-2">
          <Select value={type} onValueChange={(v) => setType(v as TaskType)}>
            <SelectTrigger aria-label="任务类型" size="sm">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>全部教学任务</SelectItem>
              {PLAN_TYPES.map((t) => (
                <SelectItem key={t} value={t}>
                  {taskTypeLabel[t]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select
            value={status}
            onValueChange={(v) => setStatus(v as TaskStatus)}
          >
            <SelectTrigger aria-label="任务状态" size="sm">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="open">未完成</SelectItem>
              <SelectItem value={ALL}>全部状态</SelectItem>
              {(Object.keys(taskStatusLabel) as TaskStatus[]).map((s) => (
                <SelectItem key={s} value={s}>
                  {taskStatusLabel[s]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <QueryView
          query={tasks}
          isEmpty={(data) => !data.some(visible)}
          empty={
            <EmptyState
              title="没有符合条件的任务"
              description="导入作业后，规划 Agent 会在这里创建干预计划"
            />
          }
        >
          {(data) => (
            <TaskTable
              tasks={data.filter(visible)}
              controlOf={controlOf}
              onEdit={setEditing}
            />
          )}
        </QueryView>
      </CardContent>
      <Sheet
        open={editing != null}
        onOpenChange={(o) => !o && setEditing(null)}
      >
        <SheetContent className="overflow-y-auto">
          {editing && (
            <OverrideForm
              key={editing.id}
              task={editing}
              onDone={() => setEditing(null)}
            />
          )}
        </SheetContent>
      </Sheet>
    </Card>
  )
}

function TaskTable({
  tasks,
  controlOf,
  onEdit,
}: {
  tasks: Task[]
  controlOf: Map<number, TaskControl>
  onEdit: (task: Task) => void
}) {
  const studentName = useStudentNames()
  const pointName = usePointNames()
  return (
    <div className="max-h-[32rem] overflow-auto rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>学生</TableHead>
            <TableHead>任务</TableHead>
            <TableHead className="min-w-48">理由</TableHead>
            <TableHead>到期</TableHead>
            <TableHead>状态</TableHead>
            <TableHead className="text-right">操作</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {tasks.map((task) => {
            const control = task.id != null ? controlOf.get(task.id) : undefined
            return (
              <TableRow key={task.id}>
                <TableCell>{studentName(task.student_id)}</TableCell>
                <TableCell>
                  <div>
                    {taskTypeLabel[task.type]}
                    {task.round > 1 && ` · 第 ${task.round} 轮`}
                  </div>
                  <div className="text-muted-foreground text-xs">
                    {pointName(task.knowledge_point_id)}
                  </div>
                </TableCell>
                <TableCell className="text-muted-foreground max-w-80 text-xs whitespace-normal">
                  <MathText text={task.reason} />
                </TableCell>
                <TableCell className="whitespace-nowrap">
                  {task.due_date ?? '—'}
                </TableCell>
                <TableCell>
                  <div className="flex flex-wrap gap-1">
                    <Badge variant="outline">
                      {taskStatusLabel[task.status]}
                    </Badge>
                    {control?.taken_over && <Badge>教师接管</Badge>}
                    {control?.overridden && (
                      <Badge variant="secondary">已覆盖</Badge>
                    )}
                  </div>
                </TableCell>
                <TableCell className="text-right">
                  <TaskActions task={task} control={control} onEdit={onEdit} />
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}

function TaskActions({
  task,
  control,
  onEdit,
}: {
  task: Task
  control: TaskControl | undefined
  onEdit: (task: Task) => void
}) {
  const invalidate = useInvalidate()
  const takeover = api.useMutation('post', '/api/tasks/{task_id}/takeover')
  const handback = api.useMutation('post', '/api/tasks/{task_id}/handback')
  const open = OPEN.includes(task.status)
  if (task.id == null || !open) return null
  const params = { path: { task_id: task.id } }
  const onSuccess = () => invalidate('/api/tasks', '/api/tasks/controls')
  const pending = takeover.isPending || handback.isPending

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex justify-end gap-1">
        <Button size="sm" variant="outline" onClick={() => onEdit(task)}>
          修改
        </Button>
        {(task.type === 'tutoring' || task.type === 'question') &&
          (control?.taken_over ? (
            <Button
              size="sm"
              variant="outline"
              disabled={pending}
              onClick={() => handback.mutate({ params }, { onSuccess })}
            >
              交还 Agent
            </Button>
          ) : (
            <Button
              size="sm"
              variant="outline"
              disabled={pending}
              onClick={() =>
                takeover.mutate({ params, body: { note: '' } }, { onSuccess })
              }
            >
              接管
            </Button>
          ))}
      </div>
      <MutationError error={takeover.error ?? handback.error} />
    </div>
  )
}

function OverrideForm({ task, onDone }: { task: Task; onDone: () => void }) {
  const studentName = useStudentNames()
  const pointName = usePointNames()
  const invalidate = useInvalidate()
  const override = api.useMutation('patch', '/api/tasks/{task_id}')
  const [dueDate, setDueDate] = useState(task.due_date ?? '')
  const [cancel, setCancel] = useState(false)
  const [note, setNote] = useState('')

  function submit(event: FormEvent) {
    event.preventDefault()
    if (task.id == null) return
    override.mutate(
      {
        params: { path: { task_id: task.id } },
        body: { due_date: dueDate || null, cancel, note },
      },
      {
        onSuccess: () => {
          invalidate('/api/tasks', '/api/tasks/controls', '/api/class/issues')
          onDone()
        },
      },
    )
  }

  return (
    <form onSubmit={submit} className="flex h-full flex-col">
      <SheetHeader>
        <SheetTitle>修改计划</SheetTitle>
        <SheetDescription>
          {studentName(task.student_id)} · {taskTypeLabel[task.type]} ·{' '}
          {pointName(task.knowledge_point_id)}
        </SheetDescription>
      </SheetHeader>
      <div className="flex-1 space-y-4 px-4">
        {task.reason && (
          <p className="text-muted-foreground rounded-md border p-3 text-sm">
            <MathText text={task.reason} />
          </p>
        )}
        <div className="space-y-2">
          <Label htmlFor="override-due">到期日</Label>
          <Input
            id="override-due"
            type="date"
            value={dueDate}
            disabled={cancel}
            onChange={(e) => setDueDate(e.target.value)}
          />
        </div>
        <Label className="font-normal">
          <Checkbox
            checked={cancel}
            onCheckedChange={(c) => setCancel(c === true)}
          />
          取消这项任务
        </Label>
        <div className="space-y-2">
          <Label htmlFor="override-note">修改理由</Label>
          <Textarea
            id="override-note"
            required
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="记入教师处理记录"
          />
        </div>
        <MutationError error={override.error} />
      </div>
      <SheetFooter>
        <Button type="submit" disabled={override.isPending}>
          {override.isPending ? '保存中…' : '保存修改'}
        </Button>
      </SheetFooter>
    </form>
  )
}

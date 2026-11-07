import { useState } from 'react'
import { api } from '@/api/client'
import type { Task } from '@/api/schema'
import { TaskAnswer } from '@/components/math/task-answer'
import { PageHeader } from '@/components/layout/app-layout'
import { EmptyState, QueryView } from '@/components/states'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useStreamEvent } from '@/hooks/use-event-stream'
import { taskStatusLabel, taskTypeLabel } from '@/lib/labels'

export default function StudentDesk() {
  const students = api.useQuery('get', '/api/students')
  const [studentId, setStudentId] = useState('')

  return (
    <>
      <PageHeader
        title="学生辅导台"
        description="打开任务，输入公式或拍照作答"
      />
      <QueryView query={students} empty={<EmptyState title="暂时没有学生" />}>
        {(data) => {
          const selected =
            data.find((student) => student.id === studentId) ?? data[0]
          return (
            <>
              <label className="mb-4 flex items-center gap-3 text-sm">
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
              <StudentTasks key={selected.id} studentId={selected.id} />
            </>
          )
        }}
      </QueryView>
    </>
  )
}

function StudentTasks({ studentId }: { studentId: string }) {
  const tasks = api.useQuery('get', '/api/tasks', {
    params: { query: { student_id: studentId } },
  })
  const [taskId, setTaskId] = useState<number | null>(null)
  useStreamEvent('task_changed', (event) => {
    if (event.student_id === studentId) void tasks.refetch()
  })
  const task = tasks.data?.find((item) => item.id === taskId)

  function supportsAnswers(item: Task) {
    return ['tutoring', 'verification', 'retest', 'precheck'].includes(
      item.type,
    )
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
      <Card>
        <CardHeader>
          <CardTitle>我的任务</CardTitle>
        </CardHeader>
        <CardContent>
          <QueryView query={tasks} empty={<EmptyState title="暂时没有任务" />}>
            {(data) => (
              <ul className="space-y-2">
                {data.map((item) => (
                  <li
                    key={item.id}
                    className="flex items-center justify-between gap-2 rounded-md border p-3 text-sm"
                  >
                    <span>
                      {taskTypeLabel[item.type]} · #{item.id}
                    </span>
                    <Badge variant="outline">
                      {taskStatusLabel[item.status]}
                    </Badge>
                    {item.id != null && supportsAnswers(item) && (
                      <Button
                        variant={taskId === item.id ? 'secondary' : 'outline'}
                        size="sm"
                        onClick={() => setTaskId(item.id ?? null)}
                        aria-pressed={taskId === item.id}
                      >
                        {taskId === item.id ? '已打开' : '打开'}
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </QueryView>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>作答</CardTitle>
        </CardHeader>
        <CardContent>
          {task?.id != null ? (
            <TaskAnswer key={task.id} taskId={task.id} />
          ) : (
            <EmptyState
              title="请选择任务"
              description="打开任务后可输入公式，也可以上传手写答案。"
            />
          )}
        </CardContent>
      </Card>
    </div>
  )
}

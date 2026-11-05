import { useState } from 'react'
import { api } from '@/api/client'
import { PageHeader } from '@/components/layout/app-layout'
import { MathInput } from '@/components/math/math-input'
import { MathText, Tex } from '@/components/math/tex'
import { EmptyState, QueryView } from '@/components/states'
import { Badge } from '@/components/ui/badge'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { taskStatusLabel, taskTypeLabel } from '@/lib/labels'

// T10 只搭页面框架；任务卡、流式对话、拍照作答、主动提问由 T11 实现，当前学生由 T26 的登录确定。
export default function StudentDesk() {
  const tasks = api.useQuery('get', '/api/tasks')
  const [latex, setLatex] = useState('')

  return (
    <>
      <PageHeader title="学生辅导台" description="辅导任务、对话与作答" />
      <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <Card>
          <CardHeader>
            <CardTitle>我的任务</CardTitle>
          </CardHeader>
          <CardContent>
            <QueryView
              query={tasks}
              empty={<EmptyState title="暂时没有任务" />}
            >
              {(data) => (
                <ul className="space-y-2">
                  {data.map((task) => (
                    <li
                      key={task.id}
                      className="flex items-center justify-between gap-2 rounded-md border p-3 text-sm"
                    >
                      <span>{taskTypeLabel[task.type]}</span>
                      <Badge variant="outline">
                        {taskStatusLabel[task.status]}
                      </Badge>
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
            <CardDescription>
              <MathText text="求 $\sin^2(3x)$ 的导数" />
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <MathInput
              value={latex}
              onChange={setLatex}
              placeholder="在这里输入公式"
            />
            <div className="bg-muted/50 min-h-12 rounded-md p-3">
              {latex ? (
                <Tex latex={latex} display />
              ) : (
                <p className="text-muted-foreground text-sm">
                  输入后在这里预览
                </p>
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </>
  )
}

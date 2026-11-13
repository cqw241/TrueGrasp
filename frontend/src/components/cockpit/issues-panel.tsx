import { api } from '@/api/client'
import { EmptyState, QueryView } from '@/components/states'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { MathText } from '@/components/math/tex'
import { usePointNames, useStudentNames } from './shared'

export function IssuesPanel({ classId }: { classId: string }) {
  const issues = api.useQuery('get', '/api/class/issues', {
    params: { query: { class_id: classId } },
  })
  const studentName = useStudentNames()
  const pointName = usePointNames()
  return (
    <Card>
      <CardHeader>
        <CardTitle>共性问题</CardTitle>
        <CardDescription>
          多名学生出现同一错因时，建议课上集中讲解
        </CardDescription>
      </CardHeader>
      <CardContent>
        <QueryView
          query={issues}
          empty={
            <EmptyState
              title="暂无共性问题"
              description="导入作业后由规划 Agent 汇总"
            />
          }
        >
          {(data) => (
            <ul className="space-y-3">
              {data.map((issue) => (
                <li
                  key={issue.task_id}
                  className="space-y-1.5 rounded-md border p-3"
                >
                  <p className="text-muted-foreground text-xs">
                    {pointName(issue.knowledge_point_id)}
                  </p>
                  <p className="font-medium">
                    <MathText text={issue.description} />
                    <span className="text-muted-foreground ml-2 text-sm font-normal">
                      {issue.student_ids.length} 人
                    </span>
                  </p>
                  <p className="text-sm">
                    <MathText text={issue.suggestion} />
                  </p>
                  <p className="text-muted-foreground text-xs">
                    {issue.student_ids.map(studentName).join('、')}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </QueryView>
      </CardContent>
    </Card>
  )
}

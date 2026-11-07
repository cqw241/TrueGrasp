import { api } from '@/api/client'
import type { MasteryState } from '@/api/schema'
import { EmptyState, QueryView } from '@/components/states'
import { Badge } from '@/components/ui/badge'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { masteryStateLabel } from '@/lib/labels'

export function OverviewPanel() {
  const overview = api.useQuery('get', '/api/class/overview')
  return (
    <Card>
      <CardHeader>
        <CardTitle>班级达标率</CardTitle>
        <CardDescription>达到当前教学目标的学生比例</CardDescription>
      </CardHeader>
      <CardContent>
        <QueryView
          query={overview}
          isEmpty={(data) => data.students === 0}
          empty={
            <EmptyState
              title="还没有学生数据"
              description="导入作业后显示班级达标情况"
            />
          }
        >
          {(data) => (
            <div className="space-y-3">
              <p className="text-3xl font-semibold">
                {Math.round(data.goal_ratio * 100)}%
                <span className="text-muted-foreground ml-2 text-sm font-normal">
                  {data.goal_met}/{data.students} 人达标
                </span>
              </p>
              <p className="text-sm">
                转交教师 <span className="font-semibold">{data.escalated}</span>{' '}
                人
              </p>
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(data.by_state).map(([state, n]) => (
                  <Badge key={state} variant="secondary">
                    {masteryStateLabel[state as MasteryState]} · {n}
                  </Badge>
                ))}
              </div>
            </div>
          )}
        </QueryView>
      </CardContent>
    </Card>
  )
}

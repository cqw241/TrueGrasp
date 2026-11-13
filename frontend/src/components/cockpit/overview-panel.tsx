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

export function OverviewPanel({ classId }: { classId: string }) {
  const overview = api.useQuery('get', '/api/class/overview', {
    params: { query: { class_id: classId } },
  })
  const goals = api.useQuery('get', '/api/goals', {
    params: { query: { class_id: classId } },
  })
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
              {goals.data && goals.data.length > 0 ? (
                <>
                  <p className="text-3xl font-semibold tabular-nums">
                    {Math.round(data.goal_ratio * 100)}%
                    <span className="text-muted-foreground ml-2 text-sm font-normal">
                      {data.goal_met}/{data.students} 人达标
                    </span>
                  </p>
                  <div
                    className="h-1.5 overflow-hidden rounded bg-muted"
                    role="progressbar"
                    aria-label="班级目标达标比例"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={Math.round(data.goal_ratio * 100)}
                  >
                    <div
                      className="h-full bg-primary"
                      style={{ width: `${data.goal_ratio * 100}%` }}
                    />
                  </div>
                </>
              ) : (
                <p className="text-sm text-muted-foreground">
                  {goals.isPending
                    ? '正在读取教学目标…'
                    : goals.isError
                      ? '教学目标读取失败，暂不展示达标比例'
                      : '尚未设定教学目标，暂不计算达标率'}
                </p>
              )}
              <p className="text-xs text-muted-foreground">
                班级共 {data.students} 人；有掌握记录{' '}
                {Object.values(data.by_state).reduce(
                  (sum, count) => sum + count,
                  0,
                )}{' '}
                人
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

import { api } from '@/api/client'
import { PageHeader } from '@/components/layout/app-layout'
import { EmptyState, QueryView } from '@/components/states'
import { Badge } from '@/components/ui/badge'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { lectureLabel } from '@/lib/labels'

// T10 只搭页面框架并接通已有接口；目标、达标率、热力图、共性问题、计划与转交列表由 T12 实现。
export default function TeacherCockpit() {
  const clock = api.useQuery('get', '/api/clock')
  const overview = api.useQuery('get', '/api/class/overview')
  const points = api.useQuery('get', '/api/knowledge/points')

  return (
    <>
      <PageHeader
        title="教师驾驶舱"
        description="教学目标、班级达标情况、干预计划与转交列表"
      />
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle>教学进度</CardTitle>
            <CardDescription>当前教学日期与所在章节</CardDescription>
          </CardHeader>
          <CardContent>
            <QueryView query={clock}>
              {(data) => (
                <p className="text-lg">
                  {data.teaching_date} · {lectureLabel(data.lecture)}
                </p>
              )}
            </QueryView>
          </CardContent>
        </Card>

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
                <p className="text-3xl font-semibold">
                  {Math.round(data.goal_ratio * 100)}%
                  <span className="text-muted-foreground ml-2 text-sm font-normal">
                    {data.goal_met}/{data.students} 人
                  </span>
                </p>
              )}
            </QueryView>
          </CardContent>
        </Card>

        <Card className="md:col-span-2 xl:col-span-1">
          <CardHeader>
            <CardTitle>知识图谱</CardTitle>
            <CardDescription>各讲已入库的知识点数</CardDescription>
          </CardHeader>
          <CardContent>
            <QueryView
              query={points}
              empty={
                <EmptyState
                  title="知识图谱为空"
                  description="在 backend 目录运行 uv run python -m app.knowledge ingest 后重启后端"
                />
              }
            >
              {(data) => {
                const counts = new Map<number, number>()
                for (const p of data) {
                  counts.set(p.lecture, (counts.get(p.lecture) ?? 0) + 1)
                }
                return (
                  <div className="flex flex-wrap gap-2">
                    {[...counts].map(([lecture, n]) => (
                      <Badge key={lecture} variant="secondary">
                        {lectureLabel(lecture)} · {n}
                      </Badge>
                    ))}
                  </div>
                )
              }}
            </QueryView>
          </CardContent>
        </Card>
      </div>
    </>
  )
}

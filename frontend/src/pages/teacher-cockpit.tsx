import { api } from '@/api/client'
import { EscalationsPanel } from '@/components/cockpit/escalations-panel'
import { GoalPanel } from '@/components/cockpit/goal-panel'
import { HeatmapPanel } from '@/components/cockpit/heatmap-panel'
import { IssuesPanel } from '@/components/cockpit/issues-panel'
import { OverviewPanel } from '@/components/cockpit/overview-panel'
import { PendingMisconceptionsPanel } from '@/components/cockpit/pending-misconceptions-panel'
import { PlanPanel } from '@/components/cockpit/plan-panel'
import { useInvalidate } from '@/components/cockpit/shared'
import { PageHeader } from '@/components/layout/app-layout'
import { QueryView } from '@/components/states'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { useStreamEvent } from '@/hooks/use-event-stream'
import { lectureLabel } from '@/lib/labels'

export default function TeacherCockpit() {
  const invalidate = useInvalidate()
  // 任务和证据变化后刷新驾驶舱；SSE 断线重连时 api/stream.ts 会让所有查询重新拉取。
  useStreamEvent('task_changed', () =>
    invalidate(
      '/api/tasks',
      '/api/tasks/controls',
      '/api/class/issues',
      '/api/escalations',
      '/api/class/overview',
    ),
  )
  useStreamEvent('evidence_added', () =>
    invalidate(
      '/api/class/overview',
      '/api/class/heatmap',
      '/api/escalations',
      '/api/misconceptions/pending',
    ),
  )

  return (
    <>
      <PageHeader
        title="教师驾驶舱"
        description="教学目标、班级达标情况、干预计划与转交列表"
      />
      <div className="grid gap-4">
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          <ClockPanel />
          <OverviewPanel />
          <div className="md:col-span-2 xl:col-span-1">
            <GoalPanel />
          </div>
        </div>
        <HeatmapPanel />
        <div className="grid gap-4 lg:grid-cols-2">
          <IssuesPanel />
          <EscalationsPanel />
        </div>
        <PlanPanel />
        <PendingMisconceptionsPanel />
      </div>
    </>
  )
}

function ClockPanel() {
  const clock = api.useQuery('get', '/api/clock')
  return (
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
  )
}

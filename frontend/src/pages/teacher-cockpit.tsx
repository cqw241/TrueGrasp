import { api } from '@/api/client'
import { EscalationsPanel } from '@/components/cockpit/escalations-panel'
import { GoalPanel } from '@/components/cockpit/goal-panel'
import { HeatmapPanel } from '@/components/cockpit/heatmap-panel'
import { HomeworkPanel } from '@/components/cockpit/homework-panel'
import { IssuesPanel } from '@/components/cockpit/issues-panel'
import { OverviewPanel } from '@/components/cockpit/overview-panel'
import { PendingMisconceptionsPanel } from '@/components/cockpit/pending-misconceptions-panel'
import { PlanPanel } from '@/components/cockpit/plan-panel'
import { useInvalidate } from '@/components/cockpit/shared'
import { PageHeader } from '@/components/layout/app-layout'
import { Workspace, WorkspaceModule } from '@/components/layout/workspace'
import { QueryView } from '@/components/states'
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
    <div className="teacher-page">
      <PageHeader
        title="教师驾驶舱"
        description="从发现问题到验证掌握，让每一次教学干预都有依据"
        actions={<ClockPanel />}
      />
      <Workspace>
        <div className="workspace-column workspace-left">
          <WorkspaceModule id="homework" title="作业上传与解析">
            <HomeworkPanel />
          </WorkspaceModule>
          <WorkspaceModule id="goals" title="教学目标">
            <GoalPanel />
          </WorkspaceModule>
          <WorkspaceModule id="issues" title="共性问题">
            <IssuesPanel />
          </WorkspaceModule>
          <WorkspaceModule id="misconceptions" title="待审核错因">
            <PendingMisconceptionsPanel />
          </WorkspaceModule>
        </div>
        <div className="workspace-column workspace-center">
          <WorkspaceModule
            id="heatmap"
            title="全书知识热力图"
            className="heatmap-module"
          >
            <HeatmapPanel />
          </WorkspaceModule>
          <WorkspaceModule id="plan" title="干预计划">
            <PlanPanel />
          </WorkspaceModule>
        </div>
        <div className="workspace-column workspace-right">
          <WorkspaceModule id="overview" title="班级达标情况">
            <OverviewPanel />
          </WorkspaceModule>
          <WorkspaceModule id="escalations" title="教师接管与转交">
            <EscalationsPanel />
          </WorkspaceModule>
          <div className="evidence-note">
            <strong>以证据判断掌握</strong>
            <p>诊断 → 针对性辅导 → 独立验证 → 延迟复测</p>
            <span>尚无评估记录的知识点保留为“未评估”。</span>
          </div>
        </div>
      </Workspace>
    </div>
  )
}

function ClockPanel() {
  const clock = api.useQuery('get', '/api/clock')
  return (
    <div className="teaching-clock">
      <span>教学进度</span>
      <QueryView query={clock}>
        {(data) => (
          <p>
            {data.teaching_date} · {lectureLabel(data.lecture)}
          </p>
        )}
      </QueryView>
    </div>
  )
}

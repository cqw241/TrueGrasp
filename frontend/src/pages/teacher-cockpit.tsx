import { Link, useSearchParams } from 'react-router'
import { Button } from '@/components/ui/button'
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
  const groups = api.useQuery('get', '/api/classes')
  const [search, setSearch] = useSearchParams()
  const classId =
    groups.data?.find((group) => group.class_id === search.get('class_id'))
      ?.class_id ?? groups.data?.[0]?.class_id

  return (
    <QueryView query={groups}>
      {(rows) =>
        classId ? (
          <div className="grid gap-4">
            <div className="flex flex-wrap items-center justify-end gap-2">
              <label htmlFor="cockpit-class" className="text-sm">
                当前班级
              </label>
              <select
                id="cockpit-class"
                value={classId}
                className="rounded-md border bg-background p-2 text-sm"
                onChange={(event) =>
                  setSearch({ class_id: event.target.value })
                }
              >
                {rows.map((row) => (
                  <option key={row.class_id} value={row.class_id}>
                    {row.name}
                  </option>
                ))}
              </select>
              <Button asChild variant="outline">
                <Link to={`/classes?class_id=${classId}`}>管理名单</Link>
              </Button>
            </div>
            <Cockpit key={classId} classId={classId} />
          </div>
        ) : (
          <div className="grid gap-4">
            <PageHeader
              title="教师驾驶舱"
              description="创建班级并添加学生后，可在这里查看本班教学进度。"
            />
            <Button asChild className="justify-self-start">
              <Link to="/classes">前往班级管理</Link>
            </Button>
          </div>
        )
      }
    </QueryView>
  )
}

function Cockpit({ classId }: { classId: string }) {
  const invalidate = useInvalidate()
  // 任务和证据变化后刷新驾驶舱；SSE 断线重连时 api/stream.ts 会让所有查询重新拉取。
  useStreamEvent('clock_advanced', () => invalidate('/api/clock'))
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
        actions={<ClockPanel classId={classId} />}
      />
      <Workspace>
        <div className="workspace-column workspace-left">
          <WorkspaceModule id="homework" title="作业上传与审核">
            <HomeworkPanel classId={classId} />
          </WorkspaceModule>
          <WorkspaceModule id="goals" title="教学目标">
            <GoalPanel classId={classId} />
          </WorkspaceModule>
          <WorkspaceModule id="issues" title="共性问题">
            <IssuesPanel classId={classId} />
          </WorkspaceModule>
          <WorkspaceModule id="misconceptions" title="待审核错因">
            <PendingMisconceptionsPanel classId={classId} />
          </WorkspaceModule>
        </div>
        <div className="workspace-column workspace-center">
          <WorkspaceModule
            id="heatmap"
            title="全书知识热力图"
            className="heatmap-module"
          >
            <HeatmapPanel classId={classId} />
          </WorkspaceModule>
          <WorkspaceModule id="plan" title="干预计划">
            <PlanPanel classId={classId} />
          </WorkspaceModule>
        </div>
        <div className="workspace-column workspace-right">
          <WorkspaceModule id="overview" title="班级达标情况">
            <OverviewPanel classId={classId} />
          </WorkspaceModule>
          <WorkspaceModule id="escalations" title="教师接管与转交">
            <EscalationsPanel classId={classId} />
          </WorkspaceModule>
          <div className="evidence-note">
            <strong>以证据判断掌握</strong>
            <p>诊断 → 针对性辅导 → 独立验证 → 延迟复测</p>
          </div>
        </div>
      </Workspace>
    </div>
  )
}

function ClockPanel({ classId }: { classId: string }) {
  const clock = api.useQuery('get', '/api/clock', {
    params: { query: { class_id: classId } },
  })
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

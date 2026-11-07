import { api } from '@/api/client'
import type { KnowledgePoint, Mastery, MasteryState } from '@/api/schema'
import { EmptyState, QueryView } from '@/components/states'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useStreamEvent } from '@/hooks/use-event-stream'
import {
  lectureLabel,
  masteryStateLabel,
  timelineKindLabel,
} from '@/lib/labels'
import { cn } from '@/lib/utils'

// 按掌握程度从高到低排列；“错因复现”“转交教师”放在最后提醒注意。
const stateOrder: MasteryState[] = [
  'integrated',
  'consolidated',
  'initial',
  'tutoring',
  'needs_intervention',
  'relapsed',
  'escalated',
]

const stateTone: Record<MasteryState, string> = {
  integrated: 'border-emerald-600/40 text-emerald-700 dark:text-emerald-400',
  consolidated: 'border-emerald-600/40 text-emerald-700 dark:text-emerald-400',
  initial: 'border-sky-600/40 text-sky-700 dark:text-sky-400',
  tutoring: 'border-amber-600/40 text-amber-700 dark:text-amber-400',
  needs_intervention: 'border-amber-600/40 text-amber-700 dark:text-amber-400',
  relapsed: 'border-destructive/50 text-destructive',
  escalated: 'border-destructive/50 text-destructive',
}

export function StateBadge({ state }: { state: MasteryState }) {
  return (
    <Badge variant="outline" className={cn(stateTone[state])}>
      {masteryStateLabel[state]}
    </Badge>
  )
}

/** 学生个人进度：各知识点的掌握状态（按讲分组）与最近的学习记录。 */
export function StudentProgress({
  studentId,
  points,
}: {
  studentId: string
  points: Map<string, KnowledgePoint>
}) {
  const path = { params: { path: { student_id: studentId } } }
  const mastery = api.useQuery(
    'get',
    '/api/students/{student_id}/mastery',
    path,
  )
  const timeline = api.useQuery(
    'get',
    '/api/students/{student_id}/timeline',
    path,
  )
  useStreamEvent('mastery_changed', (event) => {
    if (event.student_id !== studentId) return
    void mastery.refetch()
    void timeline.refetch()
  })

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
      <Card>
        <CardHeader>
          <CardTitle>知识点掌握</CardTitle>
        </CardHeader>
        <CardContent>
          <QueryView
            query={mastery}
            empty={
              <EmptyState
                title="还没有掌握记录"
                description="完成作业或任务后，这里会显示每个知识点的掌握状态。"
              />
            }
          >
            {(data) => <MasteryList rows={data} points={points} />}
          </QueryView>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>最近的学习记录</CardTitle>
        </CardHeader>
        <CardContent>
          <QueryView
            query={timeline}
            empty={<EmptyState title="还没有学习记录" />}
          >
            {(data) => (
              <ol className="space-y-3">
                {data
                  .slice(-12)
                  .reverse()
                  .map((entry, index) => (
                    <li key={index} className="border-l-2 pl-3 text-sm">
                      <div className="text-muted-foreground flex flex-wrap gap-x-2 text-xs">
                        <span>{entry.teaching_date}</span>
                        <span>{timelineKindLabel[entry.kind]}</span>
                        <span>
                          {points.get(entry.knowledge_point_id)?.name ??
                            entry.knowledge_point_id}
                        </span>
                      </div>
                      <p>{entry.title}</p>
                    </li>
                  ))}
              </ol>
            )}
          </QueryView>
        </CardContent>
      </Card>
    </div>
  )
}

function MasteryList({
  rows,
  points,
}: {
  rows: Mastery[]
  points: Map<string, KnowledgePoint>
}) {
  const counts = new Map<MasteryState, number>()
  for (const row of rows)
    counts.set(row.state, (counts.get(row.state) ?? 0) + 1)

  const lectures = new Map<number, Mastery[]>()
  for (const row of rows) {
    const lecture = points.get(row.knowledge_point_id)?.lecture ?? 0
    lectures.set(lecture, [...(lectures.get(lecture) ?? []), row])
  }
  const sortKey = (row: Mastery) => {
    const point = points.get(row.knowledge_point_id)
    return point ? point.section : 0
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        {stateOrder
          .filter((state) => counts.has(state))
          .map((state) => (
            <div
              key={state}
              className="flex items-center gap-2 rounded-md border px-3 py-1.5 text-sm"
            >
              {masteryStateLabel[state]}
              <span className="font-semibold tabular-nums">
                {counts.get(state)}
              </span>
            </div>
          ))}
      </div>
      {[...lectures.entries()]
        .sort(([a], [b]) => a - b)
        .map(([lecture, items]) => (
          <section key={lecture} className="space-y-2">
            <h3 className="text-muted-foreground text-sm font-medium">
              {lecture > 0 ? lectureLabel(lecture) : '其他'}
            </h3>
            <ul className="divide-y rounded-md border">
              {items
                .sort((a, b) => sortKey(a) - sortKey(b))
                .map((row) => (
                  <li
                    key={row.knowledge_point_id}
                    className="flex items-center justify-between gap-2 px-3 py-2 text-sm"
                  >
                    <span>
                      {points.get(row.knowledge_point_id)?.name ??
                        row.knowledge_point_id}
                    </span>
                    <StateBadge state={row.state} />
                  </li>
                ))}
            </ul>
          </section>
        ))}
    </div>
  )
}

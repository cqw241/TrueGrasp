import { api } from '@/api/client'
import type { KnowledgePoint } from '@/api/schema'
import { EvidenceReference } from '@/components/decision/evidence-reference'
import { MathText } from '@/components/math/tex'
import { EmptyState, QueryView } from '@/components/states'
import { Badge } from '@/components/ui/badge'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { useStreamEvent } from '@/hooks/use-event-stream'
import { dimensionLabel, judgedByLabel, timelineKindLabel } from '@/lib/labels'

export function TimelinePanel({
  studentId,
  points,
}: {
  studentId: string
  points: Map<string, KnowledgePoint>
}) {
  const timeline = api.useQuery('get', '/api/students/{student_id}/timeline', {
    params: { path: { student_id: studentId } },
  })
  useStreamEvent('evidence_added', (event) => {
    if (event.student_id === studentId) void timeline.refetch()
  })
  useStreamEvent('task_changed', (event) => {
    if (event.student_id === studentId) void timeline.refetch()
  })
  return (
    <Card>
      <CardHeader>
        <CardTitle>学习时间线</CardTitle>
        <CardDescription>诊断、干预、验证和后续跟进</CardDescription>
      </CardHeader>
      <CardContent>
        <QueryView query={timeline} empty={<EmptyState title="暂无学习记录" />}>
          {(entries) => (
            <ol className="space-y-4 border-l-2 pl-4">
              {entries.map((entry, index) => (
                <li
                  key={`${entry.kind}-${entry.task_id}-${entry.evidence_ids.join('-')}-${index}`}
                  className="space-y-1 text-sm"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-muted-foreground">
                      {entry.teaching_date}
                    </span>
                    <Badge variant="outline">
                      {timelineKindLabel[entry.kind]}
                    </Badge>
                    <span>
                      {points.get(entry.knowledge_point_id)?.name ??
                        entry.knowledge_point_id}
                    </span>
                  </div>
                  {entry.detail && (
                    <p>
                      <MathText text={entry.detail} />
                    </p>
                  )}
                  <div className="flex flex-wrap gap-2">
                    {entry.evidence_ids.map((id) => (
                      <EvidenceReference key={id} id={id} />
                    ))}
                  </div>
                </li>
              ))}
            </ol>
          )}
        </QueryView>
      </CardContent>
    </Card>
  )
}

export function VerificationPanel({
  studentId,
  pointId,
}: {
  studentId: string
  pointId: string
}) {
  const verification = api.useQuery(
    'get',
    '/api/students/{student_id}/verification',
    {
      params: {
        path: { student_id: studentId },
        query: { knowledge_point_id: pointId },
      },
    },
  )
  useStreamEvent('evidence_added', (event) => {
    if (event.student_id === studentId && event.knowledge_point_id === pointId)
      void verification.refetch()
  })
  return (
    <Card>
      <CardHeader>
        <CardTitle>六维验证结果</CardTitle>
        <CardDescription>所选知识点各维度的最新证据</CardDescription>
      </CardHeader>
      <CardContent>
        <QueryView query={verification}>
          {(results) => (
            <ul className="divide-y">
              {results.map((result) => (
                <li
                  key={result.dimension}
                  className="flex flex-wrap items-center justify-between gap-2 py-3 text-sm"
                >
                  <span className="font-medium">
                    {dimensionLabel[result.dimension]}
                  </span>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge
                      variant={
                        result.passed === false ? 'destructive' : 'outline'
                      }
                    >
                      {result.evidence_id === null
                        ? '未验证'
                        : result.passed === null
                          ? '无法判定'
                          : result.passed
                            ? '通过'
                            : '未通过'}
                    </Badge>
                    {result.judged_by && (
                      <span className="text-muted-foreground">
                        {judgedByLabel[result.judged_by]}
                      </span>
                    )}
                    {result.evidence_id !== null && (
                      <EvidenceReference id={result.evidence_id} />
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </QueryView>
      </CardContent>
    </Card>
  )
}

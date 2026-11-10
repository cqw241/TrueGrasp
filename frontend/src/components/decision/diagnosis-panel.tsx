import { ApiError, api } from '@/api/client'
import { Answer } from '@/components/decision/answer'
import { EvidenceReference } from '@/components/decision/evidence-reference'
import { MathText, Tex } from '@/components/math/tex'
import { EmptyState, ErrorState, LoadingState } from '@/components/states'
import { Badge } from '@/components/ui/badge'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { useStreamEvent } from '@/hooks/use-event-stream'
import { hypothesisStatusLabel, judgedByLabel } from '@/lib/labels'

export function DiagnosisPanel({
  taskId,
  studentId,
}: {
  taskId: number
  studentId: string
}) {
  const query = api.useQuery('get', '/api/tasks/{task_id}/diagnosis', {
    params: { path: { task_id: taskId } },
  })
  useStreamEvent('hypothesis_updated', (event) => {
    if (event.task_id === taskId && event.student_id === studentId)
      void query.refetch()
  })
  useStreamEvent('evidence_added', (event) => {
    if (event.student_id === studentId) void query.refetch()
  })
  useStreamEvent('message_done', (event) => {
    if (event.task_id === taskId) void query.refetch()
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>诊断与假设</CardTitle>
        <CardDescription>对照每种错因的预测与学生实际作答</CardDescription>
      </CardHeader>
      <CardContent>
        {query.isPending ? (
          <LoadingState />
        ) : query.isError ? (
          query.error instanceof ApiError && query.error.status === 404 ? (
            <EmptyState
              title="尚无诊断记录"
              description="开始辅导后，候选错因和追问会显示在这里。"
            />
          ) : (
            <ErrorState
              error={query.error}
              onRetry={() => void query.refetch()}
            />
          )
        ) : (
          <div className="space-y-5">
            <div className="space-y-2">
              <h3 className="font-medium">原始错误</h3>
              <p className="text-sm">
                学生作答：
                <Answer value={query.data.error_evidence.student_answer} />
              </p>
              {query.data.error_evidence.correct_answer && (
                <p className="text-sm">
                  参考答案：
                  <Tex latex={query.data.error_evidence.correct_answer} />
                </p>
              )}
              {query.data.error_evidence.id != null && (
                <EvidenceReference id={query.data.error_evidence.id} />
              )}
            </div>
            <div className="space-y-2">
              <h3 className="font-medium">候选错因</h3>
              <ul className="space-y-2">
                {query.data.hypotheses.map((hypothesis) => (
                  <li
                    key={hypothesis.hypothesis_id}
                    className="flex flex-wrap items-start justify-between gap-2 rounded-md border p-3 text-sm"
                  >
                    <span>
                      <MathText text={hypothesis.description} />
                      {hypothesis.unmodeled && '（待审核）'}
                    </span>
                    <Badge
                      variant={
                        hypothesis.status === 'refuted'
                          ? 'secondary'
                          : 'outline'
                      }
                    >
                      {hypothesisStatusLabel[hypothesis.status]}
                    </Badge>
                  </li>
                ))}
              </ul>
            </div>
            <div className="space-y-3">
              <h3 className="font-medium">判别追问</h3>
              {query.data.probes.length === 0 ? (
                <p className="text-muted-foreground text-sm">尚无追问题</p>
              ) : (
                query.data.probes.map((probe, index) => (
                  <section
                    key={probe.item_id}
                    className="space-y-2 rounded-md border p-3 text-sm"
                  >
                    <p className="font-medium">
                      追问 {index + 1}：<MathText text={probe.stem} />
                    </p>
                    <ul className="space-y-1">
                      {probe.predictions.map((prediction) => (
                        <li
                          key={prediction.misconception_id}
                          className="overflow-x-auto"
                        >
                          {query.data.hypotheses.find(
                            (h) =>
                              h.misconception_id ===
                              prediction.misconception_id,
                          )?.description ?? prediction.misconception_id}
                          ：<Tex latex={prediction.answer} />
                        </li>
                      ))}
                    </ul>
                    <p className="overflow-x-auto">
                      学生实际作答：
                      {probe.student_answer === null ? (
                        '待作答'
                      ) : (
                        <Answer value={probe.student_answer} />
                      )}
                    </p>
                    {probe.judged_by && (
                      <p className="text-muted-foreground">
                        {judgedByLabel[probe.judged_by]}
                      </p>
                    )}
                  </section>
                ))
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

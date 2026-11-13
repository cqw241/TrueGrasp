import { useState, type FormEvent } from 'react'
import { api } from '@/api/client'
import type { Escalation, Evidence } from '@/api/schema'
import { EmptyState, QueryView } from '@/components/states'
import { MathText, Tex } from '@/components/math/tex'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Textarea } from '@/components/ui/textarea'
import { dimensionLabel, evidenceKindLabel, judgedByLabel } from '@/lib/labels'
import { MutationError } from './mutation-error'
import { useInvalidate, usePointNames } from './shared'

export function EscalationsPanel({ classId }: { classId: string }) {
  const escalations = api.useQuery('get', '/api/escalations', {
    params: { query: { class_id: classId } },
  })
  return (
    <Card>
      <CardHeader>
        <CardTitle>转交列表</CardTitle>
        <CardDescription>
          Agent 多轮辅导仍未解决的学生，附证据链和处理建议
        </CardDescription>
      </CardHeader>
      <CardContent>
        <QueryView
          query={escalations}
          empty={<EmptyState title="没有待处理的转交" />}
        >
          {(data) => (
            <ul className="space-y-3">
              {data.map((e) => (
                <EscalationItem key={e.task.id} escalation={e} />
              ))}
            </ul>
          )}
        </QueryView>
      </CardContent>
    </Card>
  )
}

function EscalationItem({ escalation }: { escalation: Escalation }) {
  const { task } = escalation
  const pointName = usePointNames()
  const [expanded, setExpanded] = useState(false)
  const resolved = task.status === 'done'
  return (
    <li className="space-y-2 rounded-md border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-medium">
          {escalation.student_name}
          <span className="text-muted-foreground ml-2 text-xs font-normal">
            {pointName(task.knowledge_point_id)}
          </span>
        </p>
        {resolved && <Badge variant="secondary">已处理</Badge>}
      </div>
      <p className="text-sm">
        <MathText text={escalation.suggestion} />
      </p>
      <Button
        size="sm"
        variant="ghost"
        className="-ml-2"
        aria-expanded={expanded}
        onClick={() => setExpanded(!expanded)}
      >
        {expanded
          ? '收起证据链'
          : `查看证据链（${escalation.evidence.length} 条）`}
      </Button>
      {expanded && <EvidenceChain evidence={escalation.evidence} />}
      {!resolved && task.id != null && <ResolveForm taskId={task.id} />}
    </li>
  )
}

function EvidenceChain({ evidence }: { evidence: Evidence[] }) {
  return (
    <ol className="border-muted space-y-2 border-l-2 pl-3 text-sm">
      {evidence.map((ev) => (
        <li key={ev.id} className="space-y-0.5">
          <div className="flex flex-wrap items-center gap-1.5 text-xs">
            <span className="text-muted-foreground">{ev.teaching_date}</span>
            <Badge variant="outline">{evidenceKindLabel[ev.kind]}</Badge>
            {ev.dimension && (
              <Badge variant="outline">{dimensionLabel[ev.dimension]}</Badge>
            )}
            {ev.is_correct != null && (
              <Badge variant={ev.is_correct ? 'secondary' : 'destructive'}>
                {ev.is_correct ? '正确' : '错误'}
              </Badge>
            )}
            <span className="text-muted-foreground">
              {judgedByLabel[ev.judged_by]}
            </span>
          </div>
          {ev.student_answer && ev.kind !== 'teacher' && (
            <div className="overflow-x-auto">
              作答 <Tex latex={ev.student_answer} />
              {ev.correct_answer && (
                <span className="text-muted-foreground">
                  {' '}
                  · 正确答案 <Tex latex={ev.correct_answer} />
                </span>
              )}
            </div>
          )}
          {ev.reason && (
            <p className="text-muted-foreground text-xs">
              <MathText text={ev.reason} />
            </p>
          )}
        </li>
      ))}
    </ol>
  )
}

function ResolveForm({ taskId }: { taskId: number }) {
  const resolve = api.useMutation('post', '/api/escalations/{task_id}/resolve')
  const invalidate = useInvalidate()
  const [note, setNote] = useState('')

  function submit(event: FormEvent) {
    event.preventDefault()
    resolve.mutate(
      { params: { path: { task_id: taskId } }, body: { note } },
      {
        onSuccess: () =>
          invalidate('/api/escalations', '/api/tasks', '/api/class/overview'),
      },
    )
  }

  return (
    <form onSubmit={submit} className="space-y-2">
      <Textarea
        aria-label="处理说明"
        required
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="处理说明，作为一条单独的教师证据写入账本"
      />
      <MutationError error={resolve.error} />
      <Button type="submit" size="sm" disabled={resolve.isPending}>
        {resolve.isPending ? '提交中…' : '标记已处理'}
      </Button>
    </form>
  )
}

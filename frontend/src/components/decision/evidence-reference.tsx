import { useState } from 'react'
import { api } from '@/api/client'
import { Answer } from '@/components/decision/answer'
import { MathText, Tex } from '@/components/math/tex'
import { ErrorState, LoadingState } from '@/components/states'
import { Button } from '@/components/ui/button'
import { dimensionLabel, evidenceKindLabel, judgedByLabel } from '@/lib/labels'

export function EvidenceReference({ id }: { id: number }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="text-sm">
      <Button
        type="button"
        variant="outline"
        size="sm"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        {open ? '收起' : '查看'}证据 #{id}
      </Button>
      {open && <EvidenceDetails id={id} />}
    </div>
  )
}

function EvidenceDetails({ id }: { id: number }) {
  const query = api.useQuery('get', '/api/evidence/{evidence_id}', {
    params: { path: { evidence_id: id } },
  })
  if (query.isPending) return <LoadingState rows={2} />
  if (query.isError)
    return (
      <ErrorState error={query.error} onRetry={() => void query.refetch()} />
    )
  const evidence = query.data
  return (
    <div className="bg-muted/40 mt-2 space-y-1 rounded-md p-3 text-sm">
      <p>
        {evidence.teaching_date} · {evidenceKindLabel[evidence.kind]}
        {evidence.dimension && ` · ${dimensionLabel[evidence.dimension]}`}
        {' · '}
        {judgedByLabel[evidence.judged_by]}
      </p>
      <p>
        判定：
        {evidence.is_correct == null
          ? '无法判定'
          : evidence.is_correct
            ? '正确'
            : '错误'}
      </p>
      {evidence.student_answer && (
        <p className="overflow-x-auto">
          学生作答：
          <Answer value={evidence.student_answer} />
        </p>
      )}
      {evidence.correct_answer && (
        <p className="overflow-x-auto">
          参考答案：
          <Tex latex={evidence.correct_answer} />
        </p>
      )}
      {evidence.reason && (
        <p>
          <MathText text={evidence.reason} />
        </p>
      )}
    </div>
  )
}

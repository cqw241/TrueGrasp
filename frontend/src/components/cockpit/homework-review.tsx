import { useMutation } from '@tanstack/react-query'
import { useRef, useState, type FormEvent } from 'react'
import { api, fetchClient } from '@/api/client'
import type {
  HomeworkDraft,
  Question,
  Record as AnswerRecord,
} from '@/api/schema'
import { MathText } from '@/components/math/tex'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { MutationError } from './mutation-error'

const reviewLabels = {
  pending: '待核对',
  confirmed: '已核对',
  skipped: '已跳过',
  published: '已发布',
}
const fieldLabels: { [field: string]: string } = {
  stem: '题目',
  correct_answer: '参考答案',
  knowledge_point_id: '主知识点',
  student_id: '学生归属',
  student_answer: '学生原始作答',
  review_status: '核对状态',
  class_id: '所属班级',
  item_id: '已有题目关联',
  resolution: '疑点处理',
}
const selectClass =
  'min-h-11 w-full rounded-md border bg-background p-2 text-sm'

type Member = {
  student_id: string
  name: string
  student_number: string | null
}
type Point = { id: string; name: string }

export function HomeworkReview({
  draft,
  classId,
  busy,
  onChanged,
}: {
  draft: HomeworkDraft
  classId: string
  busy: boolean
  onChanged: () => void
}) {
  const [visible, setVisible] = useState(20)
  const [dirtyIds, setDirtyIds] = useState<Set<string>>(new Set())
  function dirty(recordId: string, value: boolean) {
    setDirtyIds((previous) => {
      const next = new Set(previous)
      if (value) next.add(recordId)
      else next.delete(recordId)
      return next
    })
  }
  const roster = api.useQuery('get', '/api/classes/{class_id}/students', {
    params: { path: { class_id: draft.class_id ?? classId } },
  })
  const points = api.useQuery('get', '/api/knowledge/points')
  const bind = api.useMutation('patch', '/api/homework-drafts/{draft_id}', {
    onSuccess: onChanged,
  })
  const selected = draft.records
    .filter((r) => r.review_status === 'confirmed')
    .map((r) => r.record_id)
  const publishKey = useRef<{ fingerprint: string; key: string } | null>(null)
  const [previewIssues, setPreviewIssues] = useState<string[]>([])
  const publish = useMutation({
    mutationFn: async () => {
      const body = { expected_revision: draft.revision, record_ids: selected }
      const fingerprint = JSON.stringify([
        body,
        publication.data?.status === 'failed' ? latestId : null,
      ])
      if (publishKey.current?.fingerprint !== fingerprint) {
        const preview = await fetchClient.POST(
          '/api/homework-drafts/{draft_id}/publish-preview',
          {
            params: { path: { draft_id: draft.draft_id } },
            body,
          },
        )
        if (!preview.data?.can_publish) {
          setPreviewIssues(
            preview.data?.blocking_issues.map((i) => i.message) ?? [
              '请刷新草稿后重新核对',
            ],
          )
          return
        }
        publishKey.current = { fingerprint, key: crypto.randomUUID() }
      }
      setPreviewIssues([])
      await fetchClient.POST('/api/homework-drafts/{draft_id}/publish', {
        params: {
          path: { draft_id: draft.draft_id },
          header: { 'Idempotency-Key': publishKey.current.key },
        },
        body,
      })
    },
    onSettled: onChanged,
  })
  const latestId = draft.publication_ids.at(-1)
  const publication = api.useQuery(
    'get',
    '/api/homework-publications/{publication_id}',
    {
      params: { path: { publication_id: latestId ?? '' } },
    },
    {
      enabled: Boolean(latestId),
      refetchInterval: (query) => {
        const p = query.state.data
        return p?.status === 'publishing' ||
          (p?.status === 'published' && p.workflow.state === 'pending')
          ? 2000
          : false
      },
    },
  )
  const working = busy || bind.isPending || publish.isPending
  const questions = new Map(draft.questions.map((q) => [q.question_id, q]))
  return (
    <div className="space-y-4">
      {!draft.class_id && (
        <div className="space-y-2 rounded-lg border p-3 text-sm">
          <p>请先将这份个人草稿绑定到当前班级。</p>
          <Button
            variant="outline"
            disabled={working}
            onClick={() =>
              bind.mutate({
                params: { path: { draft_id: draft.draft_id } },
                body: { expected_revision: draft.revision, class_id: classId },
              })
            }
          >
            绑定到当前班级
          </Button>
          <MutationError error={bind.error} />
        </div>
      )}
      {roster.error && <MutationError error={roster.error} />}
      {points.error && <MutationError error={points.error} />}
      {draft.records.slice(0, visible).map((record, index) => {
        const question = questions.get(record.question_id)
        return (
          question && (
            <RecordEditor
              key={record.record_id}
              draft={draft}
              question={question}
              record={record}
              members={roster.data?.filter((m) => m.status === 'active') ?? []}
              points={
                points.data?.filter((p) => p.id !== 'pending.unclassified') ??
                []
              }
              frozen={draft.records.some(
                (r) =>
                  r.question_id === question.question_id &&
                  r.review_status === 'published',
              )}
              busy={working || !draft.class_id}
              onChanged={onChanged}
              first={index === 0}
              onDirty={(value) => dirty(record.record_id, value)}
            />
          )
        )
      })}
      {draft.records.length > visible && (
        <Button variant="outline" onClick={() => setVisible(visible + 20)}>
          显示更多作答
        </Button>
      )}
      <div className="space-y-2 border-t pt-3">
        <p className="text-sm">本次将发布 {selected.length} 份已核对作答。</p>
        <Button
          className="min-h-11"
          disabled={
            working ||
            dirtyIds.size > 0 ||
            !draft.class_id ||
            selected.length === 0
          }
          onClick={() => publish.mutate()}
        >
          {publish.isPending || draft.active_publication_id
            ? '正在发布…'
            : '发布已核对记录'}
        </Button>
        {dirtyIds.size > 0 && (
          <p className="text-xs text-muted-foreground">
            请先保存或放弃未保存修改，再发布。
          </p>
        )}
        <MutationError error={publish.error} />
        {previewIssues.length > 0 && (
          <ul role="alert" className="list-disc pl-5 text-sm text-destructive">
            {[...new Set(previewIssues)].map((message) => (
              <li key={message}>{message}</li>
            ))}
          </ul>
        )}
      </div>
      {publication.data && (
        <div role="status" className="space-y-1 rounded-lg border p-3 text-sm">
          <p className="font-medium">
            {publication.data.status === 'published'
              ? '发布成功'
              : publication.data.status === 'publishing'
                ? '正在正式导入…'
                : '本次发布失败'}
          </p>
          {publication.data.import_result && (
            <p>
              已导入 {publication.data.import_result.evidence} 份作答，关联{' '}
              {publication.data.import_result.students} 名学生。
            </p>
          )}
          {publication.data.status === 'published' && (
            <p>
              {publication.data.workflow.state === 'planned'
                ? `Agent 规划已完成，本次关联 ${publication.data.workflow.task_ids.length} 个任务。`
                : publication.data.workflow.state === 'failed'
                  ? '教学规划失败，等待恢复。'
                  : '等待教学规划。'}
            </p>
          )}
          {publication.data.error && (
            <p className="text-destructive">{publication.data.error.message}</p>
          )}
          <p className="break-all text-xs text-muted-foreground">
            回执：{publication.data.publication_id}
          </p>
        </div>
      )}
      <MutationError error={publication.error} />
      {draft.edits.length > 0 && (
        <details className="rounded-lg border p-3 text-xs">
          <summary className="cursor-pointer">
            修改记录（{draft.edits.length} 条）
          </summary>
          <ul className="mt-2 space-y-2">
            {draft.edits.map((edit, i) => (
              <li key={i} className="break-words">
                v{edit.revision} · {fieldLabels[edit.field] ?? edit.field}：
                {JSON.stringify(edit.before)} → {JSON.stringify(edit.after)}
                {edit.note && `（${edit.note}）`}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

function RecordEditor({
  draft,
  question,
  record,
  members,
  points,
  frozen,
  busy,
  onChanged,
  first,
  onDirty,
}: {
  draft: HomeworkDraft
  question: Question
  record: AnswerRecord
  members: Member[]
  points: Point[]
  frozen: boolean
  busy: boolean
  onChanged: () => void
  first: boolean
  onDirty: (value: boolean) => void
}) {
  const original = draft.original_extraction?.records.find(
    (r) => r.record_id === record.record_id,
  )
  const issues = [...question.issues, ...record.issues].filter(
    (i) => !i.resolution,
  )
  const defaults = {
    revision: draft.revision,
    stem: question.stem ?? '',
    answer: question.correct_answer ?? '',
    kp: question.knowledge_point_id ?? '',
    student: record.identity.student_id ?? '',
    studentAnswer: record.student_answer ?? '',
    itemId: question.item_id,
    accepted: [] as string[],
    note: '',
  }
  const [edited, setEdited] = useState<typeof defaults | null>(null)
  const values = edited ?? defaults
  function change<K extends keyof typeof defaults>(
    field: K,
    value: (typeof defaults)[K],
  ) {
    setEdited({ ...values, [field]: value })
    onDirty(true)
  }
  const save = api.useMutation('patch', '/api/homework-drafts/{draft_id}', {
    onSuccess: () => {
      setEdited(null)
      onDirty(false)
      onChanged()
    },
  })
  function submit(status: 'pending' | 'confirmed' | 'skipped') {
    save.mutate({
      params: { path: { draft_id: draft.draft_id } },
      body: {
        expected_revision: values.revision,
        question_updates: frozen
          ? []
          : [
              {
                question_id: question.question_id,
                stem: values.stem,
                correct_answer: values.answer,
                knowledge_point_id: values.kp,
                item_id: values.itemId,
              },
            ],
        record_updates: [
          {
            record_id: record.record_id,
            student_id: values.student || null,
            student_answer: values.studentAnswer,
            review_status: status,
            note: values.note || null,
          },
        ],
        issue_resolutions: values.accepted.map((issue_id) => ({
          issue_id,
          action: 'accepted' as const,
        })),
      },
    })
  }
  const disabled =
    busy || save.isPending || record.review_status === 'published'
  const prefix = record.record_id
  function fieldId(field: string) {
    return `${prefix}-${field}`
  }
  function onSubmit(event: FormEvent) {
    event.preventDefault()
    submit('pending')
  }
  return (
    <details className="rounded-lg border p-3" open={first || undefined}>
      <summary className="min-h-8 cursor-pointer text-sm font-medium break-words">
        {members.find((m) => m.student_id === record.identity.student_id)
          ?.name ||
          record.identity.observed_name ||
          '学生待选择'}
        {' · '}
        {question.number ? `第 ${question.number} 题` : '作答'}
        <Badge variant="secondary" className="ml-2">
          {reviewLabels[record.review_status]}
        </Badge>
        {edited && (
          <span className="ml-2 text-xs text-muted-foreground">
            有未保存修改
          </span>
        )}
      </summary>
      <form
        onSubmit={onSubmit}
        className="mt-3 space-y-3"
        aria-busy={save.isPending}
      >
        <fieldset disabled={disabled} className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor={fieldId('student')}>本班学生</Label>
            <select
              id={fieldId('student')}
              className={selectClass}
              value={values.student}
              onChange={(e) => change('student', e.target.value)}
            >
              <option value="">请选择学生</option>
              {members.map((m) => (
                <option key={m.student_id} value={m.student_id}>
                  {m.name}
                  {m.student_number ? `（${m.student_number}）` : ''}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor={fieldId('stem')}>题目</Label>
            <Textarea
              id={fieldId('stem')}
              rows={2}
              value={values.stem}
              disabled={disabled || frozen}
              onChange={(e) => change('stem', e.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor={fieldId('answer')}>参考答案</Label>
            <Textarea
              id={fieldId('answer')}
              rows={2}
              value={values.answer}
              disabled={disabled || frozen}
              placeholder="材料未提供时，请教师补录"
              onChange={(e) => change('answer', e.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor={fieldId('kp')}>主知识点</Label>
            <select
              id={fieldId('kp')}
              className={selectClass}
              value={values.kp}
              disabled={disabled || frozen}
              onChange={(e) => change('kp', e.target.value)}
            >
              <option value="">请选择知识点</option>
              {points.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>
          {question.item_id && !frozen && (
            <label className="flex min-h-11 items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={values.itemId === null}
                onChange={(e) =>
                  change('itemId', e.target.checked ? null : question.item_id)
                }
              />
              作为新题发布
            </label>
          )}
          {frozen && (
            <p className="text-xs text-muted-foreground">
              此题已发布，需要修改时请新建作业。
            </p>
          )}
          <div className="space-y-1.5">
            <Label htmlFor={fieldId('student-answer')}>学生原始作答</Label>
            <Textarea
              id={fieldId('student-answer')}
              rows={3}
              value={values.studentAnswer}
              onChange={(e) => change('studentAnswer', e.target.value)}
            />
          </div>
          {issues.length > 0 && (
            <div className="space-y-2 text-xs">
              <p className="font-medium">待确认项</p>
              {issues.map((issue) => (
                <label
                  key={issue.issue_id}
                  className="flex min-h-11 items-center gap-2"
                >
                  <input
                    type="checkbox"
                    checked={values.accepted.includes(issue.issue_id)}
                    disabled={
                      disabled ||
                      (frozen &&
                        question.issues.some(
                          (i) => i.issue_id === issue.issue_id,
                        ))
                    }
                    onChange={(e) =>
                      change(
                        'accepted',
                        e.target.checked
                          ? [...values.accepted, issue.issue_id]
                          : values.accepted.filter(
                              (id) => id !== issue.issue_id,
                            ),
                      )
                    }
                  />
                  已核对：{issue.message}
                </label>
              ))}
            </div>
          )}
          <div className="space-y-1.5">
            <Label htmlFor={fieldId('note')}>核对备注或跳过原因（选填）</Label>
            <Textarea
              id={fieldId('note')}
              rows={1}
              value={values.note}
              onChange={(e) => change('note', e.target.value)}
            />
          </div>
          <div className="flex flex-wrap gap-2">
            <Button type="submit" variant="outline" className="min-h-11">
              {save.isPending ? '正在保存…' : '保存'}
            </Button>
            <Button
              type="button"
              className="min-h-11"
              onClick={() => submit('confirmed')}
            >
              标记已核对
            </Button>
            <Button
              type="button"
              variant="ghost"
              className="min-h-11"
              onClick={() => submit('skipped')}
            >
              跳过
            </Button>
          </div>
        </fieldset>
        <MutationError error={save.error} />
        {edited && values.revision !== draft.revision && (
          <p role="alert" className="text-xs text-destructive">
            草稿已更新，请重新加载后核对。
          </p>
        )}
        {edited && (
          <Button
            type="button"
            variant="ghost"
            disabled={save.isPending}
            onClick={() => {
              setEdited(null)
              onDirty(false)
              save.reset()
            }}
          >
            放弃未保存修改，重新加载当前记录
          </Button>
        )}
        {original && (
          <details className="rounded border p-2 text-xs">
            <summary className="cursor-pointer">查看首次 AI 识别作答</summary>
            <div className="mt-2 break-words">
              <MathText text={original.student_answer ?? '未识别到作答'} />
            </div>
          </details>
        )}
        <div className="flex flex-wrap gap-2 text-xs">
          {record.source_refs.map((ref, i) => (
            <a
              key={i}
              href={`/api/homework-drafts/${encodeURIComponent(draft.draft_id)}/sources/${encodeURIComponent(ref.source_id)}${ref.page ? `#page=${ref.page}` : ''}`}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex min-h-11 items-center underline underline-offset-2"
            >
              核对原件
              {ref.page
                ? ` · 第 ${ref.page} 页`
                : ref.row
                  ? ` · 第 ${ref.row} 行`
                  : ''}
            </a>
          ))}
        </div>
      </form>
    </details>
  )
}

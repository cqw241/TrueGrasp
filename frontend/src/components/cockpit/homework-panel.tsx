import { useMutation } from '@tanstack/react-query'
import { FileText, LoaderCircle, Upload } from 'lucide-react'
import { useRef, useState, type FormEvent } from 'react'
import { api, fetchClient, queryClient } from '@/api/client'
import type { HomeworkDraft } from '@/api/schema'
import { ErrorState } from '@/components/states'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { MutationError } from './mutation-error'
import { HomeworkReview } from './homework-review'

const statusLabel = {
  parsing: '正在解析',
  review: '待审核',
  parse_failed: '解析失败',
  published: '已发布',
}
const accept = '.csv,.jpg,.jpeg,.png,.webp,.pdf'

export function HomeworkPanel({ classId }: { classId: string }) {
  const [selectedId, select] = useState<string | null>(null)
  const drafts = api.useQuery(
    'get',
    '/api/homework-drafts',
    { params: { query: { class_id: classId } } },
    {
      refetchInterval: (query) =>
        query.state.data?.some((draft) => draft.status === 'parsing')
          ? 1500
          : false,
    },
  )
  const currentId = selectedId ?? drafts.data?.[0]?.draft_id
  const current = api.useQuery(
    'get',
    '/api/homework-drafts/{draft_id}',
    {
      params: { path: { draft_id: currentId ?? '' } },
    },
    {
      enabled: Boolean(currentId),
      refetchInterval: (query) =>
        query.state.data?.status === 'parsing' ||
        query.state.data?.active_publication_id
          ? 1500
          : false,
    },
  )

  function refresh() {
    void queryClient.invalidateQueries({
      queryKey: ['get', '/api/homework-drafts'],
    })
    void queryClient.invalidateQueries({
      queryKey: ['get', '/api/homework-drafts/{draft_id}'],
    })
  }

  return (
    <div className="space-y-5 p-4">
      <p className="text-sm text-muted-foreground">
        上传作业或直接输入题目，AI
        整理题干、作答和识别疑点，核对后再发布给本班学生。
      </p>
      <UploadForm
        classId={classId}
        onCreated={(draft) => {
          select(draft.draft_id)
          refresh()
        }}
      />
      {drafts.error && (
        <ErrorState
          error={drafts.error}
          onRetry={() => void drafts.refetch()}
        />
      )}
      {drafts.data && drafts.data.length > 0 && (
        <div className="space-y-2">
          <Label htmlFor="homework-draft-select">我的作业草稿</Label>
          <select
            id="homework-draft-select"
            className="w-full rounded-lg border bg-background p-2 text-sm"
            value={currentId}
            onChange={(event) => select(event.target.value)}
          >
            {drafts.data.map((draft) => (
              <option key={draft.draft_id} value={draft.draft_id}>
                {draft.title} · {statusLabel[draft.status]}
              </option>
            ))}
          </select>
        </div>
      )}
      {current.error && (
        <ErrorState
          error={current.error}
          onRetry={() => void current.refetch()}
        />
      )}
      {current.data && (
        <DraftView
          key={current.data.draft_id}
          draft={current.data}
          classId={classId}
          onChanged={refresh}
        />
      )}
    </div>
  )
}

function UploadForm({
  classId,
  onCreated,
}: {
  classId: string
  onCreated: (draft: HomeworkDraft) => void
}) {
  const [title, setTitle] = useState('')
  const [text, setText] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const fileInput = useRef<HTMLInputElement>(null)
  const key = useRef(crypto.randomUUID())
  const create = useMutation({
    mutationFn: async () => {
      const form = new FormData()
      form.append('class_id', classId)
      form.append('title', title.trim() || '作业解析')
      if (text.trim()) form.append('prompt_text', text)
      files.forEach((file) => form.append('files', file))
      const { data } = await fetchClient.POST('/api/homework-drafts', {
        params: { header: { 'Idempotency-Key': key.current } },
        body: {
          title: title.trim() || '作业解析',
          prompt_text: text,
          class_id: classId,
        },
        bodySerializer: () => form,
      })
      if (!data) throw new Error('未收到草稿，请重试')
      return data
    },
    onSuccess: (draft) => {
      onCreated(draft)
      setTitle('')
      setText('')
      setFiles([])
      if (fileInput.current) fileInput.current.value = ''
      key.current = crypto.randomUUID()
    },
  })
  function changed() {
    key.current = crypto.randomUUID()
    create.reset()
  }
  function submit(event: FormEvent) {
    event.preventDefault()
    create.mutate()
  }
  return (
    <form onSubmit={submit} aria-busy={create.isPending} className="space-y-3">
      <fieldset disabled={create.isPending} className="space-y-3">
        <div className="space-y-1.5">
          <Label htmlFor="homework-title">作业名称（选填）</Label>
          <Input
            id="homework-title"
            value={title}
            maxLength={200}
            placeholder="例如：第二讲课后作业"
            onChange={(event) => {
              setTitle(event.target.value)
              changed()
            }}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="homework-files">作业文件</Label>
          <Input
            id="homework-files"
            ref={fileInput}
            type="file"
            className="min-h-11"
            multiple
            accept={accept}
            aria-describedby="homework-file-help"
            onChange={(event) => {
              setFiles(Array.from(event.target.files ?? []))
              changed()
            }}
          />
          <p id="homework-file-help" className="text-xs text-muted-foreground">
            CSV、JPEG/PNG/WebP、PDF；最多 6 个文件、合计 30 MiB。PDF 每份最多 12
            页。
          </p>
          {files.length > 0 && (
            <p className="text-xs break-words">
              已选：{files.map((file) => file.name).join('、')}
            </p>
          )}
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="homework-text">题目与作答（也可直接输入）</Label>
          <Textarea
            id="homework-text"
            rows={3}
            maxLength={60000}
            value={text}
            placeholder="粘贴题目、学生姓名线索和原始作答；也可补充上传材料的背景说明。"
            onChange={(event) => {
              setText(event.target.value)
              changed()
            }}
          />
        </div>
        <Button
          type="submit"
          className="min-h-11"
          disabled={files.length === 0 && !text.trim()}
        >
          {create.isPending ? (
            <LoaderCircle className="animate-spin" aria-hidden />
          ) : (
            <Upload aria-hidden />
          )}
          {create.isPending ? '正在上传…' : '生成解析草稿'}
        </Button>
      </fieldset>
      <MutationError error={create.error} />
    </form>
  )
}

function sourceUrl(draftId: string, sourceId: string) {
  return `/api/homework-drafts/${encodeURIComponent(draftId)}/sources/${encodeURIComponent(sourceId)}`
}

function DraftView({
  draft,
  classId,
  onChanged,
}: {
  draft: HomeworkDraft
  classId: string
  onChanged: () => void
}) {
  const retry = api.useMutation(
    'post',
    '/api/homework-drafts/{draft_id}/parse-retries',
    { onSuccess: onChanged },
  )
  const add = useMutation({
    mutationFn: async (files: File[]) => {
      const form = new FormData()
      form.append('expected_revision', String(draft.revision))
      files.forEach((file) => form.append('files', file))
      return fetchClient.POST('/api/homework-drafts/{draft_id}/sources', {
        params: { path: { draft_id: draft.draft_id } },
        body: { expected_revision: draft.revision, files: [] },
        bodySerializer: () => form,
      })
    },
    onSuccess: onChanged,
  })
  const parsing = draft.status === 'parsing'
  const busy =
    parsing ||
    Boolean(draft.active_publication_id) ||
    retry.isPending ||
    add.isPending
  const failedSources = [
    ...new Set(draft.parse_errors.map((error) => error.source_id)),
  ]
  return (
    <section className="space-y-3 border-t pt-4" aria-label="解析草稿">
      <div className="flex flex-wrap items-center gap-2" role="status">
        {parsing && (
          <LoaderCircle className="size-4 animate-spin" aria-hidden />
        )}
        <h3 className="break-words font-medium">{draft.title}</h3>
        <Badge variant="secondary">{statusLabel[draft.status]}</Badge>
        <span className="text-xs text-muted-foreground">
          {draft.questions.length} 题 · {draft.records.length} 份作答
        </span>
      </div>
      <p className="text-xs text-muted-foreground">
        核对本班学生、题目与原始作答后发布；未核对或跳过的记录不会导入。
        {parsing && '正在逐页处理，已完成内容会持续保留。'}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        {draft.sources.map((source) => (
          <a
            key={source.source_id}
            href={sourceUrl(draft.draft_id, source.source_id)}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex min-h-11 max-w-full items-center gap-1 rounded border px-2 text-xs underline underline-offset-2 break-all"
          >
            <FileText className="size-3 shrink-0" aria-hidden />
            {source.filename}
            {source.page_count ? `（${source.page_count} 页）` : ''}
          </a>
        ))}
      </div>
      {draft.parse_errors.length > 0 && (
        <div
          className="space-y-2 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950"
          role="alert"
        >
          <p className="font-medium">部分材料未能解析，已完成内容已保留</p>
          <ul className="list-disc space-y-1 pl-4">
            {draft.parse_errors.map((error, index) => (
              <li key={index}>
                {
                  draft.sources.find(
                    (source) => source.source_id === error.source_id,
                  )?.filename
                }
                {error.page ? ` · 第 ${error.page} 页` : ''}：{error.message}
              </li>
            ))}
          </ul>
          <Button
            variant="outline"
            className="min-h-11"
            disabled={busy}
            onClick={() =>
              retry.mutate({
                params: { path: { draft_id: draft.draft_id } },
                body: {
                  expected_revision: draft.revision,
                  source_ids: failedSources,
                },
              })
            }
          >
            重试失败材料
          </Button>
        </div>
      )}
      <MutationError error={retry.error ?? add.error} />
      <div className="space-y-1.5">
        <Label htmlFor={`supplement-${draft.draft_id}`}>
          补充清晰图片或遗漏页面
        </Label>
        <Input
          id={`supplement-${draft.draft_id}`}
          type="file"
          className="min-h-11"
          accept={accept}
          multiple
          disabled={busy}
          onChange={(event) => {
            const files = Array.from(event.target.files ?? [])
            if (files.length) add.mutate(files)
            event.target.value = ''
          }}
        />
      </div>
      <HomeworkReview
        draft={draft}
        classId={classId}
        busy={busy}
        onChanged={onChanged}
      />
    </section>
  )
}

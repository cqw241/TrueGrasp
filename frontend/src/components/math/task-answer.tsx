import { Camera, ImagePlus } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { ApiError, api, fetchClient } from '@/api/client'
import type { AnswerTarget } from '@/api/schema'
import { MathInput } from '@/components/math/math-input'
import { Tex } from '@/components/math/tex'
import { ErrorState, LoadingState } from '@/components/states'
import { Button } from '@/components/ui/button'
import { useStreamEvent } from '@/hooks/use-event-stream'

const acceptedTypes = 'image/jpeg,image/png,image/webp'
const maxPhotoBytes = 8 * 1024 * 1024

export function TaskAnswer({ taskId }: { taskId: number }) {
  const [photoExpired, setPhotoExpired] = useState(false)
  const params = { path: { task_id: taskId } }
  const target = api.useQuery(
    'get',
    '/api/tasks/{task_id}/answer-target',
    { params },
    {
      refetchInterval: (query) =>
        query.state.data?.phase === 'closed' ? false : 1800,
    },
  )
  const start = api.useMutation('post', '/api/tasks/{task_id}/start')
  useStreamEvent('message_done', (event) => {
    if (event.task_id === taskId) void target.refetch()
  })
  useStreamEvent('task_changed', (event) => {
    if (event.task_id === taskId) void target.refetch()
  })

  if (target.isError && !target.data) {
    return (
      <ErrorState error={target.error} onRetry={() => void target.refetch()} />
    )
  }
  if (!target.data) return <LoadingState rows={2} />
  // 题目和辅导消息由对话记录（task-session.tsx）展示，这里只负责作答。
  const { phase, item_id: itemId, turn } = target.data
  if (phase === 'closed') {
    return <p className="text-muted-foreground text-sm">任务已结束。</p>
  }
  return (
    <div className="space-y-3">
      {photoExpired && (
        <p role="status" className="text-sm">
          题目已更新，本次照片未用于作答。请为当前题目重新拍摄。
        </p>
      )}
      {target.isError && (
        <ErrorState
          error={target.error}
          onRetry={() => void target.refetch()}
        />
      )}
      {phase === 'failed' && (
        <p role="alert" className="text-sm">
          任务处理失败。可以从当前进度继续，无需重新上传已经提交的答案。
        </p>
      )}
      {(phase === 'idle' || phase === 'failed') && (
        <Button
          disabled={start.isPending}
          onClick={() =>
            start.mutate({ params }, { onSettled: () => void target.refetch() })
          }
        >
          {start.isPending
            ? '正在进入…'
            : phase === 'failed'
              ? '继续处理'
              : '开始或继续任务'}
        </Button>
      )}
      {start.isError && (phase === 'idle' || phase === 'failed') && (
        <ErrorState error={start.error} title="无法打开任务" />
      )}
      {phase === 'processing' && (
        <p role="status" className="text-sm">
          正在处理，请稍候…
        </p>
      )}
      {(phase === 'waiting' || (phase === 'processing' && itemId)) && (
        <AnswerEditor
          key={`${turn ?? 0}:${itemId ?? 'text'}`}
          taskId={taskId}
          target={target.data}
          disabled={target.isError || phase !== 'waiting'}
          setPhotoExpired={setPhotoExpired}
          onRefresh={async () => {
            await target.refetch()
          }}
        />
      )}
    </div>
  )
}

function AnswerEditor({
  taskId,
  target,
  disabled,
  setPhotoExpired,
  onRefresh,
}: {
  taskId: number
  target: AnswerTarget
  disabled: boolean
  setPhotoExpired: (expired: boolean) => void
  onRefresh: () => Promise<void>
}) {
  const { item_id: itemId, turn = 0 } = target
  const acceptsText = target.accepts_text || !itemId
  const [answer, setAnswer] = useState('')
  const [preview, setPreview] = useState('')
  const [doubts, setDoubts] = useState<string[] | null>(null)
  const [replacement, setReplacement] = useState<{
    latex: string
    doubts: string[]
  } | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<Error | null>(null)
  const [sending, setSending] = useState(false)
  const [sendError, setSendError] = useState<Error | null>(null)
  const camera = useRef<HTMLInputElement>(null)
  const files = useRef<HTMLInputElement>(null)
  const objectUrl = useRef('')
  const upload = useRef<AbortController | null>(null)
  const submission = useRef(false)

  useEffect(
    () => () => {
      upload.current?.abort()
      if (objectUrl.current) URL.revokeObjectURL(objectUrl.current)
    },
    [],
  )

  const busy = disabled || uploading || sending

  async function recognize(file: File) {
    if (!itemId || busy || upload.current || submission.current) return
    setUploadError(null)
    if (!acceptedTypes.split(',').includes(file.type)) {
      setUploadError(new Error('请选择 JPEG、PNG 或 WebP 图片。'))
      return
    }
    if (file.size === 0 || file.size > maxPhotoBytes) {
      setUploadError(new Error('照片不能为空，大小不能超过 8 MB。'))
      return
    }
    const controller = new AbortController()
    setPhotoExpired(false)
    upload.current = controller
    setUploading(true)
    if (objectUrl.current) URL.revokeObjectURL(objectUrl.current)
    objectUrl.current = URL.createObjectURL(file)
    setPreview(objectUrl.current)
    setReplacement(null)
    // 识别无疑问、学生也还没写答案时直接提交，进入诊断；有疑问时请学生核对后再提交
    // （技术文档第 8 节第 5 条）。T06 的 requires_confirmation 恒为真，这里只看 doubts。
    let direct: string | null = null
    try {
      const { data } = await fetchClient.POST(
        '/api/tasks/{task_id}/answer-photo',
        {
          params: {
            path: { task_id: taskId },
            query: { item_id: itemId, turn },
          },
          headers: { 'Content-Type': file.type },
          signal: controller.signal,
          body: '',
          bodySerializer: () => file,
        },
      )
      if (!controller.signal.aborted && data) {
        if (answer.trim()) {
          setReplacement({ latex: data.latex, doubts: data.doubts })
        } else if (data.doubts.length === 0) {
          setAnswer(data.latex)
          direct = data.latex
        } else {
          setAnswer(data.latex)
          setDoubts(data.doubts)
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        if (error instanceof ApiError && error.status === 409) {
          setPhotoExpired(true)
          await onRefresh()
        } else {
          setUploadError(
            error instanceof Error ? error : new Error('照片识别失败'),
          )
        }
      }
    } finally {
      upload.current = null
      if (!controller.signal.aborted) setUploading(false)
    }
    if (direct !== null) await submit(direct)
  }

  function removePhoto() {
    if (objectUrl.current) URL.revokeObjectURL(objectUrl.current)
    objectUrl.current = ''
    setPreview('')
    setUploadError(null)
    setReplacement(null)
  }

  async function submit(latex = answer) {
    const content = latex.trim()
    if (!content || disabled || upload.current || submission.current) return
    submission.current = true
    setSending(true)
    setSendError(null)
    try {
      await fetchClient.POST('/api/tasks/{task_id}/messages', {
        params: { path: { task_id: taskId }, query: { expected_turn: turn } },
        body: {
          content,
          answering_item_id: itemId ?? null,
        },
      })
      await onRefresh()
    } catch (error) {
      setSendError(error instanceof Error ? error : new Error('提交失败'))
      if (error instanceof ApiError && error.status === 409) await onRefresh()
    } finally {
      submission.current = false
      setSending(false)
    }
  }

  return (
    <form
      className="space-y-3"
      onSubmit={(event) => {
        event.preventDefault()
        void submit()
      }}
    >
      {itemId ? (
        <>
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              variant="outline"
              disabled={busy}
              onClick={() => camera.current?.click()}
            >
              <Camera aria-hidden /> 拍照
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={busy}
              onClick={() => files.current?.click()}
            >
              <ImagePlus aria-hidden /> 选择图片
            </Button>
            <input
              ref={camera}
              type="file"
              accept={acceptedTypes}
              capture="environment"
              hidden
              onChange={(event) => {
                const file = event.currentTarget.files?.[0]
                event.currentTarget.value = ''
                if (file) void recognize(file)
              }}
            />
            <input
              ref={files}
              type="file"
              accept={acceptedTypes}
              hidden
              onChange={(event) => {
                const file = event.currentTarget.files?.[0]
                event.currentTarget.value = ''
                if (file) void recognize(file)
              }}
            />
          </div>
          {preview && (
            <div className="space-y-2">
              <img
                src={preview}
                alt="作答照片预览"
                className="max-h-48 rounded-md border object-contain"
              />
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={busy}
                onClick={removePhoto}
              >
                移除照片
              </Button>
            </div>
          )}
          {uploading && (
            <p role="status" className="text-sm">
              正在识别照片…
            </p>
          )}
          {uploadError && (
            <ErrorState error={uploadError} title="照片识别失败" />
          )}
          {replacement && (
            <div className="bg-muted space-y-2 rounded-md p-3 text-sm">
              <p>新照片已识别，当前答案仍保留。</p>
              <Tex latex={replacement.latex} display />
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => {
                  setAnswer(replacement.latex)
                  setDoubts(replacement.doubts)
                  setReplacement(null)
                }}
              >
                用识别结果替换
              </Button>
            </div>
          )}
          {doubts !== null && (
            <div role="status" className="bg-muted rounded-md p-3 text-sm">
              <p>请核对识别结果，必要时修改，再提交作答。</p>
              {doubts.length > 0 && (
                <ul className="mt-1 list-disc pl-5">
                  {doubts.map((doubt, index) => (
                    <li key={index}>{doubt}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </>
      ) : null}
      {!acceptsText ? (
        <>
          <MathInput
            value={answer}
            onChange={setAnswer}
            disabled={busy}
            placeholder="输入或校对公式"
          />
          {answer && (
            <div className="bg-muted/50 rounded-md p-3">
              <Tex latex={answer} display />
            </div>
          )}
        </>
      ) : (
        <textarea
          aria-label="回复"
          className="bg-background min-h-24 w-full rounded-md border p-3"
          placeholder={itemId ? '输入公式或说明卡在哪一步' : '输入回复'}
          value={answer}
          disabled={busy}
          onChange={(event) => setAnswer(event.target.value)}
        />
      )}
      {sendError && <ErrorState error={sendError} title="作答未提交" />}
      <Button type="submit" disabled={busy || !answer.trim()}>
        {sending ? '提交中…' : doubts !== null ? '确认并提交' : '提交'}
      </Button>
    </form>
  )
}

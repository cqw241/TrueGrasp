import { Camera, ImagePlus, X } from 'lucide-react'
import { useRef, useState } from 'react'
import { fetchClient } from '@/api/client'
import type { Task } from '@/api/schema'
import { MathText } from '@/components/math/tex'
import { ErrorState } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'

const acceptedTypes = 'image/jpeg,image/png,image/webp'
const maxPhotoBytes = 8 * 1024 * 1024

/**
 * 学生主动提问（技术文档第 5 节 ⑥）。提交后后端创建一条 question 任务，
 * 辅导 Agent 先请学生尝试，再按对话给出分层提示；提示在任务会话中展示。
 */
export function AskQuestion({
  studentId,
  onCreated,
  onCancel,
}: {
  studentId: string
  onCreated: (task: Task) => void
  onCancel: () => void
}) {
  const [content, setContent] = useState('')
  const [attempt, setAttempt] = useState('')
  const [doubts, setDoubts] = useState<string[]>([])
  const [recognizing, setRecognizing] = useState(false)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<{ title: string; error: unknown } | null>(
    null,
  )
  const camera = useRef<HTMLInputElement>(null)
  const files = useRef<HTMLInputElement>(null)
  const busy = recognizing || sending

  async function recognize(file: File) {
    setError(null)
    if (!acceptedTypes.split(',').includes(file.type)) {
      setError({
        title: '照片识别失败',
        error: new Error('请选择 JPEG、PNG 或 WebP 图片。'),
      })
      return
    }
    if (file.size === 0 || file.size > maxPhotoBytes) {
      setError({
        title: '照片识别失败',
        error: new Error('照片不能为空，大小不能超过 8 MB。'),
      })
      return
    }
    setRecognizing(true)
    try {
      const { data } = await fetchClient.POST(
        '/api/students/{student_id}/question-photo',
        {
          params: { path: { student_id: studentId } },
          headers: { 'Content-Type': file.type },
          body: '',
          bodySerializer: () => file,
        },
      )
      if (data) {
        // 识别结果是 LaTeX；包成行内公式，学生可以在文本框里补充文字或修改。
        setContent(data.latex.includes('$') ? data.latex : `$${data.latex}$`)
        setDoubts(data.doubts)
      }
    } catch (err) {
      setError({ title: '照片识别失败', error: err })
    } finally {
      setRecognizing(false)
    }
  }

  async function submit() {
    if (!content.trim() || busy) return
    setSending(true)
    setError(null)
    try {
      const { data } = await fetchClient.POST(
        '/api/students/{student_id}/questions',
        {
          params: { path: { student_id: studentId } },
          body: { content: content.trim(), attempt: attempt.trim() },
        },
      )
      if (data) onCreated(data)
    } catch (err) {
      setError({ title: '提问未提交', error: err })
    } finally {
      setSending(false)
    }
  }

  function photoInput(
    ref: React.RefObject<HTMLInputElement | null>,
    capture?: 'environment',
  ) {
    return (
      <input
        ref={ref}
        type="file"
        accept={acceptedTypes}
        capture={capture}
        hidden
        onChange={(event) => {
          const file = event.currentTarget.files?.[0]
          event.currentTarget.value = ''
          if (file) void recognize(file)
        }}
      />
    )
  }

  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault()
        void submit()
      }}
    >
      <div className="flex items-start justify-between gap-2">
        <p className="text-muted-foreground text-sm">
          说出哪道题没做出来。辅导老师不会直接给答案，会先请你试一试，再一层层提示。
        </p>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="取消提问"
          onClick={onCancel}
        >
          <X />
        </Button>
      </div>

      <div className="space-y-2">
        <label htmlFor="question-content" className="text-sm font-medium">
          题目
        </label>
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={busy}
            onClick={() => camera.current?.click()}
          >
            <Camera aria-hidden /> 拍题目
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={busy}
            onClick={() => files.current?.click()}
          >
            <ImagePlus aria-hidden /> 选择图片
          </Button>
          {photoInput(camera, 'environment')}
          {photoInput(files)}
        </div>
        {recognizing && (
          <p role="status" className="text-sm">
            正在识别题目…
          </p>
        )}
        <Textarea
          id="question-content"
          placeholder="输入题目，公式写在 $ $ 中，如：求 $\int x e^x\,dx$"
          value={content}
          disabled={busy}
          onChange={(event) => setContent(event.target.value)}
        />
        {doubts.length > 0 && (
          <div role="status" className="bg-muted rounded-md p-3 text-sm">
            <p>请核对识别结果，必要时修改：</p>
            <ul className="mt-1 list-disc pl-5">
              {doubts.map((doubt, index) => (
                <li key={index}>{doubt}</li>
              ))}
            </ul>
          </div>
        )}
        {content.trim() && (
          <div className="bg-muted/50 rounded-md p-3 text-sm">
            <MathText text={content} />
          </div>
        )}
      </div>

      <div className="space-y-2">
        <label htmlFor="question-attempt" className="text-sm font-medium">
          我的尝试或卡住的地方（可选）
        </label>
        <Textarea
          id="question-attempt"
          placeholder="例如：换元后不知道 dx 怎么变"
          value={attempt}
          disabled={busy}
          onChange={(event) => setAttempt(event.target.value)}
        />
      </div>

      {error && <ErrorState error={error.error} title={error.title} />}
      <Button type="submit" disabled={busy || !content.trim()}>
        {sending ? '提交中…' : '提问'}
      </Button>
    </form>
  )
}

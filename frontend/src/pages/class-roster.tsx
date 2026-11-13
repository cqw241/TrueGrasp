import { useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router'
import { api, queryClient } from '@/api/client'
import { PageHeader } from '@/components/layout/app-layout'
import { QueryView } from '@/components/states'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import type { ClassView, MemberView } from '@/api/schema'

const IMPORT_STATUS = {
  added: { label: '成功', variant: 'default' },
  duplicate: { label: '重复', variant: 'secondary' },
  missing: { label: '缺失', variant: 'outline' },
  failed: { label: '失败', variant: 'destructive' },
} as const

function FormError({ error }: { error: unknown }) {
  if (!error) return null
  return (
    <p className="text-destructive text-sm" role="alert">
      {error instanceof Error ? error.message : '请求失败，请重试'}
    </p>
  )
}

function refresh() {
  return queryClient.invalidateQueries()
}

export default function ClassRoster() {
  const classes = api.useQuery('get', '/api/classes')
  const create = api.useMutation('post', '/api/classes')
  const [params, setParams] = useSearchParams()
  const [name, setName] = useState('')
  const selected =
    classes.data?.find((row) => row.class_id === params.get('class_id')) ??
    classes.data?.[0]

  async function submit(event: FormEvent) {
    event.preventDefault()
    await create
      .mutateAsync({ body: { name } })
      .then((group) => {
        setName('')
        setParams({ class_id: group.class_id })
        void refresh()
      })
      .catch(() => {})
  }

  return (
    <div className="mx-auto grid w-full max-w-5xl gap-6">
      <PageHeader
        title="班级管理"
        description="创建班级、维护学生名单，并为学生发放登录设置链接。"
      />
      <form
        onSubmit={submit}
        className="rounded-xl border bg-card p-5 grid gap-3"
      >
        <Label htmlFor="new-class">新班级名称</Label>
        <div className="flex flex-wrap gap-2">
          <Input
            id="new-class"
            className="w-full sm:w-72"
            value={name}
            required
            maxLength={128}
            onChange={(event) => setName(event.target.value)}
          />
          <Button disabled={create.isPending || !name.trim()}>创建班级</Button>
        </div>
        <FormError error={create.error} />
      </form>
      <QueryView query={classes}>
        {(rows) =>
          selected ? (
            <>
              <div className="flex flex-wrap gap-2" aria-label="我的班级">
                {rows.map((row) => (
                  <Button
                    key={row.class_id}
                    variant={
                      row.class_id === selected.class_id ? 'default' : 'outline'
                    }
                    aria-pressed={row.class_id === selected.class_id}
                    onClick={() => setParams({ class_id: row.class_id })}
                  >
                    {row.name}
                  </Button>
                ))}
              </div>
              <Roster key={selected.class_id} group={selected} />
            </>
          ) : (
            <p className="text-muted-foreground">
              还没有班级。请先创建一个班级。
            </p>
          )
        }
      </QueryView>
    </div>
  )
}

function Roster({ group }: { group: ClassView }) {
  const classId = group.class_id
  const scope = { path: { class_id: classId } }
  const roster = api.useQuery('get', '/api/classes/{class_id}/students', {
    params: scope,
  })
  const candidates = api.useQuery('get', '/api/classes/available-students')
  const rename = api.useMutation('patch', '/api/classes/{class_id}')
  const enroll = api.useMutation('post', '/api/classes/{class_id}/students')
  const batch = api.useMutation(
    'post',
    '/api/classes/{class_id}/students/import',
  )
  const [className, setClassName] = useState(group.name)
  const [studentName, setStudentName] = useState('')
  const [studentId, setStudentId] = useState('')
  const [number, setNumber] = useState('')
  const [lines, setLines] = useState('')

  function add(event: FormEvent) {
    event.preventDefault()
    enroll.mutate(
      {
        params: scope,
        body: {
          ...(studentId ? { student_id: studentId } : { name: studentName }),
          student_number: number.trim() || null,
        },
      },
      {
        onSuccess: () => {
          setStudentName('')
          setStudentId('')
          setNumber('')
          void refresh()
        },
      },
    )
  }

  return (
    <section className="grid gap-5">
      <div className="flex items-center justify-between gap-4">
        <h2 className="text-xl font-semibold">{group.name}</h2>
        <Button asChild variant="outline">
          <Link to={`/teacher?class_id=${classId}`}>查看教学驾驶舱</Link>
        </Button>
      </div>
      <form
        className="grid gap-2 rounded-xl border bg-card p-5"
        onSubmit={(event) => {
          event.preventDefault()
          rename.mutate(
            { params: scope, body: { name: className } },
            { onSuccess: () => void refresh() },
          )
        }}
      >
        <Label htmlFor="class-name">修改班级名称</Label>
        <div className="flex flex-wrap gap-2">
          <Input
            className="w-full sm:w-72"
            id="class-name"
            value={className}
            maxLength={128}
            required
            onChange={(event) => setClassName(event.target.value)}
          />
          <Button
            variant="outline"
            disabled={
              rename.isPending ||
              !className.trim() ||
              className.trim() === group.name
            }
          >
            保存名称
          </Button>
        </div>
        <FormError error={rename.error} />
      </form>
      <div className="grid gap-5 md:grid-cols-2">
        <form
          onSubmit={add}
          className="grid content-start gap-3 rounded-xl border bg-card p-5"
        >
          <h3 className="font-semibold">添加学生</h3>
          <Label htmlFor="existing-student">学生身份</Label>
          <select
            id="existing-student"
            className="rounded-md border bg-background p-2 text-sm"
            value={studentId}
            onChange={(event) => setStudentId(event.target.value)}
          >
            <option value="">创建新学生</option>
            {candidates.data?.map((student) => (
              <option key={student.id} value={student.id}>
                {student.name}（身份尾号 {student.id.slice(-8)}）
              </option>
            ))}
          </select>
          <FormError error={candidates.error} />
          {!studentId && (
            <>
              <Label htmlFor="student-name">姓名</Label>
              <Input
                id="student-name"
                required
                maxLength={128}
                value={studentName}
                onChange={(event) => setStudentName(event.target.value)}
              />
            </>
          )}
          <Label htmlFor="student-number">班内学号（选填）</Label>
          <Input
            id="student-number"
            maxLength={128}
            value={number}
            onChange={(event) => setNumber(event.target.value)}
          />
          <Button
            className="justify-self-start"
            disabled={enroll.isPending || (!studentId && !studentName.trim())}
          >
            添加到班级
          </Button>
          <FormError error={enroll.error} />
          {enroll.isSuccess && (
            <p role="status" className="text-sm">
              已添加。可在名单中生成登录链接。
            </p>
          )}
        </form>
        <form
          className="grid content-start gap-3 rounded-xl border bg-card p-5"
          onSubmit={(event) => {
            event.preventDefault()
            const rows = lines
              .trimEnd()
              .split(/\r?\n/)
              .map((line) => line.split(/[,，\t]/))
            batch.mutate(
              { params: scope, body: { rows } },
              { onSuccess: () => void refresh() },
            )
          }}
        >
          <h3 className="font-semibold">批量导入名单</h3>
          <Label htmlFor="roster-import">
            每行填写姓名和班内学号，用逗号或制表符分隔，无需表头（最多 500
            行）。
          </Label>
          <Textarea
            id="roster-import"
            rows={5}
            maxLength={130000}
            placeholder="张同学,20260001"
            value={lines}
            onChange={(event) => setLines(event.target.value)}
          />
          <Button
            className="justify-self-start"
            disabled={!lines.trim() || batch.isPending}
          >
            导入名单
          </Button>
          <FormError error={batch.error} />
          {batch.data && (
            <ul className="max-h-48 overflow-auto text-sm" aria-live="polite">
              {batch.data.map((result) => (
                <li
                  key={result.row}
                  className="flex flex-wrap items-center gap-2 py-1"
                >
                  <Badge variant={IMPORT_STATUS[result.status].variant}>
                    {IMPORT_STATUS[result.status].label}
                  </Badge>
                  <span>
                    第 {result.row} 行 · {result.message}
                    {result.student && `（${result.student.name}）`}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </form>
      </div>
      <div className="grid gap-3 rounded-xl border bg-card p-5">
        <h3 className="font-semibold">在班名单</h3>
        <QueryView query={roster}>
          {(rows) =>
            rows.length ? (
              <ul className="divide-y">
                {rows.map((row) => (
                  <StudentRow key={row.student_id} row={row} />
                ))}
              </ul>
            ) : (
              <p className="text-muted-foreground text-sm">班级尚无学生。</p>
            )
          }
        </QueryView>
      </div>
    </section>
  )
}

function StudentRow({ row }: { row: MemberView }) {
  const issue = api.useMutation(
    'post',
    '/api/classes/{class_id}/students/{student_id}/setup-link',
    { gcTime: 0 },
  )
  const remove = api.useMutation(
    'delete',
    '/api/classes/{class_id}/students/{student_id}',
  )
  const scope = { path: { class_id: row.class_id, student_id: row.student_id } }
  const [confirm, setConfirm] = useState(false)
  const [copied, setCopied] = useState(false)
  const url =
    issue.data && new URL(issue.data.setup_path, window.location.origin).href

  return (
    <li className="grid gap-3 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span>
          <strong>{row.name}</strong>{' '}
          <span className="text-muted-foreground text-sm">
            {row.student_number ?? `身份尾号 ${row.student_id.slice(-8)}`}
          </span>
        </span>
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={issue.isPending || remove.isPending}
            onClick={() => {
              setCopied(false)
              issue.mutate({ params: scope })
            }}
          >
            发放登录 / 重置链接
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => setConfirm(!confirm)}
            disabled={remove.isPending}
          >
            退班
          </Button>
        </div>
      </div>
      {confirm && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span>退班后保留学习历史，并停止本班未完成任务。</span>
          <Button
            size="sm"
            variant="destructive"
            disabled={remove.isPending}
            onClick={() =>
              remove.mutate(
                { params: scope },
                {
                  onSuccess: () => {
                    issue.reset()
                    void refresh()
                  },
                },
              )
            }
          >
            确认退班
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setConfirm(false)}>
            取消
          </Button>
        </div>
      )}
      {url && (
        <div className="grid gap-2 rounded-md border p-3 text-sm">
          <p>
            账号：{issue.data.username}。链接有效期 24
            小时，仅可使用一次，请单独发给该学生。
          </p>
          <div className="flex flex-wrap gap-2">
            <Input
              className="min-w-0 flex-1"
              aria-label="一次性登录设置链接"
              readOnly
              value={url}
              onFocus={(event) => event.target.select()}
            />
            <Button
              size="sm"
              variant="outline"
              onClick={() =>
                void navigator.clipboard
                  ?.writeText(url)
                  .then(() => setCopied(true))
                  .catch(() => setCopied(false))
              }
            >
              {copied ? '已复制' : '复制链接'}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => issue.reset()}>
              收起
            </Button>
          </div>
        </div>
      )}
      <FormError error={issue.error || remove.error} />
    </li>
  )
}

import { useState, type FormEvent } from 'react'
import { api } from '@/api/client'
import type { MasteryState, TeachingGoal } from '@/api/schema'
import { EmptyState, QueryView } from '@/components/states'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet'
import { lectureLabel, masteryStateLabel } from '@/lib/labels'
import { MutationError } from './mutation-error'
import { useInvalidate, usePointNames } from './shared'

const TARGET_STATES: MasteryState[] = ['initial', 'consolidated', 'integrated']

export function GoalPanel({ classId }: { classId: string }) {
  const goals = api.useQuery('get', '/api/goals', {
    params: { query: { class_id: classId } },
  })
  const pointName = usePointNames()
  const [open, setOpen] = useState(false)

  return (
    <Card>
      <CardHeader>
        <CardTitle>教学目标</CardTitle>
        <CardDescription>规划 Agent 按目标差距安排辅导</CardDescription>
        <CardAction>
          <Sheet open={open} onOpenChange={setOpen}>
            <SheetTrigger asChild>
              <Button size="sm" variant="outline">
                设定目标
              </Button>
            </SheetTrigger>
            <SheetContent className="overflow-y-auto">
              <GoalForm classId={classId} onDone={() => setOpen(false)} />
            </SheetContent>
          </Sheet>
        </CardAction>
      </CardHeader>
      <CardContent>
        <QueryView
          query={goals}
          empty={
            <EmptyState
              title="还没有教学目标"
              description="设定目标后，驾驶舱按目标统计达标率"
            />
          }
        >
          {(data) => <GoalSummary goal={latest(data)} pointName={pointName} />}
        </QueryView>
      </CardContent>
    </Card>
  )
}

/** 与后端 ledger.queries.current_goal 一致：最新设定的目标生效。 */
function latest(goals: TeachingGoal[]): TeachingGoal {
  return goals.reduce((a, b) => ((b.id ?? 0) > (a.id ?? 0) ? b : a))
}

function GoalSummary({
  goal,
  pointName,
}: {
  goal: TeachingGoal
  pointName: (id: string) => string
}) {
  return (
    <div className="space-y-2 text-sm">
      <p>
        <span className="font-semibold">{goal.deadline}</span> 前{' '}
        <span className="font-semibold">
          {Math.round(goal.target_ratio * 100)}%
        </span>{' '}
        的学生达到“{masteryStateLabel[goal.target_state]}”
      </p>
      <p className="text-muted-foreground">
        辅导 {goal.max_rounds} 轮仍未通过即转交教师
      </p>
      <div className="flex flex-wrap gap-1.5">
        {(goal.knowledge_point_ids ?? []).map((id) => (
          <Badge key={id} variant="secondary">
            {pointName(id)}
          </Badge>
        ))}
      </div>
    </div>
  )
}

function GoalForm({
  onDone,
  classId,
}: {
  onDone: () => void
  classId: string
}) {
  const points = api.useQuery('get', '/api/knowledge/points')
  const create = api.useMutation('post', '/api/goals')
  const invalidate = useInvalidate()
  const [lecture, setLecture] = useState(2)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [targetState, setTargetState] = useState<MasteryState>('initial')
  const [ratio, setRatio] = useState(90)
  const [deadline, setDeadline] = useState('')
  const [maxRounds, setMaxRounds] = useState(2)

  const lecturePoints = (points.data ?? [])
    .filter((p) => p.lecture === lecture)
    .sort((a, b) => a.section - b.section)

  function toggle(id: string, checked: boolean) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    create.mutate(
      {
        params: { query: { class_id: classId } },
        body: {
          knowledge_point_ids: [...selected],
          target_state: targetState,
          target_ratio: ratio / 100,
          deadline,
          max_rounds: maxRounds,
        },
      },
      {
        onSuccess: () => {
          invalidate('/api/goals', '/api/class/overview')
          onDone()
        },
      },
    )
  }

  return (
    <form onSubmit={submit} className="flex h-full flex-col">
      <SheetHeader>
        <SheetTitle>设定教学目标</SheetTitle>
        <SheetDescription>
          新目标立即生效，规划 Agent 据此重新安排辅导
        </SheetDescription>
      </SheetHeader>
      <div className="flex-1 space-y-4 px-4">
        <div className="space-y-2">
          <Label id="goal-lecture-label">讲次</Label>
          <Select
            value={String(lecture)}
            onValueChange={(v) => {
              setLecture(Number(v))
              setSelected(new Set())
            }}
          >
            <SelectTrigger
              className="w-full"
              aria-labelledby="goal-lecture-label"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {Array.from({ length: 12 }, (_, i) => i + 1).map((n) => (
                <SelectItem key={n} value={String(n)}>
                  {lectureLabel(n)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <fieldset className="space-y-2">
          <legend className="mb-2 text-sm font-medium">知识点</legend>
          {lecturePoints.length === 0 && (
            <p className="text-muted-foreground text-sm">这一讲还没有知识点</p>
          )}
          {lecturePoints.map((p) => (
            <Label key={p.id} className="font-normal">
              <Checkbox
                checked={selected.has(p.id)}
                onCheckedChange={(c) => toggle(p.id, c === true)}
              />
              {p.section}. {p.name}
            </Label>
          ))}
        </fieldset>
        <div className="space-y-2">
          <Label id="goal-state-label">目标状态</Label>
          <Select
            value={targetState}
            onValueChange={(v) => setTargetState(v as MasteryState)}
          >
            <SelectTrigger
              className="w-full"
              aria-labelledby="goal-state-label"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {TARGET_STATES.map((s) => (
                <SelectItem key={s} value={s}>
                  {masteryStateLabel[s]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-2">
            <Label htmlFor="goal-ratio">达标比例（%）</Label>
            <Input
              id="goal-ratio"
              type="number"
              min={1}
              max={100}
              required
              value={ratio}
              onChange={(e) => setRatio(Number(e.target.value))}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="goal-rounds">辅导轮次上限</Label>
            <Input
              id="goal-rounds"
              type="number"
              min={1}
              required
              value={maxRounds}
              onChange={(e) => setMaxRounds(Number(e.target.value))}
            />
          </div>
        </div>
        <div className="space-y-2">
          <Label htmlFor="goal-deadline">截止日期</Label>
          <Input
            id="goal-deadline"
            type="date"
            required
            value={deadline}
            onChange={(e) => setDeadline(e.target.value)}
          />
        </div>
        <MutationError error={create.error} />
      </div>
      <SheetFooter>
        <Button
          type="submit"
          disabled={selected.size === 0 || create.isPending}
        >
          {create.isPending ? '保存中…' : '保存目标'}
        </Button>
      </SheetFooter>
    </form>
  )
}

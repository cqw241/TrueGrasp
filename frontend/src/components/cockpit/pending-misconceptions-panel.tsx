import { useState } from 'react'
import { api } from '@/api/client'
import type { Misconception } from '@/api/schema'
import { EmptyState, QueryView } from '@/components/states'
import { Tex } from '@/components/math/tex'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Textarea } from '@/components/ui/textarea'
import { MutationError } from './mutation-error'
import { useInvalidate, usePointNames } from './shared'

export function PendingMisconceptionsPanel({ classId }: { classId: string }) {
  const pending = api.useQuery('get', '/api/misconceptions/pending', {
    params: { query: { class_id: classId } },
  })
  return (
    <Card>
      <CardHeader>
        <CardTitle>待审核错因</CardTitle>
        <CardDescription>
          辅导中发现、错因库里还没有的错因；采纳后进入错因库，后续诊断可以检索到
        </CardDescription>
      </CardHeader>
      <CardContent>
        <QueryView
          query={pending}
          empty={<EmptyState title="没有待审核的错因" />}
        >
          {(data) => (
            <ul className="space-y-3">
              {data.map((m) => (
                <PendingItem key={m.id} misconception={m} />
              ))}
            </ul>
          )}
        </QueryView>
      </CardContent>
    </Card>
  )
}

function PendingItem({ misconception }: { misconception: Misconception }) {
  const pointName = usePointNames()
  const invalidate = useInvalidate()
  const adopt = api.useMutation(
    'post',
    '/api/misconceptions/{misconception_id}/adopt',
  )
  const [description, setDescription] = useState(misconception.description)

  return (
    <li className="space-y-2 rounded-md border p-3">
      <p className="text-muted-foreground text-xs">
        {pointName(misconception.knowledge_point_id)}
      </p>
      <Textarea
        aria-label="错因描述"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
      />
      {(misconception.examples ?? []).map((ex, i) => (
        <div key={i} className="overflow-x-auto text-sm">
          <p>{ex.question}</p>
          <p className="text-muted-foreground">
            错误 <Tex latex={ex.wrong} /> · 正确 <Tex latex={ex.correct} />
          </p>
        </div>
      ))}
      <MutationError error={adopt.error} />
      <Button
        size="sm"
        disabled={adopt.isPending || !description.trim()}
        onClick={() =>
          adopt.mutate(
            {
              params: { path: { misconception_id: misconception.id } },
              body: {
                description:
                  description === misconception.description
                    ? null
                    : description,
              },
            },
            { onSuccess: () => invalidate('/api/misconceptions/pending') },
          )
        }
      >
        {adopt.isPending ? '采纳中…' : '采纳'}
      </Button>
    </li>
  )
}

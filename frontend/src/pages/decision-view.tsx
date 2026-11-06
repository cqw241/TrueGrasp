import { ChevronRight } from 'lucide-react'
import { Fragment } from 'react'
import { api } from '@/api/client'
import { PageHeader } from '@/components/layout/app-layout'
import { EmptyState, QueryView } from '@/components/states'
import { Badge } from '@/components/ui/badge'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { lectureLabel } from '@/lib/labels'

// 两条精修路径（技术文档第 8 节）。
const PATHS = [
  { source: 'calc.ch1.composition', target: 'calc.ch9.multivar_chain' },
  { source: 'calc.ch4.substitution', target: 'calc.ch10.polar' },
]

function KnowledgePath({ source, target }: (typeof PATHS)[number]) {
  const path = api.useQuery('get', '/api/knowledge/path', {
    params: { query: { source, target } },
  })
  return (
    <QueryView
      query={path}
      empty={<EmptyState title="两个知识点之间没有先修路径" />}
    >
      {(points) => (
        <ol className="flex flex-wrap items-center gap-2">
          {points.map((p, i) => (
            <Fragment key={p.id}>
              {i > 0 && (
                <ChevronRight
                  className="text-muted-foreground size-4"
                  aria-hidden
                />
              )}
              <li>
                <Badge variant="outline" className="h-auto py-1">
                  <span className="text-muted-foreground">
                    {lectureLabel(p.lecture)}
                  </span>
                  {p.name}
                </Badge>
              </li>
            </Fragment>
          ))}
        </ol>
      )}
    </QueryView>
  )
}

// T10 只搭页面框架；假设面板、学习时间线、六维验证结果由 T13 实现。
export default function DecisionView() {
  return (
    <>
      <PageHeader
        title="决策证据视图"
        description="错因假设、学习时间线、独立验证结果与跨章节知识路径"
      />
      <Card>
        <CardHeader>
          <CardTitle>跨章节知识路径</CardTitle>
          <CardDescription>精修的两条先修路径</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {PATHS.map((p) => (
            <KnowledgePath key={p.source} {...p} />
          ))}
        </CardContent>
      </Card>
    </>
  )
}

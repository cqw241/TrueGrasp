import { HeatmapChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  VisualMapComponent,
} from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { useEffect, useRef } from 'react'
import { api } from '@/api/client'
import type { HeatCell } from '@/api/schema'
import { EmptyState, QueryView } from '@/components/states'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { lectureLabel } from '@/lib/labels'

echarts.use([
  HeatmapChart,
  GridComponent,
  TooltipComponent,
  VisualMapComponent,
  CanvasRenderer,
])

const LECTURES = Array.from({ length: 12 }, (_, i) => i + 1)
const CELL = 28

export function HeatmapPanel({ classId }: { classId: string }) {
  const heatmap = api.useQuery('get', '/api/class/heatmap', {
    params: { query: { class_id: classId } },
  })
  return (
    <Card>
      <CardHeader>
        <CardTitle>全书知识热力图</CardTitle>
        <CardDescription>
          每格一个知识点，颜色为达到“初步掌握”及以上的学生比例；灰色表示还没有学生被评估
        </CardDescription>
      </CardHeader>
      <CardContent>
        <QueryView
          query={heatmap}
          empty={
            <EmptyState
              title="知识图谱为空"
              description="在 backend 目录运行 uv run python -m app.knowledge ingest 后重启后端"
            />
          }
        >
          {(cells) => <Heatmap cells={cells} />}
        </QueryView>
      </CardContent>
    </Card>
  )
}

type Datum = { value: [number, number, number]; cell: HeatCell }

function Heatmap({ cells }: { cells: HeatCell[] }) {
  const ref = useRef<HTMLDivElement>(null)

  // 纵轴为讲，横轴为讲内知识点的顺序（按节排序）。
  const byLecture = new Map<number, HeatCell[]>()
  for (const cell of cells) {
    byLecture.set(cell.lecture, [...(byLecture.get(cell.lecture) ?? []), cell])
  }
  // 没有评估数据的格子记为 -1，落在 visualMap 范围外，显示为灰色。
  const data: Datum[] = []
  let columns = 1
  for (const [lecture, list] of byLecture) {
    list.sort((a, b) => a.section - b.section)
    columns = Math.max(columns, list.length)
    list.forEach((cell, x) => {
      data.push({
        value: [x, lecture - 1, cell.assessed > 0 ? cell.mastered_ratio : -1],
        cell,
      })
    })
  }

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const chart = echarts.init(el)
    chart.setOption({
      grid: { left: 64, right: 16, top: 8, bottom: 56 },
      tooltip: {
        formatter: (params: { data: Datum }) => {
          const c = params.data.cell
          const lines = [
            `<b>${c.name}</b>`,
            `${lectureLabel(c.lecture)} 第 ${c.section} 节`,
            `已评估 ${c.assessed} 人`,
          ]
          if (c.assessed > 0) {
            lines.push(`达标 ${Math.round(c.mastered_ratio * 100)}%`)
          }
          if (c.misconception_students > 0) {
            lines.push(`有已确认错因 ${c.misconception_students} 人`)
          }
          return lines.join('<br/>')
        },
      },
      xAxis: {
        type: 'category',
        data: Array.from({ length: columns }, (_, i) => i + 1),
        splitArea: { show: false },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'category',
        data: LECTURES.map(lectureLabel),
        inverse: true,
        splitArea: { show: false },
        axisTick: { show: false },
      },
      visualMap: {
        type: 'piecewise',
        orient: 'horizontal',
        left: 'center',
        bottom: 0,
        itemGap: 12,
        pieces: [
          { lt: 0, label: '未评估', color: '#e5e7eb' },
          { gte: 0, lt: 0.5, label: '<50%', color: '#fca5a5' },
          { gte: 0.5, lt: 0.8, label: '50–80%', color: '#fde68a' },
          { gte: 0.8, lt: 0.9, label: '80–90%', color: '#86efac' },
          { gte: 0.9, label: '≥90%', color: '#15803d' },
        ],
      },
      series: {
        type: 'heatmap',
        data,
        itemStyle: { borderColor: '#fff', borderWidth: 2 },
        emphasis: { itemStyle: { borderColor: '#111' } },
      },
    })
    const observer = new ResizeObserver(() => chart.resize())
    observer.observe(el)
    return () => {
      observer.disconnect()
      chart.dispose()
    }
  })

  return (
    <>
      <div className="overflow-x-auto" aria-hidden>
        <div
          ref={ref}
          style={{ minWidth: columns * CELL + 80, height: 12 * CELL + 64 }}
        />
      </div>
      {/* 画布对屏幕阅读器不可见，用表格提供同样的数据。sr-only 放在外层 div：
          表格会忽略 width 和 overflow，直接用在 table 上会撑宽页面。 */}
      <div className="sr-only">
        <table>
          <caption>全书知识热力图数据</caption>
          <thead>
            <tr>
              <th scope="col">讲次与节</th>
              <th scope="col">知识点</th>
              <th scope="col">已评估人数</th>
              <th scope="col">达标比例</th>
              <th scope="col">有已确认错因的人数</th>
            </tr>
          </thead>
          <tbody>
            {data.map(({ cell }) => (
              <tr key={cell.knowledge_point_id}>
                <td>
                  {lectureLabel(cell.lecture)} 第 {cell.section} 节
                </td>
                <th scope="row">{cell.name}</th>
                <td>{cell.assessed}</td>
                <td>
                  {cell.assessed > 0
                    ? `${Math.round(cell.mastered_ratio * 100)}%`
                    : '未评估'}
                </td>
                <td>{cell.misconception_students}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

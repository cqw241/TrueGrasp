import { useQueryClient } from '@tanstack/react-query'
import { useMemo } from 'react'
import { api } from '@/api/client'
import type { paths } from '@/api/schema'

/** 让若干 GET 接口的查询失效并重新拉取；按路径前缀匹配，忽略查询参数。 */
export function useInvalidate() {
  const queryClient = useQueryClient()
  return (...paths: (keyof paths)[]) => {
    for (const path of paths) {
      void queryClient.invalidateQueries({ queryKey: ['get', path] })
    }
  }
}

/** 学生 ID → 姓名；名单未加载时退回显示 ID。 */
export function useStudentNames() {
  const students = api.useQuery('get', '/api/students')
  return useMemo(() => {
    const names = new Map(students.data?.map((s) => [s.id, s.name]))
    return (id: string | null | undefined) => (id ? (names.get(id) ?? id) : '')
  }, [students.data])
}

/** 知识点 ID → 名称（附讲次与节）。 */
export function usePointNames() {
  const points = api.useQuery('get', '/api/knowledge/points')
  return useMemo(() => {
    const names = new Map(points.data?.map((p) => [p.id, p.name]))
    return (id: string) =>
      names.get(id) ?? (id === 'pending.unclassified' ? '待归类' : id)
  }, [points.data])
}

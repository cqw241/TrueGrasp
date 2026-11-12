import type { UseQueryResult } from '@tanstack/react-query'
import {
  AlertCircleIcon,
  Construction,
  Inbox,
  RefreshCw,
  type LucideIcon,
} from 'lucide-react'
import type { ReactNode } from 'react'
import { ApiError } from '@/api/client'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'

type EmptyStateProps = {
  title: string
  description?: ReactNode
  icon?: LucideIcon
  /** 引导下一步的操作，如“导入作业”按钮。 */
  action?: ReactNode
  className?: string
}

export function EmptyState({
  title,
  description,
  icon: Icon = Inbox,
  action,
  className,
}: EmptyStateProps) {
  return (
    <div
      data-slot="empty-state"
      className={cn(
        'flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed px-6 py-10 text-center',
        className,
      )}
    >
      <Icon className="text-muted-foreground size-8" aria-hidden />
      <p className="font-medium">{title}</p>
      {description && (
        <p className="text-muted-foreground max-w-sm text-sm">{description}</p>
      )}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}

type ErrorStateProps = {
  error: unknown
  title?: string
  onRetry?: () => void
  className?: string
}

/** 请求失败的提示。后端返回 501（接口尚未实现）时显示为“功能开发中”，不当作错误。 */
export function ErrorState({
  error,
  title = '加载失败',
  onRetry,
  className,
}: ErrorStateProps) {
  if (error instanceof ApiError && error.status === 501) {
    return (
      <EmptyState
        icon={Construction}
        title="功能开发中"
        description={error.message}
        className={className}
      />
    )
  }
  const message =
    error instanceof Error ? error.message : '发生未知错误，请稍后重试'
  return (
    <Alert variant="destructive" className={className}>
      <AlertCircleIcon />
      <AlertTitle>{title}</AlertTitle>
      <AlertDescription>
        <p>{message}</p>
        {onRetry && (
          <Button variant="outline" size="sm" onClick={onRetry}>
            <RefreshCw data-icon="inline-start" />
            重试
          </Button>
        )}
      </AlertDescription>
    </Alert>
  )
}

export function LoadingState({
  rows = 3,
  className,
}: {
  rows?: number
  className?: string
}) {
  return (
    <div className={cn('space-y-2', className)} aria-busy aria-label="加载中">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-6 w-full" />
      ))}
    </div>
  )
}

type QueryViewProps<T> = {
  // 错误类型写 unknown：openapi-react-query 按接口声明推断为 422 错误体，
  // 实际抛出的总是 api/client.ts 中间件构造的 ApiError。
  query: UseQueryResult<T, unknown>
  /** 判断数据是否为空，默认空数组为空。 */
  isEmpty?: (data: T) => boolean
  empty?: ReactNode
  loading?: ReactNode
  children: (data: T) => ReactNode
}

/** 统一处理一个查询的加载、出错、无数据三种状态，有数据时渲染 children。 */
export function QueryView<T>({
  query,
  isEmpty = (data) => Array.isArray(data) && data.length === 0,
  empty = <EmptyState title="暂无数据" />,
  loading = <LoadingState />,
  children,
}: QueryViewProps<T>) {
  if (query.isPending) return loading
  if (query.isError) {
    return (
      <ErrorState error={query.error} onRetry={() => void query.refetch()} />
    )
  }
  if (isEmpty(query.data)) return empty
  return children(query.data)
}

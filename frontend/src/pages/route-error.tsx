import { FileQuestion } from 'lucide-react'
import { isRouteErrorResponse, Link, useRouteError } from 'react-router'
import { EmptyState, ErrorState } from '@/components/states'
import { Button } from '@/components/ui/button'

export function NotFound() {
  return (
    <EmptyState
      icon={FileQuestion}
      title="页面不存在"
      action={
        <Button asChild variant="outline">
          <Link to="/">返回首页</Link>
        </Button>
      }
    />
  )
}

/** 页面渲染出错或拆分的代码包加载失败（如部署了新版本）时显示，替代 React Router 的默认错误页。 */
export function RouteError() {
  const error = useRouteError()
  if (isRouteErrorResponse(error) && error.status === 404) return <NotFound />
  return (
    <div className="p-4 md:p-8">
      <ErrorState
        title="页面出错了"
        error={
          error instanceof Error ? error : new Error('页面加载失败，请刷新重试')
        }
        onRetry={() => window.location.reload()}
      />
    </div>
  )
}

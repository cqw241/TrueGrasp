import { ApiError } from '@/api/client'

/** 写操作失败的提示，接口尚未实现（501）时说明由哪个任务实现。 */
export function MutationError({ error }: { error: unknown }) {
  if (!error) return null
  const message =
    error instanceof ApiError && error.status === 501
      ? `后端接口开发中：${error.message}`
      : error instanceof Error
        ? error.message
        : '操作失败，请稍后重试'
  return (
    <p role="alert" className="text-destructive text-sm">
      {message}
    </p>
  )
}

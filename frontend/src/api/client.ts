import { QueryClient } from '@tanstack/react-query'
import createFetchClient, { type Middleware } from 'openapi-fetch'
import createQueryHooks from 'openapi-react-query'
import type { paths } from './schema'

/** 后端返回非 2xx，或请求没有到达后端。status 为 0 表示网络错误。 */
export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

// FastAPI 的错误体：HTTPException 为 {detail: string}，请求校验失败为 {detail: [{msg}]}。
function detailOf(body: unknown): string | null {
  if (typeof body !== 'object' || body === null || !('detail' in body)) {
    return null
  }
  const { detail } = body
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((d: { msg?: unknown }) => (typeof d.msg === 'string' ? d.msg : ''))
      .filter(Boolean)
      .join('；')
  }
  return null
}

const throwOnError: Middleware = {
  async onResponse({ response }) {
    if (response.ok) return
    const body: unknown = await response
      .clone()
      .json()
      .catch(() => null)
    throw new ApiError(
      response.status,
      detailOf(body) ??
        (response.status >= 502 && response.status <= 504
          ? '后端暂时不可用，请稍后重试'
          : `请求失败（HTTP ${response.status}）`),
    )
  },
  // 只在 fetch 本身失败时调用；取消请求的 AbortError 原样抛出，交给 React Query 处理。
  onError({ error }) {
    if (error instanceof DOMException && error.name === 'AbortError') return
    return new ApiError(0, '无法连接后端，请检查网络或稍后重试')
  },
}

export const fetchClient = createFetchClient<paths>()
fetchClient.use(throwOnError)

/** 类型化的 React Query hooks：api.useQuery('get', '/api/clock') 等。 */
export const api = createQueryHooks(fetchClient)

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // 只重试网络错误和网关类错误；4xx、500、501（接口尚未实现）重试也不会成功。
      retry: (failureCount, error) =>
        failureCount < 2 &&
        error instanceof ApiError &&
        (error.status === 0 || error.status >= 502),
      refetchOnWindowFocus: false,
    },
  },
})

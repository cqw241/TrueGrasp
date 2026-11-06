import { createBrowserRouter, Navigate } from 'react-router'
import { AppLayout } from '@/components/layout/app-layout'
import { LoadingState } from '@/components/states'
import { NotFound, RouteError } from '@/pages/route-error'

// 页面按路由拆包：MathLive、ECharts 等较大的依赖只在用到的页面加载。
export const router = createBrowserRouter([
  {
    path: '/',
    Component: AppLayout,
    ErrorBoundary: RouteError,
    HydrateFallback: () => <LoadingState className="p-8" />,
    children: [
      { index: true, element: <Navigate to="/teacher" replace /> },
      {
        path: 'teacher',
        lazy: async () => ({
          Component: (await import('@/pages/teacher-cockpit')).default,
        }),
      },
      {
        path: 'student',
        lazy: async () => ({
          Component: (await import('@/pages/student-desk')).default,
        }),
      },
      {
        path: 'decision',
        lazy: async () => ({
          Component: (await import('@/pages/decision-view')).default,
        }),
      },
      { path: '*', Component: NotFound },
    ],
  },
])

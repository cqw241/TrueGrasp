import { createBrowserRouter, Navigate, Outlet } from 'react-router'
import { useAuth } from '@/auth'
import { AppLayout } from '@/components/layout/app-layout'
import { LoadingState } from '@/components/states'
import { NotFound, RouteError } from '@/pages/route-error'
import Login from '@/pages/login'

function Guard() {
  const { user, loading } = useAuth()
  if (loading) return <LoadingState className="p-8" />
  if (!user) return <Navigate to="/login" replace />
  return <Outlet />
}

function TeacherOnly() {
  const { user } = useAuth()
  return user?.role === 'teacher' ? (
    <Outlet />
  ) : (
    <Navigate to="/student" replace />
  )
}

function Home() {
  const { user } = useAuth()
  return (
    <Navigate to={user?.role === 'student' ? '/student' : '/teacher'} replace />
  )
}

// 页面按路由拆包：MathLive、ECharts 等较大的依赖只在用到的页面加载。
export const router = createBrowserRouter([
  { path: '/login', Component: Login },
  {
    path: '/',
    Component: Guard,
    ErrorBoundary: RouteError,
    HydrateFallback: () => <LoadingState className="p-8" />,
    children: [
      {
        Component: AppLayout,
        children: [
          { index: true, Component: Home },
          {
            Component: TeacherOnly,
            children: [
              {
                path: 'teacher',
                lazy: async () => ({
                  Component: (await import('@/pages/teacher-cockpit')).default,
                }),
              },
              {
                path: 'decision',
                lazy: async () => ({
                  Component: (await import('@/pages/decision-view')).default,
                }),
              },
            ],
          },
          {
            path: 'student',
            lazy: async () => ({
              Component: (await import('@/pages/student-desk')).default,
            }),
          },
          { path: '*', Component: NotFound },
        ],
      },
    ],
  },
])

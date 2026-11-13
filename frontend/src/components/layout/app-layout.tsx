import {
  GraduationCap,
  LayoutDashboard,
  Menu,
  Network,
  BookOpen,
  LogOut,
  type LucideIcon,
} from 'lucide-react'
import { Suspense, useState, type ReactNode } from 'react'
import { NavLink, Outlet } from 'react-router'
import { LoadingState } from '@/components/states'
import { Button } from '@/components/ui/button'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet'
import { useStreamStatus } from '@/hooks/use-event-stream'
import { cn } from '@/lib/utils'
import { useAuth } from '@/auth'

const NAV: { to: string; label: string; icon: LucideIcon }[] = [
  { to: '/teacher', label: '教师驾驶舱', icon: LayoutDashboard },
  { to: '/student', label: '学生辅导台', icon: GraduationCap },
  { to: '/decision', label: '决策证据视图', icon: Network },
]

function Brand() {
  return (
    <div className="brand">
      <span className="brand-symbol">
        <BookOpen size={21} aria-hidden />
      </span>
      <div>
        <span className="brand-name">真懂 AI</span>
        <span className="brand-caption">高等数学自主教学</span>
      </div>
    </div>
  )
}

function NavItems({ onNavigate }: { onNavigate?: () => void }) {
  const { user } = useAuth()
  return (
    <nav className="workspace-nav" aria-label="主要导航">
      {NAV.filter(
        ({ to }) => user?.role === 'teacher' || to === '/student',
      ).map(({ to, label, icon: Icon }) => (
        <NavLink
          key={to}
          to={to}
          onClick={onNavigate}
          className={({ isActive }) =>
            cn(
              'nav-item flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors',
              isActive
                ? 'bg-secondary text-secondary-foreground font-medium'
                : 'text-muted-foreground hover:bg-muted hover:text-foreground',
            )
          }
        >
          <Icon className="size-4" aria-hidden />
          {label}
        </NavLink>
      ))}
    </nav>
  )
}

const STREAM_TEXT = {
  open: '实时连接正常',
  connecting: '正在连接…',
  closed: '实时连接已断开，正在重试',
} as const

function StreamIndicator({ compact = false }: { compact?: boolean }) {
  const status = useStreamStatus()
  return (
    <div
      className="text-muted-foreground flex items-center gap-2 text-xs"
      title={STREAM_TEXT[status]}
      role="status"
    >
      <span
        className={cn(
          'size-2 rounded-full',
          status === 'open' && 'bg-emerald-500',
          status === 'connecting' && 'animate-pulse bg-amber-500',
          status === 'closed' && 'bg-destructive',
        )}
        aria-hidden
      />
      <span className={cn(compact && 'sr-only')}>{STREAM_TEXT[status]}</span>
    </div>
  )
}

/**
 * 桌面横向导航；平板与手机用抽屉导航。教师模块可在总览和展开视图间切换。
 */
export function AppLayout() {
  const [menuOpen, setMenuOpen] = useState(false)
  const { user, logout } = useAuth()
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <div className="flex min-w-0 flex-col">
        <header className="app-topbar">
          <div className="lg:hidden">
            <Sheet open={menuOpen} onOpenChange={setMenuOpen}>
              <SheetTrigger asChild>
                <Button variant="ghost" size="icon" aria-label="打开导航">
                  <Menu />
                </Button>
              </SheetTrigger>
              <SheetContent side="left" className="w-64 gap-6 p-4">
                <SheetTitle>
                  <Brand />
                </SheetTitle>
                <SheetDescription className="sr-only">
                  页面导航
                </SheetDescription>
                <NavItems onNavigate={() => setMenuOpen(false)} />
              </SheetContent>
            </Sheet>
          </div>
          <Brand />
          <div className="desktop-nav">
            <NavItems />
          </div>
          <div className="topbar-account">
            <div>
              <span className="hidden xl:block">
                <StreamIndicator />
              </span>
              <span className="xl:hidden">
                <StreamIndicator compact />
              </span>
            </div>
            <span className="account-name">
              {user?.role === 'teacher' ? '教师' : '学生'}
              <span className="hidden sm:inline"> / {user?.username}</span>
            </span>
            <Button
              variant="ghost"
              size="icon"
              aria-label="退出登录"
              title="退出登录"
              onClick={() => void logout()}
            >
              <LogOut />
            </Button>
          </div>
        </header>

        <main id="main-content" tabIndex={-1} className="app-main">
          <Suspense fallback={<LoadingState />}>
            <Outlet />
          </Suspense>
        </main>
      </div>
    </div>
  )
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string
  description?: string
  actions?: ReactNode
}) {
  return (
    <div className="page-header">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {description && (
          <p className="text-muted-foreground mt-1 text-sm">{description}</p>
        )}
      </div>
      {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
    </div>
  )
}

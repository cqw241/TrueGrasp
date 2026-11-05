import {
  GraduationCap,
  LayoutDashboard,
  Menu,
  Network,
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

const NAV: { to: string; label: string; icon: LucideIcon }[] = [
  { to: '/teacher', label: '教师驾驶舱', icon: LayoutDashboard },
  { to: '/student', label: '学生辅导台', icon: GraduationCap },
  { to: '/decision', label: '决策证据视图', icon: Network },
]

function Brand() {
  return (
    <div className="flex items-baseline gap-2">
      <span className="text-lg font-semibold">真懂 AI</span>
      <span className="text-muted-foreground hidden text-xs sm:inline">
        高等数学自主教学
      </span>
    </div>
  )
}

function NavItems({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav className="flex flex-col gap-1">
      {NAV.map(({ to, label, icon: Icon }) => (
        <NavLink
          key={to}
          to={to}
          onClick={onNavigate}
          className={({ isActive }) =>
            cn(
              'flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors',
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
 * 三档屏幕：桌面（≥1024px）固定侧边栏；平板与手机顶部栏 + 抽屉导航。
 * 内容区边距随宽度收窄，页面自身的栅格用 md:/lg: 断点适配。
 */
export function AppLayout() {
  const [menuOpen, setMenuOpen] = useState(false)
  return (
    <div className="bg-background min-h-svh lg:grid lg:grid-cols-[15rem_minmax(0,1fr)]">
      <aside className="sticky top-0 hidden h-svh flex-col gap-6 border-r p-4 lg:flex">
        <Brand />
        <NavItems />
        <div className="mt-auto">
          <StreamIndicator />
        </div>
      </aside>

      <div className="flex min-w-0 flex-col">
        <header className="bg-background/95 sticky top-0 z-40 flex h-14 items-center gap-2 border-b px-3 backdrop-blur md:px-6 lg:hidden">
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
              <SheetDescription className="sr-only">页面导航</SheetDescription>
              <NavItems onNavigate={() => setMenuOpen(false)} />
            </SheetContent>
          </Sheet>
          <Brand />
          <div className="ml-auto">
            <StreamIndicator compact />
          </div>
        </header>

        <main className="flex-1 p-4 md:p-6 lg:p-8">
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
    <div className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
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

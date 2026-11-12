import { Maximize2, Minimize2 } from 'lucide-react'
import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from 'react'
import { cn } from '@/lib/utils'

const WorkspaceContext = createContext<{
  expanded: string | null
  expand: Dispatch<SetStateAction<string | null>>
}>({ expanded: null, expand: () => {} })

/** Expand in place so forms, query subscriptions and chart instances stay mounted. */
export function Workspace({
  children,
  className,
}: {
  children: ReactNode
  className?: string
}) {
  const [expanded, expand] = useState<string | null>(null)
  return (
    <WorkspaceContext value={{ expanded, expand }}>
      <div
        className={cn(
          'teaching-workspace',
          className,
          expanded && 'workspace-focused',
        )}
      >
        {children}
      </div>
    </WorkspaceContext>
  )
}

export function WorkspaceModule({
  id,
  title,
  children,
  className,
}: {
  id: string
  title: string
  children: ReactNode
  className?: string
}) {
  const { expanded, expand } = useContext(WorkspaceContext)
  const focused = expanded === id
  const trigger = useRef<HTMLButtonElement>(null)
  // A conditional module (for example the optional session) can disappear
  // while expanded. Restore the remaining overview instead of hiding it.
  useEffect(
    () => () => expand((current) => (current === id ? null : current)),
    [expand, id],
  )
  useEffect(() => {
    if (!focused) return
    function escape(event: KeyboardEvent) {
      // Let a nested sheet, select or formula editor handle its own Escape first.
      if (
        event.key !== 'Escape' ||
        event.defaultPrevented ||
        event.target instanceof HTMLSelectElement ||
        document.querySelector(
          '[role="dialog"], [role="listbox"], math-field:focus-within',
        )
      )
        return
      expand(null)
      trigger.current?.focus()
    }
    window.addEventListener('keydown', escape)
    return () => window.removeEventListener('keydown', escape)
  }, [focused, expand])
  return (
    <section
      hidden={expanded !== null && !focused}
      className={cn('workspace-module', focused && 'module-focused', className)}
      aria-labelledby={`${id}-heading`}
    >
      <button
        ref={trigger}
        type="button"
        className="module-heading"
        aria-expanded={focused}
        aria-controls={`${id}-body`}
        onClick={() => expand(focused ? null : id)}
        title={focused ? '返回总览（Esc）' : `展开${title}`}
      >
        <span id={`${id}-heading`}>{title}</span>
        <span className="module-expand-label">
          {focused ? '返回总览' : '展开'}
        </span>
        {focused ? <Minimize2 aria-hidden /> : <Maximize2 aria-hidden />}
      </button>
      <div id={`${id}-body`} className="module-body">
        {children}
      </div>
    </section>
  )
}

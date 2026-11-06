import { MathfieldElement } from 'mathlive'
import { useEffect, useEffectEvent, useLayoutEffect, useRef } from 'react'
import { flushSync } from 'react-dom'
import { cn } from '@/lib/utils'

// 字体由 katex.min.css 提供（与 Tex 组件共用），MathLive 不再自行加载字体和按键音效。
MathfieldElement.fontsDirectory = null
MathfieldElement.soundsDirectory = null

type MathInputProps = {
  /** LaTeX。受控组件：输入框始终显示 value，父组件不接受某次输入时，输入框回到 value。 */
  value: string
  onChange: (latex: string) => void
  /** 学生在输入框里按回车。 */
  onSubmit?: (latex: string) => void
  disabled?: boolean
  placeholder?: string
  className?: string
  'aria-label'?: string
}

/** MathLive 公式输入框。触屏设备上聚焦时弹出虚拟数学键盘。 */
export function MathInput({
  value,
  onChange,
  onSubmit,
  disabled = false,
  placeholder,
  className,
  'aria-label': ariaLabel = '公式输入',
}: MathInputProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const fieldRef = useRef<MathfieldElement | null>(null)
  const valueRef = useRef(value)

  // 让输入框显示 valueRef。只在两者不同时写入，避免打断光标位置。
  const syncField = () => {
    const field = fieldRef.current
    if (field && field.value !== valueRef.current) {
      field.setValue(valueRef.current, { silenceNotifications: true })
    }
  }

  useLayoutEffect(() => {
    valueRef.current = value
    syncField()
  })

  const handleInput = useEffectEvent((event: Event) => {
    const field = event.target as MathfieldElement
    if ((event as InputEvent).inputType === 'insertLineBreak') {
      onSubmit?.(field.value)
      return
    }
    // 同步提交父组件的更新，再按最新的 value 校正：父组件拒绝或改写输入时，
    // 它可能不重新渲染，useLayoutEffect 不会执行，只能在这里把输入框改回去。
    flushSync(() => onChange(field.value))
    syncField()
  })

  // 自定义元素用命令式创建：React 不负责它的子节点和属性同步。
  useEffect(() => {
    const field = new MathfieldElement()
    field.mathVirtualKeyboardPolicy = 'auto'
    field.smartMode = false
    field.className =
      'block w-full border-0 bg-transparent text-lg outline-none'
    const listener = (event: Event) => handleInput(event)
    field.addEventListener('input', listener)
    containerRef.current!.append(field)
    fieldRef.current = field
    field.setValue(valueRef.current, { silenceNotifications: true })
    return () => {
      field.removeEventListener('input', listener)
      field.remove()
      fieldRef.current = null
    }
  }, [])

  useEffect(() => {
    const field = fieldRef.current
    if (!field) return
    field.disabled = disabled
    field.setAttribute('aria-label', ariaLabel)
    if (placeholder !== undefined) field.placeholder = placeholder
  }, [disabled, placeholder, ariaLabel])

  return (
    <div
      ref={containerRef}
      className={cn(
        'border-input focus-within:border-ring focus-within:ring-ring/50 min-h-11 rounded-md border px-3 py-1.5 shadow-xs transition-[color,box-shadow] focus-within:ring-[3px]',
        disabled && 'pointer-events-none opacity-50',
        className,
      )}
    />
  )
}

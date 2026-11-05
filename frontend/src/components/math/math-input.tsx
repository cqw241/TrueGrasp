import { MathfieldElement } from 'mathlive'
import { useEffect, useEffectEvent, useRef } from 'react'
import { cn } from '@/lib/utils'

// 字体由 katex.min.css 提供（与 Tex 组件共用），MathLive 不再自行加载字体和按键音效。
MathfieldElement.fontsDirectory = null
MathfieldElement.soundsDirectory = null

type MathInputProps = {
  /** LaTeX。受控组件，父组件清空 value 时输入框同步清空。 */
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

  const handleInput = useEffectEvent((event: Event) => {
    const field = event.target as MathfieldElement
    if ((event as InputEvent).inputType === 'insertLineBreak') {
      onSubmit?.(field.value)
    } else {
      onChange(field.value)
    }
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
    return () => {
      field.removeEventListener('input', listener)
      field.remove()
      fieldRef.current = null
    }
  }, [])

  useEffect(() => {
    const field = fieldRef.current
    if (field && field.value !== value) {
      field.setValue(value, { silenceNotifications: true })
    }
  }, [value])

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

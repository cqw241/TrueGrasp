import katex from 'katex'
import 'katex/dist/katex.min.css'
import { Fragment, useMemo } from 'react'
import { cn } from '@/lib/utils'

type TexProps = {
  /** LaTeX 源码，不带 $ 定界符。 */
  latex: string
  /** 行间公式（居中、独占一行）；默认为行内公式。 */
  display?: boolean
  className?: string
}

/** 渲染一段 LaTeX。语法错误时显示红色源码，不抛异常。 */
export function Tex({ latex, display = false, className }: TexProps) {
  const html = useMemo(
    () =>
      katex.renderToString(latex, {
        displayMode: display,
        throwOnError: false,
        // 题干里的 \text{中文} 会触发 unicode 警告，对渲染无影响。
        strict: 'ignore',
        output: 'htmlAndMathml',
      }),
    [latex, display],
  )
  const Tag = display ? 'div' : 'span'
  return (
    <Tag
      className={cn(display && 'my-2 overflow-x-auto', className)}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}

// $$…$$、\[…\]、$…$、\(…\)；\$ 视为普通美元符号。
const DELIMITED =
  /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|(?<!\\)\$((?:\\\$|[^$])+?)\$|\\\(([\s\S]+?)\\\)/g

type Segment =
  | { kind: 'text'; text: string }
  | { kind: 'math'; latex: string; display: boolean }

function splitMath(source: string): Segment[] {
  const segments: Segment[] = []
  let last = 0
  for (const match of source.matchAll(DELIMITED)) {
    if (match.index > last) {
      segments.push({ kind: 'text', text: source.slice(last, match.index) })
    }
    const [, displayDollar, displayBracket, inlineDollar, inlineParen] = match
    const display = displayDollar ?? displayBracket
    segments.push(
      display !== undefined
        ? { kind: 'math', latex: display, display: true }
        : { kind: 'math', latex: inlineDollar ?? inlineParen, display: false },
    )
    last = match.index + match[0].length
  }
  if (last < source.length) {
    segments.push({ kind: 'text', text: source.slice(last) })
  }
  return segments
}

/** 渲染夹杂公式的文字，如题干“求 $\sin^2(3x)$ 的导数”和辅导消息。保留换行。 */
export function MathText({
  text,
  className,
}: {
  text: string
  className?: string
}) {
  const segments = useMemo(() => splitMath(text), [text])
  return (
    <span className={cn('whitespace-pre-wrap', className)}>
      {segments.map((segment, i) =>
        segment.kind === 'text' ? (
          <Fragment key={i}>{segment.text.replaceAll('\\$', '$')}</Fragment>
        ) : (
          <Tex key={i} latex={segment.latex} display={segment.display} />
        ),
      )}
    </span>
  )
}

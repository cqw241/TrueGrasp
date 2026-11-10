import { MathText, Tex } from '@/components/math/tex'

/** 作答可以是文字或公式，与学生会话保持相同的展示方式。 */
export function Answer({ value }: { value: string }) {
  return /[㐀-鿿$]/.test(value) ? (
    <MathText text={value} />
  ) : (
    <Tex latex={value} />
  )
}

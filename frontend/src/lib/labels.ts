import type {
  Dimension,
  HypothesisStatus,
  JudgedBy,
  MasteryState,
  TaskStatus,
  TaskType,
  TimelineKind,
} from '@/api/schema'

// 后端枚举值为英文（backend/app/contracts/enums.py），界面名称统一在这里映射。
// 用 Record 声明：后端新增枚举值、重新生成类型后，这里会编译失败，提醒补上名称。

export const masteryStateLabel: Record<MasteryState, string> = {
  needs_intervention: '待干预',
  tutoring: '辅导中',
  initial: '初步掌握',
  consolidated: '巩固',
  integrated: '融会贯通',
  relapsed: '错因复现',
  escalated: '转交教师',
}

export const dimensionLabel: Record<Dimension, string> = {
  correctness: '正确性',
  distractor: '抗诱错',
  near_transfer: '近迁移',
  explanation: '解释',
  retention: '保持',
  cross_chapter: '跨章节应用',
}

export const taskTypeLabel: Record<TaskType, string> = {
  tutoring: '辅导',
  verification: '验证',
  retest: '复测',
  precheck: '章前预检',
  escalation: '转交教师',
  class_review: '课上讲解建议',
}

export const taskStatusLabel: Record<TaskStatus, string> = {
  pending: '待开始',
  active: '进行中',
  done: '已完成',
  cancelled: '已取消',
}

export const hypothesisStatusLabel: Record<HypothesisStatus, string> = {
  pending: '待检验',
  supported: '支持',
  refuted: '排除',
}

export const judgedByLabel: Record<JudgedBy, string> = {
  tool: '工具核验',
  model: '模型评阅',
  teacher: '教师处理',
}

export const timelineKindLabel: Record<TimelineKind, string> = {
  diagnosis: '诊断',
  intervention: '干预',
  verification: '验证',
  retest: '复测',
  backtrack: '回溯',
  escalation: '转交教师',
  precheck: '章前预检',
}

const LECTURE_NUMERALS = '一二三四五六七八九十'

/** 1 → “第一讲”，12 → “第十二讲”。 */
export function lectureLabel(lecture: number): string {
  const tens = Math.floor(lecture / 10)
  const ones = lecture % 10
  const numeral =
    (tens > 1 ? LECTURE_NUMERALS[tens - 1] : '') +
    (tens > 0 ? '十' : '') +
    (ones > 0 ? LECTURE_NUMERALS[ones - 1] : '')
  return `第${numeral}讲`
}

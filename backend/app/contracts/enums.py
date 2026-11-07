"""全系统共用的枚举。值用英文，界面上的中文名称由前端映射。"""

from enum import StrEnum


class ExamTag(StrEnum):
    """考研数学一/二/三适用范围，只作背景提醒。"""

    MATH1 = "math1"
    MATH2 = "math2"
    MATH3 = "math3"


class ContentSource(StrEnum):
    """错因、策略卡的来源。"""

    TEACHER = "teacher"  # 教师编写
    UNMODELED = "unmodeled"  # 辅导中由模型提出的未建模错因，待教师审核


class ItemSource(StrEnum):
    SEED = "seed"  # 教师编写的种子题
    GENERATED = "generated"  # 模型生成并通过出题质量门槛
    HOMEWORK = "homework"  # 随作业导入


class ItemPurpose(StrEnum):
    HOMEWORK = "homework"
    PROBE = "probe"  # 诊断中的判别追问题
    PRACTICE = "practice"  # 干预中的练习
    VERIFICATION = "verification"  # 独立验证、复测、章前预检


class Dimension(StrEnum):
    """独立验证的六个考查维度（技术文档第 5 节 ④）。"""

    CORRECTNESS = "correctness"  # 正确性
    DISTRACTOR = "distractor"  # 抗诱错
    NEAR_TRANSFER = "near_transfer"  # 近迁移
    EXPLANATION = "explanation"  # 解释
    RETENTION = "retention"  # 保持
    CROSS_CHAPTER = "cross_chapter"  # 跨章节应用


class EvidenceKind(StrEnum):
    HOMEWORK = "homework"  # 作业中的作答
    DIAGNOSIS = "diagnosis"  # 判别追问题的作答
    PRACTICE = "practice"  # 干预练习的作答
    VERIFICATION = "verification"  # 独立验证、复测、预检的作答
    TEACHER = "teacher"  # 教师处理记录


class JudgedBy(StrEnum):
    TOOL = "tool"  # 工具核验
    MODEL = "model"  # 模型评阅
    TEACHER = "teacher"  # 教师处理


class HypothesisStatus(StrEnum):
    PENDING = "pending"  # 待检验
    SUPPORTED = "supported"  # 支持
    REFUTED = "refuted"  # 排除


class MasteryState(StrEnum):
    """掌握状态（技术文档第 5 节 ④），只由程序按证据迁移。"""

    NEEDS_INTERVENTION = "needs_intervention"  # 待干预
    TUTORING = "tutoring"  # 辅导中
    INITIAL = "initial"  # 初步掌握
    CONSOLIDATED = "consolidated"  # 巩固
    INTEGRATED = "integrated"  # 融会贯通
    RELAPSED = "relapsed"  # 错因复现
    ESCALATED = "escalated"  # 转交教师


class TaskType(StrEnum):
    TUTORING = "tutoring"  # 辅导
    VERIFICATION = "verification"  # 验证
    RETEST = "retest"  # 复测
    PRECHECK = "precheck"  # 章前预检
    ESCALATION = "escalation"  # 转教师
    CLASS_REVIEW = "class_review"  # 建议课上集中讲解；student_id 为空
    QUESTION = "question"  # 学生主动提问（技术文档第 5 节 ⑥）


class TaskStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    CANCELLED = "cancelled"


class EventType(StrEnum):
    HOMEWORK_IMPORTED = "homework_imported"  # 作业导入
    ANSWER_SUBMITTED = "answer_submitted"  # 作答
    STUDENT_QUESTION = "student_question"  # 学生提问
    TASK_DUE = "task_due"  # 复测、预检等任务到期
    LECTURE_ENTERED = "lecture_entered"  # 进入新章节
    TEACHER_INSTRUCTION = "teacher_instruction"  # 教师指令


class TutorOutcome(StrEnum):
    """辅导 Agent 一轮结束后，告诉编排层下一步做什么。"""

    AWAIT_STUDENT = "await_student"  # 等学生作答或回复
    REQUEST_VERIFICATION = "request_verification"  # 申请独立验证
    REQUEST_REPLAN = "request_replan"  # 3 轮仍无法确诊、连续无进展等，请规划 Agent 重新规划


class TimelineKind(StrEnum):
    """决策证据视图中学生学习时间线的条目类型。"""

    DIAGNOSIS = "diagnosis"
    INTERVENTION = "intervention"
    VERIFICATION = "verification"
    RETEST = "retest"
    BACKTRACK = "backtrack"
    ESCALATION = "escalation"
    PRECHECK = "precheck"


class Operation(StrEnum):
    """题目结构化描述 ItemSpec 的运算类型，决定 verify_item 如何重算。"""

    EXPRESSION = "expression"  # 化简或求值一个表达式（如函数复合）
    DERIVATIVE = "derivative"  # 求导或求偏导
    INDEFINITE_INTEGRAL = "indefinite_integral"
    DEFINITE_INTEGRAL = "definite_integral"
    DOUBLE_INTEGRAL = "double_integral"
    LIMIT = "limit"


class EquivalenceMode(StrEnum):
    EXPR = "expr"  # 符号化简后差为 0，失败时数值抽样
    ANTIDERIVATIVE = "antiderivative"  # 求导后比较，忽略常数 C
    VALUE = "value"  # 数值比较

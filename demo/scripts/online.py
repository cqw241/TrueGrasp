"""Drive a running TrueGrasp instance through its existing HTTP API."""

import csv
import io
import time
from datetime import date

import httpx

from app.contracts.api import AnswerTarget, ClockState
from app.contracts.entities import Item, Task
from app.contracts.enums import TaskStatus, TaskType

from ..dataset import Answer, Dataset

STAGES = {
    "diagnosis_ch2": TaskType.TUTORING,
    "verification": TaskType.VERIFICATION,
    "retest_friday": TaskType.RETEST,
    "precheck_ch9": TaskType.PRECHECK,
    "ch9_followup": TaskType.VERIFICATION,
}


class DemoError(RuntimeError):
    pass


class DemoAPI:
    def __init__(self, client: httpx.Client, wait: float):
        self.client = client
        self.wait = wait

    def request(self, method: str, route: str, **kwargs):
        response = self.client.request(method, f"/api{route}", **kwargs)
        if not response.is_success:
            raise DemoError(f"{method} /api{route}: HTTP {response.status_code} {response.text}")
        return response.json()

    def clock(self) -> ClockState:
        return ClockState.model_validate(self.request("GET", "/clock"))

    def advance(self, target: date):
        now = self.clock()
        days = (target - now.teaching_date).days
        if days < 0:
            raise DemoError("教学日期不能倒退；请恢复到更早的快照")
        return self.request("POST", "/clock/advance", json={"days": days}) if days else now

    def goal(self, plan: Dataset):
        payload = plan.export()["goal"]
        goals = self.request("GET", "/goals")
        for goal in goals:
            if all(goal.get(key) == value for key, value in payload.items()):
                return goal
        return self.request("POST", "/goals", json=payload)

    def import_homework(
        self, plan: Dataset, student_ids: list[str], *, live_answer: str | None = None
    ):
        now = self.clock()
        live = live_answer is not None
        stage = "homework_ch10_live" if live else "homework_ch2"
        rows = [
            a
            for a in plan.answers
            if a.stage == stage and (not student_ids or a.student_id in student_ids)
        ]
        if not rows or any(s not in {a.student_id for a in rows} for s in student_ids):
            raise DemoError("没有对应学生的作业输入")
        lecture = 10 if live else 2
        if now.lecture != lecture or now.teaching_date != rows[0].teaching_date:
            raise DemoError(f"请先进入第 {lecture} 讲、教学日期 {rows[0].teaching_date}")
        if not live and self.request("GET", "/students"):
            raise DemoError("第二讲导入需要新建的预跑库；已有学生，请检查上次导入结果")
        if live_answer is not None and not live_answer.strip():
            raise DemoError("请输入 D 在现场作出的初始作业答案")
        names = {s.id: s.name for s in plan.scenario.students}
        buf = io.StringIO(newline="")
        writer = csv.writer(buf)
        writer.writerow(
            (
                "student_id",
                "student_name",
                "item_id",
                "knowledge_point_id",
                "stem",
                "student_answer",
                "correct_answer",
            )
        )
        for row in rows:
            item = plan.items[row.item_id]
            writer.writerow(
                (
                    row.student_id,
                    names[row.student_id],
                    item.id,
                    item.knowledge_point_id,
                    item.stem,
                    live_answer if live else row.student_answer,
                    item.answer,
                )
            )
        return self.request(
            "POST",
            "/homework",
            files={"file": (f"{stage}.csv", buf.getvalue().encode(), "text/csv")},
            timeout=self.wait,
        )

    def wait_target(self, task_id: int, *, turn: int | None = None) -> AnswerTarget:
        deadline = time.monotonic() + self.wait
        while True:
            target = AnswerTarget.model_validate(
                self.request("GET", f"/tasks/{task_id}/answer-target")
            )
            if target.phase == "failed":
                raise DemoError(f"任务 {task_id} 执行失败；查看后端日志")
            if target.phase == "closed" or (
                target.phase in {"idle", "waiting"} and (turn is None or target.turn > turn)
            ):
                return target
            if time.monotonic() >= deadline:
                raise DemoError(f"等待任务 {task_id} 超时；先检查当前题和日志再重试")
            time.sleep(0.25)

    def task_for(self, rows: list[Answer], plan: Dataset, today: date) -> Task:
        row = rows[0]
        kind = STAGES[row.stage]
        point = plan.items[row.item_id].knowledge_point_id
        types = {kind}
        if kind == TaskType.VERIFICATION:
            # The tutor graph can hand off to the independent verifier within the same task.
            types.add(TaskType.TUTORING)
        tasks = [
            Task.model_validate(raw)
            for raw in self.request("GET", "/tasks", params={"student_id": row.student_id})
        ]
        matches = [
            t
            for t in tasks
            if t.type in types
            and (t.type == kind or t.status in (TaskStatus.ACTIVE, TaskStatus.DONE))
            and t.knowledge_point_id == point
            and t.round == row.round
            and t.status != TaskStatus.CANCELLED
            and (t.due_date is None or t.due_date <= today)
            and (kind != TaskType.PRECHECK or t.created_on == today)
        ]
        # Prefer an open task. Completed tasks are only used to verify previous submissions.
        open_tasks = [t for t in matches if t.status != TaskStatus.DONE]
        candidates = open_tasks or matches
        if len(candidates) != 1:
            raise DemoError(
                f"未找到唯一已到期待办：{kind} / {point} / 第 {row.round} 轮；"
                f"候选 ID：{[t.id for t in candidates]}"
            )
        return candidates[0]

    def _history(self, task_id: int) -> tuple[dict[str, Item], list[tuple[Item, str]]]:
        shown = {}
        answered = []
        current = None
        for message in self.request("GET", f"/tasks/{task_id}/messages"):
            if message["role"] == "assistant":
                if message.get("item"):
                    current = Item.model_validate(message["item"])
                    shown[current.id] = current
            elif current is not None:
                answered.append((current, message["content"]))
                current = None
        return shown, answered

    @staticmethod
    def _match(plan: Dataset, rows: list[Answer], item: Item):
        for row in rows:
            try:
                return row, plan.answer_for(row.key, item)
            except ValueError:
                continue
        return None

    def submit(self, plan: Dataset, rows: list[Answer], today: date, report) -> None:
        task = self.task_for(rows, plan, today)
        task_id = task.id
        target = self.wait_target(task_id)
        if target.phase == "idle":
            self.request("POST", f"/tasks/{task_id}/start")
            target = self.wait_target(task_id, turn=target.turn)
        remaining = list(rows)
        shown, answered = self._history(task_id)
        for item, content in answered:
            match = self._match(plan, remaining, item)
            if match and match[1].content == content:
                remaining.remove(match[0])
                report(
                    {
                        "key": match[0].key,
                        "status": "already_answered",
                        "task_id": task_id,
                        "item_id": item.id,
                    }
                )
        while remaining:
            item = shown.get(target.item_id)
            match = self._match(plan, remaining, item) if item else None
            if target.phase != "waiting" or not match:
                raise DemoError(
                    f"任务 {task_id} 当前 {target.phase}，实际题 {target.item_id} "
                    f"（{target.stem}）；待提交 {[a.key for a in remaining]}。"
                    "请检查证据，题目不一致时由操作者在辅导台作答"
                )
            row, message = match
            self.request(
                "POST",
                f"/tasks/{task_id}/messages",
                params={"expected_turn": target.turn},
                json=message.model_dump(mode="json"),
            )
            target = self.wait_target(task_id, turn=target.turn)
            shown, answered = self._history(task_id)
            if not any(
                i.id == message.answering_item_id and a == message.content for i, a in answered
            ):
                raise DemoError(f"任务 {task_id} 未读到已持久化的作答")
            remaining.remove(row)
            report(
                {
                    "key": row.key,
                    "status": "answered",
                    "task_id": task_id,
                    "item_id": message.answering_item_id,
                    "next_phase": target.phase,
                    "next_item_id": target.item_id,
                }
            )

    def batch(self, plan: Dataset, stage: str, student_ids: list[str], report) -> int:
        now = self.clock()
        rows = [
            row
            for row in plan.batch(stage)
            if row.teaching_date == now.teaching_date
            and (not student_ids or row.student_id in student_ids)
        ]
        if not rows or any(s not in {a.student_id for a in rows} for s in student_ids):
            raise DemoError(
                f"{now.teaching_date} 没有所选学生的脚本作答；A/B/C 后续及 D 现场作答必须人工操作"
            )
        if stage == "precheck_ch9" and now.lecture != 9:
            raise DemoError("请先进入第九讲，按教学时钟生成实际预检任务")
        if stage == "precheck_ch9" and not student_ids:
            assigned = {
                t.student_id
                for raw in self.request("GET", "/tasks", params={"type": TaskType.PRECHECK})
                if (t := Task.model_validate(raw)).created_on == now.teaching_date
                and t.status != TaskStatus.CANCELLED
                and any(
                    a.student_id == t.student_id
                    and a.round == t.round
                    and plan.items[a.item_id].knowledge_point_id == t.knowledge_point_id
                    for a in rows
                )
            }
            report({"stage": stage, "script_students_with_matching_precheck": sorted(assigned)})
            rows = [row for row in rows if row.student_id in assigned]
            if not rows:
                raise DemoError("当前没有匹配 T20 作答的第九讲预检任务，请查看实际任务列表")
        failures = 0
        for student in dict.fromkeys(row.student_id for row in rows):
            try:
                self.submit(
                    plan,
                    [row for row in rows if row.student_id == student],
                    now.teaching_date,
                    report,
                )
            except DemoError as exc:
                failures += 1
                report(
                    {"status": "blocked", "student_id": student, "stage": stage, "detail": str(exc)}
                )
        return failures

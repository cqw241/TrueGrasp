"""主图负责业务流转；子图只接收各自的输入，不共享对话通道。"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlmodel import Session, select

from app.contracts.agents import TutorTurnInput, VerifierInput
from app.contracts.entities import Item, Task
from app.contracts.enums import Dimension, ItemPurpose, ItemSource, TaskType
from app.orchestration.context import current_teaching_date
from app.orchestration.inputs import used_fingerprints
from app.orchestration.questions import question_turn
from app.orchestration.storage import QUESTION_FOLLOWUP, ItemExposure, VerificationExposureConflict
from app.verify.tools import SympyMathTools

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from app.orchestration.runtime import Orchestrator


class RunState(TypedDict, total=False):
    kind: str
    dispatch: bool
    event_id: int
    task_id: int
    teaching_date: str
    plan: dict[str, Any]
    plan_refreshes: int
    plan_stale: bool
    task_ids: list[int]
    phase: str
    messages: list[dict[str, Any]]
    shown_item_ids: list[str]
    awaiting_item_id: str | None
    incoming_message: dict[str, Any] | None
    tutor_turn: dict[str, Any]
    dimensions: list[str] | None
    context_knowledge_point_id: str | None
    verifier_input: dict[str, Any]
    verification_items: list[dict[str, Any]]
    stale_verification_item: bool
    item_index: int
    judgement: dict[str, Any]
    followup_event_ids: list[int]
    replan_event_id: int
    question_hint: int
    question_clarified: bool
    question_diagnosed: bool
    question_diagnostic_evidence_id: int
    question_attempt: str
    question_observation: str


class TutorState(TypedDict):
    input: dict[str, Any]
    output: dict[str, Any]


class VerifierState(TypedDict, total=False):
    """仅含出题白名单或本次独立验证作答，没有身份、辅导消息和父图状态。"""

    operation: str
    input: dict[str, Any]
    item: dict[str, Any]
    answer: str
    items: list[dict[str, Any]]
    judgement: dict[str, Any]


def build_graph(runtime: Orchestrator, checkpointer: BaseCheckpointSaver):
    async def tutor_turn(state: TutorState) -> dict:
        result = await runtime.tutor.turn(
            TutorTurnInput.model_validate(state["input"]), runtime.bus
        )
        return {"output": result.model_dump(mode="json")}

    tutor = StateGraph(TutorState)
    tutor.add_node("turn", tutor_turn)
    tutor.add_edge(START, "turn")
    tutor.add_edge("turn", END)
    tutor_graph = tutor.compile()

    async def compose(state: VerifierState) -> dict:
        items = await runtime.verifier.compose(VerifierInput.model_validate(state["input"]))
        return {"items": [item.model_dump(mode="json") for item in items]}

    async def judge(state: VerifierState) -> dict:
        result = await runtime.verifier.judge(Item.model_validate(state["item"]), state["answer"])
        return {"judgement": result.model_dump(mode="json")}

    verifier = StateGraph(VerifierState)
    verifier.add_node("compose", compose)
    verifier.add_node("judge", judge)
    verifier.add_conditional_edges(START, lambda state: state["operation"])
    verifier.add_edge("compose", END)
    verifier.add_edge("judge", END)
    # The parent checkpoints both calls. The subgraph must not resume with a
    # previously captured input after the parent has refreshed its whitelist.
    verifier_graph = verifier.compile(checkpointer=False)

    async def call_tutor(state: RunState) -> dict:
        token = current_teaching_date.set(date.fromisoformat(state["teaching_date"]))
        try:
            result = await tutor_graph.ainvoke({"input": runtime.tutor_input(state)})
        finally:
            current_teaching_date.reset(token)
        return {"tutor_turn": result["output"]}

    async def call_question(state: RunState) -> dict:
        return await question_turn(runtime, state)

    def prepare_verification(state: RunState) -> dict:
        if state.get("dimensions") is None:
            with Session(runtime.engine) as session:
                task = session.get(Task, state["task_id"])
                if (
                    task
                    and task.type == TaskType.VERIFICATION
                    and task.reason.startswith(QUESTION_FOLLOWUP)
                ):
                    state = {**state, "dimensions": [Dimension.CORRECTNESS.value]}
                elif task and task.type == TaskType.VERIFICATION and not task.misconception_ids:
                    seed_dimensions = set(
                        session.exec(
                            select(Item.dimension).where(
                                Item.knowledge_point_id == task.knowledge_point_id,
                                Item.purpose == ItemPurpose.VERIFICATION,
                                Item.source == ItemSource.SEED,
                            )
                        )
                    )
                    if seed_dimensions == {Dimension.EXPLANATION}:
                        state = {**state, "dimensions": [Dimension.EXPLANATION.value]}
        return runtime.prepare_verification(state)

    async def compose_verification(state: RunState) -> dict:
        # A checkpoint may have been saved with legacy fingerprints or before another
        # task showed a question. Refresh the whitelist before resuming composition.
        prepared = prepare_verification(state)
        result = await verifier_graph.ainvoke(
            {"operation": "compose", "input": prepared["verifier_input"]}, durability="exit"
        )
        return {**prepared, "verification_items": result["items"], "item_index": 0}

    def present_verification(state: RunState) -> dict:
        item = Item.model_validate(state["verification_items"][state["item_index"]])
        if item.spec is not None:
            current = SympyMathTools().fingerprint(item.spec)
            with Session(runtime.engine) as session:
                task = session.get(Task, state["task_id"])
                # Node retries can follow a successful business write but failed checkpoint.
                recorded = session.exec(
                    select(ItemExposure).where(
                        ItemExposure.student_id == task.student_id,
                        ItemExposure.task_id == task.id,
                        ItemExposure.item_id == item.id,
                    )
                ).first()
                if recorded is None and current in used_fingerprints(
                    session, task.student_id, state["shown_item_ids"]
                ):
                    return {"stale_verification_item": True}
        try:
            return {**runtime.present_item(state), "stale_verification_item": False}
        except VerificationExposureConflict:
            return {"stale_verification_item": True}

    async def replace_verification_item(state: RunState) -> dict:
        items = state["verification_items"].copy()
        index = state["item_index"]
        prepared = prepare_verification(state)["verifier_input"]
        used = set(prepared["used_fingerprints"])
        tools = SympyMathTools()
        for i, value in enumerate(items):
            if i == index:
                continue
            staged = Item.model_validate(value)
            if staged.fingerprint:
                used.add(staged.fingerprint)
            if staged.spec:
                used.add(tools.fingerprint(staged.spec))
        inp = {
            **prepared,
            "dimensions": [items[index]["dimension"]],
            "used_fingerprints": sorted(used),
        }
        result = await verifier_graph.ainvoke(
            {"operation": "compose", "input": inp}, durability="exit"
        )
        replacements = result["items"]
        if len(replacements) != 1:
            raise ValueError("重选验证题时只允许返回一道题")
        replacement = Item.model_validate(replacements[0])
        if replacement.spec is not None and tools.fingerprint(replacement.spec) in used:
            raise ValueError("重选的验证题与已用题或待展示题重复")
        runtime.save_items({**state, "verifier_input": inp, "verification_items": replacements})
        items[index] = replacements[0]
        return {"verification_items": items, "stale_verification_item": False}

    async def judge_verification(state: RunState) -> dict:
        # 这里也不透传父图：评阅只能看到这一道独立测题和该题作答。
        result = await verifier_graph.ainvoke(
            {
                "operation": "judge",
                "item": state["verification_items"][state["item_index"]],
                "answer": state["incoming_message"]["content"],
            },
            durability="exit",
        )
        return {"judgement": result["judgement"]}

    def wait_for_student(state: RunState) -> dict:
        # 不在中断节点中发题、调用 Agent 或写库；恢复会重跑这个函数。
        message = interrupt({"awaiting_item_id": state.get("awaiting_item_id")})
        return {"incoming_message": message}

    graph = StateGraph(RunState)
    for name, node in {
        "plan": runtime.plan,
        "save_plan": runtime.save_plan,
        "activate": runtime.activate,
        "tutor": call_tutor,
        "save_tutor": runtime.save_tutor,
        "question": call_question,
        "prepare_verification": prepare_verification,
        "compose_verification": compose_verification,
        "save_items": runtime.save_items,
        "present_item": present_verification,
        "replace_verification_item": replace_verification_item,
        "wait_input": wait_for_student,
        "accept_input": runtime.accept_input,
        "judge_verification": judge_verification,
        "save_evidence": runtime.save_evidence,
        "finish": runtime.finish,
        "replan": runtime.replan,
    }.items():
        graph.add_node(name, node)
    graph.add_conditional_edges(
        START, lambda state: "plan" if state["kind"] == "event" else "activate"
    )
    graph.add_edge("plan", "save_plan")
    graph.add_conditional_edges(
        "save_plan", lambda state: "plan" if state.get("plan_stale") else END
    )
    graph.add_conditional_edges(
        "activate",
        lambda state: {
            "tutoring": "tutor",
            "question": "question",
            "verifying": "prepare_verification",
        }[state["phase"]],
    )
    graph.add_conditional_edges(
        "question",
        lambda state: {
            "question": "wait_input",
            "question_diagnosis": "tutor",
            "question_finished": "finish",
        }[state["phase"]],
    )
    graph.add_edge("tutor", "save_tutor")
    graph.add_conditional_edges(
        "save_tutor",
        lambda state: (
            {
                "await_student": "wait_input",
                "request_verification": "prepare_verification",
                "request_replan": "replan",
            }[state["tutor_turn"]["outcome"]]
            if state["phase"] != "question_diagnosis"
            or state["tutor_turn"]["outcome"] != "request_verification"
            else "question"
        ),
    )
    graph.add_edge("prepare_verification", "compose_verification")
    graph.add_edge("compose_verification", "save_items")
    graph.add_edge("save_items", "present_item")
    graph.add_conditional_edges(
        "present_item",
        lambda state: (
            "replace_verification_item" if state["stale_verification_item"] else "wait_input"
        ),
    )
    graph.add_edge("replace_verification_item", "present_item")
    graph.add_edge("wait_input", "accept_input")
    graph.add_conditional_edges(
        "accept_input",
        lambda state: {
            "tutoring": "tutor",
            "question_diagnosis": "tutor",
            "question": "question",
            "verifying": "judge_verification",
        }[state["phase"]],
    )
    graph.add_edge("judge_verification", "save_evidence")
    graph.add_conditional_edges(
        "save_evidence",
        lambda state: (
            "present_item" if state["item_index"] < len(state["verification_items"]) else "finish"
        ),
    )
    graph.add_edge("finish", END)
    graph.add_edge("replan", END)
    return graph.compile(checkpointer=checkpointer)

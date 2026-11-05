"""主图负责业务流转；子图只接收各自的输入，不共享对话通道。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.contracts.agents import TutorTurnInput, VerifierInput
from app.contracts.entities import Item

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from app.orchestration.runtime import Orchestrator


class RunState(TypedDict, total=False):
    kind: str
    event_id: int
    task_id: int
    teaching_date: str
    plan: dict[str, Any]
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
    item_index: int
    judgement: dict[str, Any]
    followup_event_ids: list[int]
    replan_event_id: int


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
    verifier_graph = verifier.compile()

    async def call_tutor(state: RunState) -> dict:
        result = await tutor_graph.ainvoke({"input": runtime.tutor_input(state)})
        return {"tutor_turn": result["output"]}

    async def compose_verification(state: RunState) -> dict:
        result = await verifier_graph.ainvoke(
            {"operation": "compose", "input": state["verifier_input"]}
        )
        return {"verification_items": result["items"], "item_index": 0}

    async def judge_verification(state: RunState) -> dict:
        # 这里也不透传父图：评阅只能看到这一道独立测题和该题作答。
        result = await verifier_graph.ainvoke(
            {
                "operation": "judge",
                "item": state["verification_items"][state["item_index"]],
                "answer": state["incoming_message"]["content"],
            }
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
        "prepare_verification": runtime.prepare_verification,
        "compose_verification": compose_verification,
        "save_items": runtime.save_items,
        "present_item": runtime.present_item,
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
    graph.add_edge("save_plan", END)
    graph.add_conditional_edges(
        "activate",
        lambda state: "tutor" if state["phase"] == "tutoring" else "prepare_verification",
    )
    graph.add_edge("tutor", "save_tutor")
    graph.add_conditional_edges(
        "save_tutor",
        lambda state: {
            "await_student": "wait_input",
            "request_verification": "prepare_verification",
            "request_replan": "replan",
        }[state["tutor_turn"]["outcome"]],
    )
    graph.add_edge("prepare_verification", "compose_verification")
    graph.add_edge("compose_verification", "save_items")
    graph.add_edge("save_items", "present_item")
    graph.add_edge("present_item", "wait_input")
    graph.add_edge("wait_input", "accept_input")
    graph.add_conditional_edges(
        "accept_input",
        lambda state: "tutor" if state["phase"] == "tutoring" else "judge_verification",
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

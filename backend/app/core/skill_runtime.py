"""技能/任务运行时状态机模块。

本模块是会话级技能编排的核心，负责根据路由器（Router）的决策来驱动
``ChatSession`` 的状态变迁。它管理技能的激活、挂起、恢复、完成，
以及多任务队列（pending_tasks）的增删改查和等价合并。

核心概念
--------
- **ChatSession**：一次用户会话的持久化对象，包含当前激活的技能/步骤、
  槽位（slots）、待办任务队列（pending_tasks）、等待输入状态等。
- **RouterDecision**：路由器对用户意图分析后产出的决策，包含决策类型
  （如 start_new_task、continue_active、complete_task 等）、目标任务、
  槽位提示、任务更新等。
- **Task Frame（任务帧）**：待办任务的标准化字典结构，包含 task_id、
  skill_id、step_id、slots、状态、恢复策略等。挂起的任务以帧的形式
  存入 ``pending_tasks_json`` 队列。
- **任务等价性（equivalence）**：两个任务帧如果指向同一技能且拥有相同的
  "身份槽位"（如 document_id、order_id），则被视为等价，可合并而非重复入队。
- **身份槽位（TASK_IDENTITY_FIELDS）**：用于判定任务等价性的关键业务字段集合。

状态流转概览
------------
1. ``apply_decision``：主入口，根据路由决策全面更新会话状态。
2. ``complete_current_skill``：完成当前技能，清理相关状态。
3. ``suspend_current_skill``：挂起当前技能，生成任务帧入队。
4. ``restore_task_frame``：从队列恢复一个任务帧为激活状态。

与其他模块的关系
----------------
- 依赖 :mod:`app.db.models` 的 ``ChatSession`` 模型和工具函数。
- 依赖 :mod:`app.session.session_schema` 的 ``RouterDecision``、``PendingTask``、
  ``TaskUpdate`` 数据模型。
- 依赖 :mod:`app.session.slot_policy`` 的 ``strip_router_generated_message_slots``
  做槽位清理。
- 被 :mod:`app.session` 下的会话编排/轮次处理器调用。
"""

from __future__ import annotations

from typing import Any

from app.db.models import ChatSession, new_id, utc_now
from app.session.session_schema import PendingTask, RouterDecision, TaskUpdate
from app.session.slot_policy import strip_router_generated_message_slots


# 任务身份字段集合：用于判定两个任务帧是否"等价"（指向同一业务实体）。
# 当两个任务的 skill_id 相同且这些字段有交集且值一致时，视为等价任务，
# 可合并而非重复入队。
TASK_IDENTITY_FIELDS = {
    "document_id",
    "knowledge_base_id",
    "order_id",
    "product_id",
    "product_name",
    "product_name_1",
    "product_name_2",
    "refund_type",
    "sku_id",
    "tool_name",
}


class SkillRuntime:
    """技能运行时状态机，负责驱动 ChatSession 的技能/任务状态变迁。

    该类是无状态的（所有状态都存储在传入的 ChatSession 对象上），
    方法通过直接修改 session 的各字段来实现状态转换。

    设计意图：将路由决策到会话状态变更的映射逻辑集中在此类中，
    使上层编排器只需调用 ``apply_decision`` 即可完成全部状态更新，
    而无需关心具体的状态转换细节。
    """

    def apply_decision(self, session: ChatSession, decision: RouterDecision) -> ChatSession:
        """根据路由决策全面更新会话状态（模块主入口）。

        执行流程：
        1. 清理决策中的槽位（移除路由自动生成的消息槽位）。
        2. 清理会话中由路由自动生成的消息槽位，规范化待办任务帧。
        3. 清空技能栈（skill_stack），应用任务更新和新增待办任务。
        4. 若决策选中了已有任务，从队列取出对应任务帧。
        5. 根据决策类型（decision）执行对应的状态转换。
        6. 处理等待输入状态和槽位提示。
        7. 更新时间戳。

        Args:
            session: 当前会话对象，会被原地修改。
            decision: 路由器产出的决策对象。

        Returns:
            更新后的会话对象（同一引用）。
        """
        # 清理决策中所有槽位：移除路由自动生成的消息类槽位
        _sanitize_decision_slots(decision)
        # 清理会话已有的消息槽位
        session.slots_json = strip_router_generated_message_slots(session.slots_json)
        # 规范化待办任务帧：合并 slots/slot_hints，移除空值
        session.pending_tasks_json = _sanitize_task_frames(session.pending_tasks_json)
        # 清空技能栈，准备根据决策重新设置
        session.skill_stack_json = []
        # 应用任务更新（修改/移除已有任务）
        self._apply_task_updates(session, decision.task_updates)
        # 追加新增的待办任务
        self._append_pending_tasks(session, decision.pending_tasks)
        # 清除"等待回答后恢复"标记
        session.resume_after_answer_json = None

        # 若决策选中了已有任务，从队列中取出其任务帧
        selected_frame = self._take_task_frame(session, decision.selected_task_id)
        # 将决策中的槽位提示合并到选中的任务帧
        selected_frame = _frame_with_slot_hints(selected_frame, decision.slot_hints)

        decision_name = decision.decision
        # 根据决策类型分发处理
        if decision_name in {"create_pending", "update_pending", "answer_only", "clarify"}:
            # 这些决策类型不需要改变当前激活的技能/步骤
            pass
        elif decision_name == "handoff_human":
            # 转人工：标记会话为 handoff 状态
            session.status = "handoff"
        elif decision_name in {"start_new_task", "continue_active", "switch_to_pending"}:
            # 启动新任务/继续当前任务/切换到待办任务：激活目标
            self._activate_decision_target(session, decision, selected_frame)
        elif decision_name == "complete_task":
            # 完成任务：若指定了任务 ID 则移除该任务帧，否则完成当前技能
            if decision.selected_task_id:
                self._remove_task_frame(session, decision.selected_task_id)
            else:
                self.complete_current_skill(session)

        # 处理"等待用户输入"状态
        if decision.awaiting_input:
            awaiting_input = decision.awaiting_input.model_dump(mode="json")
            active_task_id = _active_task_id(session)
            # 若有激活任务但 awaiting_input 中未指定 task_id，则自动补全
            if active_task_id and not awaiting_input.get("task_id"):
                awaiting_input["task_id"] = active_task_id
            session.awaiting_input_json = awaiting_input
        # 合并槽位提示到会话（完成任务的决策除外）
        if decision.slot_hints and decision_name != "complete_task":
            session.slots_json = strip_router_generated_message_slots(
                {**(session.slots_json or {}), **dict(decision.slot_hints)}
            )

        session.updated_at = utc_now()
        return session

    def complete_current_skill(self, session: ChatSession) -> ChatSession:
        """完成当前激活的技能，清理所有相关状态。

        执行流程：
        1. 获取当前激活的任务 ID。
        2. 生成一个"已完成"状态的任务帧快照。
        3. 从队列移除当前任务帧。
        4. 移除队列中与已完成任务等价的其他任务帧（避免重复）。
        5. 清空所有技能相关字段。

        Args:
            session: 当前会话对象，会被原地修改。

        Returns:
            更新后的会话对象（同一引用）。
        """
        active_task_id = _active_task_id(session)
        # 生成当前技能的完成帧快照（用于后续等价去重）
        completed_frame = _current_frame(session, status="completed")
        # 从队列中移除当前激活的任务
        if active_task_id:
            self._remove_task_frame(session, active_task_id)
        # 移除与已完成任务等价的其他待办任务（避免保留重复任务）
        if completed_frame:
            session.pending_tasks_json = _without_equivalent_task_frames(
                session.pending_tasks_json,
                completed_frame,
                exclude_task_id=active_task_id,
            )
        # 清空所有技能激活状态
        session.skill_stack_json = []
        session.active_skill_id = None
        session.active_step_id = None
        session.slots_json = {}
        session.awaiting_input_json = None
        session.resume_after_answer_json = None
        session.updated_at = utc_now()
        return session

    def suspend_current_skill(
        self, session: ChatSession, *, enqueue: bool = False
    ) -> dict[str, Any] | None:
        """挂起当前激活的技能，生成任务帧并（可选）入队。

        将当前技能的状态打包为一个 "pending" 任务帧，保存其技能 ID、步骤 ID、
        槽位、摘要、最后问题等信息，以便后续恢复。挂起后会清空当前激活状态。

        Args:
            session: 当前会话对象，会被原地修改。
            enqueue: 若为 ``True`` 则将生成的任务帧追加到待办队列；
                若为 ``False`` 则仅返回帧但不入队（由调用方自行处理）。

        Returns:
            挂起的任务帧字典；若当前无激活技能则返回 ``None``。
        """
        # 生成 pending 状态的任务帧，带恢复策略标记
        frame = _current_frame(session, status="pending", resume_policy="resume_after_turn_tasks")
        if not frame:
            return None
        # 保存当前的等待输入状态到帧中，便于恢复时还原
        frame["awaiting_input"] = dict(session.awaiting_input_json or {})
        if enqueue:
            self.enqueue_task_frame(session, frame)
        # 清空当前激活状态（技能已被挂起到队列中）
        session.active_skill_id = None
        session.active_step_id = None
        session.slots_json = {}
        session.awaiting_input_json = None
        session.summary = None
        session.last_agent_question = None
        session.resume_after_answer_json = None
        session.updated_at = utc_now()
        return frame

    def restore_task_frame(self, session: ChatSession, frame: dict[str, Any]) -> ChatSession:
        """从任务帧恢复技能到激活状态。

        将之前挂起的任务帧重新激活为当前技能，恢复其技能 ID、步骤 ID、槽位、
        摘要、最后问题以及等待输入状态。

        Args:
            session: 当前会话对象，会被原地修改。
            frame: 要恢复的任务帧字典。

        Returns:
            更新后的会话对象（同一引用）。
        """
        # 激活帧中的技能/步骤/槽位/摘要等
        _activate_frame(session, frame)
        # 恢复等待输入状态
        awaiting_input = frame.get("awaiting_input")
        if isinstance(awaiting_input, dict):
            session.awaiting_input_json = dict(awaiting_input)
        session.updated_at = utc_now()
        return session

    def enqueue_task_frame(self, session: ChatSession, frame: dict[str, Any]) -> None:
        """将任务帧追加到待办队列（去重：同一 task_id 不重复入队）。

        Args:
            session: 当前会话对象。
            frame: 要入队的任务帧字典。
        """
        next_frame = dict(frame)
        next_frame["status"] = "pending"
        frames = list(session.pending_tasks_json or [])
        task_id = str(next_frame.get("task_id") or "").strip()
        # 若队列中已存在相同 task_id 的帧，则跳过（去重）
        if task_id and any(
            isinstance(item, dict) and str(item.get("task_id") or "") == task_id
            for item in frames
        ):
            return
        frames.append(next_frame)
        session.pending_tasks_json = frames

    def enqueue_pending_task(self, session: ChatSession, task: PendingTask) -> None:
        """将 PendingTask 模型转换为任务帧并追加到待办队列。

        Args:
            session: 当前会话对象。
            task: 待入队的 PendingTask 模型实例。
        """
        self._append_pending_tasks(session, [task])

    def _activate_decision_target(
        self,
        session: ChatSession,
        decision: RouterDecision,
        selected_frame: dict[str, Any] | None,
    ) -> None:
        """根据决策激活目标任务（内部方法）。

        激活优先级：
        1. 若存在选中的任务帧（selected_frame），直接激活它。
        2. 否则根据决策类型和目标技能/步骤设置激活状态。

        Args:
            session: 当前会话对象。
            decision: 路由决策。
            selected_frame: 从队列取出的任务帧（可能为 ``None``）。
        """
        # 优先激活已选中的任务帧
        if selected_frame:
            _activate_frame(session, selected_frame)
            return
        # 无目标任务则不做任何操作
        if not decision.target_skill_id:
            return
        # 切换到待办任务：直接设置技能/步骤，清空任务 ID
        if decision.decision == "switch_to_pending":
            session.active_skill_id = decision.target_skill_id
            session.active_step_id = decision.target_step_id
            session.slots_json = strip_router_generated_message_slots(decision.slot_hints)
            _set_active_task_id(session, None)
            return
        # 当前无激活技能时，设置为目标技能
        if not session.active_skill_id and decision.target_skill_id:
            session.active_skill_id = decision.target_skill_id
            session.slots_json = strip_router_generated_message_slots(decision.slot_hints)
            _set_active_task_id(session, None)
        # 启动新任务时，覆盖当前激活技能
        if decision.target_skill_id and decision.decision == "start_new_task":
            session.active_skill_id = decision.target_skill_id
            session.slots_json = strip_router_generated_message_slots(decision.slot_hints)
            _set_active_task_id(session, None)
        # 设置目标步骤
        if decision.target_step_id:
            session.active_step_id = decision.target_step_id

    def _append_pending_tasks(self, session: ChatSession, tasks: list[PendingTask]) -> None:
        """将多个 PendingTask 追加到待办队列（含去重和等价合并）。

        对于每个待追加的任务：
        - 若 task_id 已存在则跳过。
        - 若找到等价任务帧（同技能+同身份槽位）则合并。
        - 否则直接追加。

        Args:
            session: 当前会话对象。
            tasks: 待追加的 PendingTask 列表。
        """
        if not tasks:
            return
        frames = list(session.pending_tasks_json or [])
        # 收集已有任务的 task_id 集合
        existing_ids = {
            str(frame.get("task_id"))
            for frame in frames
            if isinstance(frame, dict) and frame.get("task_id")
        }
        for task in tasks:
            frame = _task_frame_from_pending(task)
            task_id = str(frame.get("task_id") or "")
            # task_id 为空或已存在则跳过
            if not task_id or task_id in existing_ids:
                continue
            # 检查是否存在等价任务帧
            existing_index = _find_equivalent_task_frame_index(frames, frame)
            if existing_index is not None:
                # 合并到等价任务帧
                frames[existing_index] = _merge_task_frames(frames[existing_index], frame)
                continue
            # 无等价任务，直接追加
            frames.append(frame)
            existing_ids.add(task_id)
        session.pending_tasks_json = frames

    def _apply_task_updates(self, session: ChatSession, updates: list[TaskUpdate]) -> None:
        """应用任务更新列表到待办队列（修改状态/槽位或移除任务）。

        对于每个更新：
        - 若标记为移除或状态为 removed/completed/cancelled，则从队列删除。
        - 否则构建补丁（状态、技能、步骤、意图、槽位等）并应用到匹配的任务帧。

        Args:
            session: 当前会话对象。
            updates: 任务更新列表。
        """
        if not updates:
            return
        pending = list(session.pending_tasks_json or [])
        for update in updates:
            # 移除类更新
            if update.remove or update.status in {"removed", "completed", "cancelled"}:
                pending = _without_task_or_skill(pending, task_id=update.task_id)
                continue
            # 构建更新补丁，仅包含非 None 的字段
            patch = {
                key: value
                for key, value in {
                    "status": update.status,
                    "skill_id": update.target_skill_id,
                    "target_skill_id": update.target_skill_id,
                    "step_id": update.target_step_id,
                    "target_step_id": update.target_step_id,
                    "intent_summary": update.user_intent,
                    "user_intent": update.user_intent,
                    "reason": update.reason,
                    "source_message": update.source_message,
                    "updated_at": utc_now().isoformat(),
                }.items()
                if value is not None
            }
            # 槽位提示单独处理
            if update.slot_hints:
                slot_hints = strip_router_generated_message_slots(update.slot_hints)
                if slot_hints:
                    patch["slots"] = slot_hints
                    patch["slot_hints"] = slot_hints
            # 将补丁应用到匹配 task_id 的任务帧
            pending = _patch_task_frame(pending, update.task_id, patch)
        session.pending_tasks_json = pending

    def _take_task_frame(self, session: ChatSession, task_id: str | None) -> dict[str, Any] | None:
        """从待办队列中取出（移除）指定 task_id 的任务帧。

        与 :meth:`_remove_task_frame` 不同，此方法会返回被取出的帧，
        供调用方进一步使用（如激活）。

        Args:
            session: 当前会话对象。
            task_id: 要取出的任务 ID。

        Returns:
            被取出的任务帧字典；若未找到或 task_id 为空则返回 ``None``。
        """
        if not task_id:
            return None
        frame, pending = _pop_task_frame(session.pending_tasks_json, task_id)
        if frame:
            session.pending_tasks_json = pending
            return frame
        return None

    def _remove_task_frame(self, session: ChatSession, task_id: str | None) -> None:
        """从待办队列中移除指定 task_id 的任务帧。

        Args:
            session: 当前会话对象。
            task_id: 要移除的任务 ID。
        """
        if not task_id:
            return
        session.pending_tasks_json = _without_task_or_skill(session.pending_tasks_json, task_id=task_id)


def _sanitize_decision_slots(decision: RouterDecision) -> None:
    """清理决策对象中所有层级 的槽位，移除路由自动生成的消息类槽位。

    处理范围：决策本身的 slot_hints、任务帧/待办任务/新建任务的 slot_hints、
    任务更新的 slot_hints。

    Args:
        decision: 要清理的路由决策对象（会被原地修改）。
    """
    decision.slot_hints = strip_router_generated_message_slots(decision.slot_hints)
    for task in [*decision.task_frames, *decision.pending_tasks, *decision.created_tasks]:
        task.slot_hints = strip_router_generated_message_slots(task.slot_hints)
    for update in decision.task_updates:
        update.slot_hints = strip_router_generated_message_slots(update.slot_hints)


def _sanitize_task_frames(frames_json: list[dict] | None) -> list[dict]:
    """规范化待办任务帧列表，合并并清理槽位字段。

    对每个帧：合并 slot_hints 和 slots（slot_hints 优先级低），清理后若为空
    则移除这两个字段，否则统一设置。

    Args:
        frames_json: 原始任务帧列表。

    Returns:
        规范化后的任务帧列表。
    """
    frames: list[dict] = []
    for frame in list(frames_json or []):
        if not isinstance(frame, dict):
            continue
        next_frame = dict(frame)
        raw_slots = next_frame.get("slots") if isinstance(next_frame.get("slots"), dict) else {}
        raw_hints = next_frame.get("slot_hints") if isinstance(next_frame.get("slot_hints"), dict) else {}
        # 合并：slot_hints 作为基础，slots 覆盖（slots 优先级更高）
        slots = strip_router_generated_message_slots({**raw_hints, **raw_slots})
        if slots:
            next_frame["slots"] = slots
            next_frame["slot_hints"] = slots
        else:
            # 空槽位移除字段
            next_frame.pop("slots", None)
            next_frame.pop("slot_hints", None)
        frames.append(next_frame)
    return frames


def _task_frame_from_pending(task: PendingTask) -> dict[str, Any]:
    """将 PendingTask 模型转换为标准化的任务帧字典。

    生成的帧包含完整的任务元数据，字段命名兼容新旧两种风格（如同时设置
    skill_id/target_skill_id）。

    Args:
        task: PendingTask 模型实例。

    Returns:
        标准化的任务帧字典。
    """
    now = utc_now().isoformat()
    # 生成 task_id：优先使用已有值，否则新建
    task_id = task.task_id or new_id("task")
    skill_id = task.target_skill_id
    step_id = task.target_step_id
    slots = strip_router_generated_message_slots(task.slot_hints)
    return {
        "task_id": task_id,
        "status": task.status or "pending",
        # 同时设置新旧两种字段名，保持兼容
        "skill_id": skill_id,
        "target_skill_id": skill_id,
        "step_id": step_id,
        "target_step_id": step_id,
        "slots": slots,
        "slot_hints": slots,
        "intent_summary": task.user_intent,
        "user_intent": task.user_intent,
        "source_turn_id": None,
        "source_message": task.source_message,
        "parent_task_id": None,
        "resume_policy": None,
        "reason": task.reason,
        "confidence": task.confidence,
        "created_at": now,
        "updated_at": now,
    }


def _current_frame(
    session: ChatSession,
    status: str,
    resume_policy: str | None = None,
) -> dict[str, Any] | None:
    """从当前会话的激活状态生成一个任务帧快照。

    将会话当前的 active_skill_id、active_step_id、slots、summary、
    last_agent_question 等打包为标准任务帧。

    Args:
        session: 当前会话对象。
        status: 帧的状态（如 "completed"、"pending"）。
        resume_policy: 恢复策略标识，用于挂起时标记恢复方式。

    Returns:
        任务帧字典；若当前无激活技能则返回 ``None``。
    """
    if not session.active_skill_id:
        return None
    now = utc_now().isoformat()
    # 获取当前激活任务的 ID，没有则新建
    task_id = _active_task_id(session) or new_id("task")
    return {
        "task_id": task_id,
        "status": status,
        "skill_id": session.active_skill_id,
        "target_skill_id": session.active_skill_id,
        "step_id": session.active_step_id,
        "target_step_id": session.active_step_id,
        "slots": strip_router_generated_message_slots(session.slots_json),
        "slot_hints": strip_router_generated_message_slots(session.slots_json),
        "intent_summary": None,
        "source_turn_id": None,
        "source_message": None,
        "parent_task_id": None,
        "resume_policy": resume_policy,
        # 以下字段用于挂起后恢复上下文
        "summary": session.summary,
        "last_agent_question": session.last_agent_question,
        "created_at": now,
        "updated_at": now,
    }


def _active_task_id(session: ChatSession) -> str | None:
    """从会话的 awaiting_input 状态中提取当前激活的任务 ID。

    任务 ID 存储在 awaiting_input_json 的 ``task_id`` 字段中。

    Args:
        session: 当前会话对象。

    Returns:
        激活任务的 ID 字符串；若无则返回 ``None``。
    """
    metadata = session.awaiting_input_json if isinstance(session.awaiting_input_json, dict) else {}
    task_id = metadata.get("task_id") if isinstance(metadata, dict) else None
    return str(task_id) if task_id else None


def _set_active_task_id(session: ChatSession, task_id: str | None) -> None:
    """设置或清除当前激活的任务 ID（存储在 awaiting_input_json 中）。

    Args:
        session: 当前会话对象。
        task_id: 要设置的任务 ID；为 ``None`` 时清除已有值。
    """
    if not task_id:
        # 清除 task_id：若 awaiting_input 中有该字段则移除
        if isinstance(session.awaiting_input_json, dict) and "task_id" in session.awaiting_input_json:
            data = dict(session.awaiting_input_json)
            data.pop("task_id", None)
            session.awaiting_input_json = data or None
        return
    # 设置 task_id
    data = dict(session.awaiting_input_json or {})
    data["task_id"] = task_id
    session.awaiting_input_json = data


def _activate_frame(session: ChatSession, frame: dict[str, Any]) -> None:
    """将任务帧激活为当前会话状态。

    从帧中恢复技能 ID、步骤 ID、槽位、摘要、最后问题和等待输入状态。

    Args:
        session: 当前会话对象，会被原地修改。
        frame: 要激活的任务帧。
    """
    # 恢复技能和步骤（兼容新旧字段名）
    session.active_skill_id = frame.get("skill_id") or frame.get("target_skill_id")
    session.active_step_id = frame.get("step_id") or frame.get("target_step_id")
    # 恢复槽位（slots 优先于 slot_hints）
    slots = frame.get("slots") if isinstance(frame.get("slots"), dict) else frame.get("slot_hints")
    session.slots_json = strip_router_generated_message_slots(slots)
    # 恢复摘要和最后问题
    session.summary = frame.get("summary")
    session.last_agent_question = frame.get("last_agent_question")
    # 恢复等待输入状态
    awaiting_input = frame.get("awaiting_input")
    if isinstance(awaiting_input, dict):
        session.awaiting_input_json = dict(awaiting_input)
    # 恢复任务 ID
    _set_active_task_id(session, str(frame.get("task_id") or ""))


def _pop_last_skill_frame(
    stack_json: list[dict] | None,
    skill_id: str | None,
) -> tuple[dict | None, list[dict]]:
    """从技能栈中弹出指定技能的最后一个帧（倒序查找）。

    Args:
        stack_json: 技能栈列表。
        skill_id: 要弹出的技能 ID。

    Returns:
        二元组 ``(frame, remaining_stack)``：弹出的帧（可能为 ``None``）
        和剩余的栈列表。
    """
    stack = list(stack_json or [])
    if not skill_id:
        return None, stack
    # 倒序遍历，找到第一个匹配的帧
    for index in range(len(stack) - 1, -1, -1):
        if stack[index].get("skill_id") == skill_id or stack[index].get("target_skill_id") == skill_id:
            frame = stack.pop(index)
            return frame, stack
    return None, stack


def _pop_task_frame(frames_json: list[dict] | None, task_id: str) -> tuple[dict | None, list[dict]]:
    """从任务帧列表中弹出指定 task_id 的帧。

    Args:
        frames_json: 任务帧列表。
        task_id: 要弹出的任务 ID。

    Returns:
        二元组 ``(frame, remaining_frames)``：弹出的帧（可能为 ``None``）
        和剩余的帧列表。
    """
    frames = list(frames_json or [])
    for index, frame in enumerate(frames):
        if isinstance(frame, dict) and str(frame.get("task_id") or "") == task_id:
            # 返回匹配帧及其前后剩余部分
            return frame, frames[:index] + frames[index + 1 :]
    return None, frames


def _without_task_or_skill(
    frames_json: list[dict] | None,
    task_id: str | None = None,
    skill_id: str | None = None,
) -> list[dict]:
    """从任务帧列表中移除匹配 task_id 或 skill_id 的所有帧。

    Args:
        frames_json: 任务帧列表。
        task_id: 要移除的任务 ID（可选）。
        skill_id: 要移除的技能 ID（可选，匹配 skill_id 或 target_skill_id）。

    Returns:
        过滤后的帧列表。
    """
    frames = []
    for frame in list(frames_json or []):
        # 按 task_id 过滤
        if task_id and str(frame.get("task_id") or "") == task_id:
            continue
        # 按 skill_id 过滤（兼容新旧字段名）
        if skill_id and (frame.get("skill_id") == skill_id or frame.get("target_skill_id") == skill_id):
            continue
        frames.append(frame)
    return frames


def _find_equivalent_task_frame_index(frames: list[dict], target: dict[str, Any]) -> int | None:
    """在任务帧列表中查找与目标帧等价的帧的索引。

    等价判定见 :func:`_task_frames_equivalent`。

    Args:
        frames: 任务帧列表。
        target: 目标帧。

    Returns:
        等价帧的索引；若未找到则返回 ``None``。
    """
    for index, frame in enumerate(frames):
        if isinstance(frame, dict) and _task_frames_equivalent(frame, target):
            return index
    return None


def _without_equivalent_task_frames(
    frames_json: list[dict] | None,
    completed_frame: dict[str, Any],
    exclude_task_id: str | None = None,
) -> list[dict]:
    """从任务帧列表中移除所有与已完成帧等价的帧（排除指定 task_id）。

    用于完成任务时清理队列中的重复任务。

    Args:
        frames_json: 任务帧列表。
        completed_frame: 已完成的任务帧，作为等价判定的基准。
        exclude_task_id: 要排除（不移除）的 task_id。

    Returns:
        过滤后的帧列表。
    """
    frames: list[dict] = []
    for frame in list(frames_json or []):
        if not isinstance(frame, dict):
            continue
        # 排除指定 task_id 的帧
        if exclude_task_id and str(frame.get("task_id") or "") == exclude_task_id:
            continue
        # 移除与已完成帧等价的帧
        if _task_frames_equivalent(frame, completed_frame):
            continue
        frames.append(frame)
    return frames


def _task_frames_equivalent(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """判定两个任务帧是否等价。

    等价条件：
    1. 指向同一技能（skill_id 相同）。
    2. 两者的身份槽位（TASK_IDENTITY_FIELDS 中的字段）有交集。
    3. 交集中的字段值完全一致。

    Args:
        left: 左侧任务帧。
        right: 右侧任务帧。

    Returns:
        ``True`` 表示等价；``False`` 表示不等价。
    """
    # 技能不同则不等价
    if _frame_skill_id(left) != _frame_skill_id(right):
        return False
    left_identity = _task_identity_slots(left)
    right_identity = _task_identity_slots(right)
    # 任一方无身份槽位则不等价
    if not left_identity or not right_identity:
        return False
    # 取交集键
    common_keys = set(left_identity) & set(right_identity)
    if not common_keys:
        return False
    # 交集键的值必须全部一致
    return all(left_identity[key] == right_identity[key] for key in common_keys)


def _merge_task_frames(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """将两个等价任务帧合并为一个。

    合并规则：
    - 槽位：existing 和 incoming 的槽位合并（incoming 覆盖 existing）。
    - 其他字段：incoming 的非空值覆盖 existing（task_id 和 created_at 除外，
      保留 existing 的值）。
    - updated_at：更新为当前时间。

    Args:
        existing: 已存在的任务帧。
        incoming: 新传入的任务帧。

    Returns:
        合并后的任务帧。
    """
    merged = dict(existing)
    # 合并已有帧的槽位
    existing_slots = strip_router_generated_message_slots(
        existing.get("slots") if isinstance(existing.get("slots"), dict) else {}
    )
    # 合并新帧的槽位
    incoming_slots = strip_router_generated_message_slots(
        incoming.get("slots") if isinstance(incoming.get("slots"), dict) else {}
    )
    # 槽位合并：incoming 覆盖 existing
    slots = {**existing_slots, **incoming_slots}
    # 用 incoming 的非空字段更新 merged（排除 task_id 和 created_at）
    merged.update(
        {
            key: value
            for key, value in incoming.items()
            if key not in {"task_id", "created_at", "slots", "slot_hints"} and value not in {None, ""}
        }
    )
    if slots:
        merged["slots"] = slots
        merged["slot_hints"] = slots
    else:
        merged.pop("slots", None)
        merged.pop("slot_hints", None)
    # 保留已有的 task_id 和 created_at
    merged["task_id"] = existing.get("task_id") or incoming.get("task_id")
    merged["created_at"] = existing.get("created_at") or incoming.get("created_at")
    merged["updated_at"] = utc_now().isoformat()
    return merged


def _frame_skill_id(frame: dict[str, Any]) -> str | None:
    """提取任务帧的技能 ID（兼容 skill_id 和 target_skill_id）。

    Args:
        frame: 任务帧字典。

    Returns:
        技能 ID 字符串；若无则返回 ``None``。
    """
    value = frame.get("skill_id") or frame.get("target_skill_id")
    return str(value) if value else None


def _task_identity_slots(frame: dict[str, Any]) -> dict[str, str]:
    """提取任务帧中的身份槽位（用于等价判定）。

    从 slots/slot_hints 中提取属于 ``TASK_IDENTITY_FIELDS`` 的字段，
    值统一转为小写字符串以便比较。

    Args:
        frame: 任务帧字典。

    Returns:
        身份槽位字典 ``{field: value}``；若无有效身份槽位则返回空字典。
    """
    slots = frame.get("slots") if isinstance(frame.get("slots"), dict) else frame.get("slot_hints")
    if not isinstance(slots, dict):
        return {}
    slots = strip_router_generated_message_slots(slots)
    identity: dict[str, str] = {}
    for key in TASK_IDENTITY_FIELDS:
        value = slots.get(key)
        if value is None or value == "":
            continue
        # 统一转为小写字符串以便等价比较
        identity[key] = str(value).strip().lower()
    return identity


def _patch_task_frame(frames_json: list[dict] | None, task_id: str, patch: dict[str, Any]) -> list[dict]:
    """将补丁应用到匹配 task_id 的任务帧。

    遍历帧列表，找到 task_id 匹配的帧后用补丁覆盖更新。

    Args:
        frames_json: 任务帧列表。
        task_id: 要更新的任务 ID。
        patch: 补丁字典（键值对形式）。

    Returns:
        更新后的帧列表。
    """
    frames = []
    for frame in list(frames_json or []):
        if isinstance(frame, dict) and str(frame.get("task_id") or "") == task_id:
            # 匹配的帧：合并补丁
            frames.append({**frame, **patch})
        else:
            frames.append(frame)
    return frames


def _upsert_frame(frames_json: list[dict] | None, frame: dict[str, Any]) -> list[dict]:
    """插入或替换任务帧（按 task_id 匹配）。

    若列表中已存在相同 task_id 的帧则替换，否则追加到末尾。

    Args:
        frames_json: 任务帧列表。
        frame: 要插入/替换的帧。

    Returns:
        更新后的帧列表。
    """
    task_id = str(frame.get("task_id") or "")
    frames = []
    replaced = False
    for current in list(frames_json or []):
        current_task_id = str(current.get("task_id") or "")
        # task_id 匹配则替换
        if task_id and current_task_id == task_id:
            frames.append(frame)
            replaced = True
        else:
            frames.append(current)
    # 未发生替换则追加
    if not replaced:
        frames.append(frame)
    return frames


def _frame_with_slot_hints(frame: dict[str, Any] | None, slot_hints: dict | None) -> dict[str, Any] | None:
    """将槽位提示合并到任务帧中。

    合并优先级：incoming slot_hints > existing slots > existing slot_hints。

    Args:
        frame: 原始任务帧（可能为 ``None``）。
        slot_hints: 要合并的槽位提示（可能为 ``None``）。

    Returns:
        合并后的新任务帧；若输入 frame 或 slot_hints 为空则原样返回。
    """
    if not frame or not slot_hints:
        return frame
    next_frame = dict(frame)
    # 提取已有的 slots
    current_slots = strip_router_generated_message_slots(
        next_frame.get("slots") if isinstance(next_frame.get("slots"), dict) else {}
    )
    # 提取已有的 slot_hints
    current_hints = strip_router_generated_message_slots(
        next_frame.get("slot_hints") if isinstance(next_frame.get("slot_hints"), dict) else {}
    )
    # 提取传入的 slot_hints
    incoming_hints = strip_router_generated_message_slots(slot_hints)
    # 三层合并：incoming > slots > hints
    merged_slots = strip_router_generated_message_slots({**current_hints, **current_slots, **incoming_hints})
    next_frame["slots"] = merged_slots
    next_frame["slot_hints"] = merged_slots
    next_frame["updated_at"] = utc_now().isoformat()
    return next_frame

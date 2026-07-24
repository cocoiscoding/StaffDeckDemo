"""定时任务服务模块。

提供定时任务（Scheduled Task）的完整生命周期管理：

1. **CRUD**：创建（``create_scheduled_task``）、更新（``update_scheduled_task``）定时任务。
2. **调度计算**：根据调度类型（once/daily/weekly/monthly）和时区，
   计算下一次执行时间（``compute_next_run_at``）。
3. **到期扫描**：使用乐观锁（``lease_until``）安全地竞争到期任务（``due_scheduled_tasks``）。
4. **执行引擎**：为每次执行创建独立会话，通过 ``AgentLoop`` 执行任务 prompt，
   支持同步和异步两种执行模式。
5. **LLM 草案检测**：从用户自然语言输入中解析出可编辑的定时任务草案（``detect_scheduled_task_draft``）。

调度类型支持：``once``（一次性）、``daily``（每天）、``weekly``（每周）、``monthly``（每月）。
并发策略（ConcurrencyPolicy）：``forbid``（禁止并发）、``allow``（允许并发）。
"""

from __future__ import annotations

import calendar
import re
import socket
import threading
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.agents.branching import model_for_agent
from app.core import AgentLoop
from app.db import engine
from app.db.models import (
    AgentEvent,
    AgentProfile,
    ChatSession,
    ScheduledTask,
    ScheduledTaskRun,
    User,
    new_id,
    utc_now,
)
from app.llm import LLMClient, LLMError
from app.observability.spans import llm_operation
from app.scheduled_tasks.schema import (
    ScheduledTaskCreateRequest,
    ScheduledTaskDraftRead,
    ScheduledTaskRead,
    ScheduledTaskRunRead,
    ScheduledTaskUpdateRequest,
)
from app.session.session_schema import ChatTurnRequest, ChatTurnResponse
from app.security.permissions import agent_owned_by_user as _agent_owned_by_user
from app.security.permissions import is_admin_user as _is_admin_user
from app.security.tenant import ensure_tenant


DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_TASK_TIME = "09:00"
LEASE_SECONDS = 15 * 60
WORKER_SLEEP_SECONDS = 5
SCHEDULE_TYPES = {"once", "daily", "weekly", "monthly"}


class _LLMScheduledTaskDraft(BaseModel):
    """LLM 解析出的定时任务草案内部模型。

    由 ``detect_scheduled_task_draft`` 调用 LLM 生成，
    包含从用户自然语言中解析出的任务标题、执行内容、调度类型和时间等字段。
    """

    should_create: bool = False
    title: str = ""
    prompt: str = ""
    description: str | None = None
    schedule_type: str = "daily"
    schedule: dict[str, Any] = Field(default_factory=dict)
    timezone: str | None = None
    rrule: str | None = None
    confidence: float = 0.0
    reason: str | None = None


SCHEDULE_DRAFT_PROMPT = """
你是 StaffDeck 数字员工的自动任务配置解析器。
用户已经在对话框中选择了“创建定时任务”模式。请把用户输入整理成一个可编辑的自动任务草案。
如果用户没有写清时间计划，默认每天 09:00 执行；如果用户没有写清任务目标，用原始输入作为执行内容。

返回一个 JSON object，字段如下：
- should_create: boolean
- title: 12 到 32 个中文字符，概括自动任务名称
- prompt: 每次到点后交给数字员工的新会话任务描述，不要包含“帮我设个定时任务”等配置话术
- description: 可选，解释为什么这样拆解
- schedule_type: one of "once", "daily", "weekly", "monthly"
- schedule:
  - once: {"run_at": "YYYY-MM-DDTHH:mm:ss±HH:MM"}
  - daily: {"time": "HH:mm"}
  - weekly: {"time": "HH:mm", "weekdays": [0-6]}，0=周一，6=周日
  - monthly: {"time": "HH:mm", "day_of_month": 1-31}
- timezone: IANA 时区，默认使用 default_timezone
- rrule: 可选 RRULE 字符串
- confidence: 0 到 1
- reason: 简短说明

时间不完整时可以合理补齐：只说“每天”默认 09:00；只说“每周一”默认 09:00。
调度类型判断规则：
- 用户只给出一个具体时间点，例如“下午2点10分”“14:10”“今晚8点”，且没有明确“每天/每日/每周/每月/定期/重复”等周期要求时，生成 once。
- once.run_at 使用 now 所在日期和用户给出的时间；如果该时间已经过去，则顺延到下一天。
- 只有用户明确说“每天/每日/每晚/每早/每周/每月/工作日/定期/重复”等周期要求时，才生成 daily/weekly/monthly。
不要输出 Markdown，不要输出解释文本，只输出 JSON。
"""


def scheduled_task_read(row: ScheduledTask) -> ScheduledTaskRead:
    """将定时任务数据库行转换为 API 响应模型。

    Args:
        row: ``ScheduledTask`` 数据库模型实例。

    Returns:
        ``ScheduledTaskRead`` 响应对象。
    """
    return ScheduledTaskRead(
        id=row.id,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        created_by_user_id=row.created_by_user_id,
        title=row.title,
        prompt=row.prompt,
        description=row.description,
        schedule_type=row.schedule_type,
        schedule=row.schedule_json or {},
        timezone=row.timezone,
        rrule=row.rrule,
        status=row.status,
        concurrency_policy=row.concurrency_policy,
        misfire_policy=row.misfire_policy,
        max_runs=row.max_runs,
        end_at=_dt(row.end_at),
        next_run_at=_dt(row.next_run_at),
        last_run_at=_dt(row.last_run_at),
        last_status=row.last_status,
        run_count=row.run_count,
        source_session_id=row.source_session_id,
        metadata=row.metadata_json or {},
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def scheduled_task_run_read(row: ScheduledTaskRun, task: ScheduledTask | None = None) -> ScheduledTaskRunRead:
    """将定时任务执行记录行转换为 API 响应模型。

    Args:
        row: ``ScheduledTaskRun`` 数据库模型实例。
        task: 关联的定时任务（可选），用于补充任务标题和状态。

    Returns:
        ``ScheduledTaskRunRead`` 响应对象。
    """
    return ScheduledTaskRunRead(
        id=row.id,
        tenant_id=row.tenant_id,
        scheduled_task_id=row.scheduled_task_id,
        task_title=task.title if task else None,
        task_status=task.status if task else None,
        agent_id=row.agent_id,
        user_id=row.user_id,
        session_id=row.session_id,
        scheduled_for=row.scheduled_for.isoformat(),
        status=row.status,
        started_at=_dt(row.started_at),
        finished_at=_dt(row.finished_at),
        result_summary=row.result_summary,
        error=row.error,
        trace=row.trace_json or {},
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def create_scheduled_task(
    db: Session,
    request: ScheduledTaskCreateRequest,
    current_user: User,
) -> ScheduledTask:
    """创建新的定时任务。

    校验 agent 访问权限，规范化调度配置，计算首次执行时间后持久化。

    Args:
        db: 数据库会话。
        request: 创建请求体。
        current_user: 当前登录用户。

    Returns:
        ScheduledTask: 新建的定时任务行。

    Raises:
        HTTPException: 权限不足、调度类型无效或字段为空时抛出。
    """
    ensure_tenant(db, request.tenant_id)
    _ensure_agent_access(db, request.tenant_id, request.agent_id, current_user)
    schedule = normalize_schedule(request.schedule_type, request.schedule, request.timezone)
    now = utc_now()
    end_at = parse_user_datetime(request.end_at, request.timezone) if request.end_at else None
    row = ScheduledTask(
        tenant_id=request.tenant_id,
        agent_id=request.agent_id,
        created_by_user_id=current_user.id,
        title=_nonempty(request.title, "自动任务名称不能为空", 80),
        prompt=_nonempty(request.prompt, "自动任务描述不能为空", 10000),
        description=(request.description or "").strip() or None,
        schedule_type=request.schedule_type,
        schedule_json=schedule,
        timezone=request.timezone or DEFAULT_TIMEZONE,
        rrule=(request.rrule or "").strip() or build_rrule(request.schedule_type, schedule),
        status=request.status,
        concurrency_policy=request.concurrency_policy,
        misfire_policy=request.misfire_policy,
        max_runs=request.max_runs,
        end_at=end_at,
        source_session_id=request.source_session_id,
        metadata_json=request.metadata or {},
        created_at=now,
        updated_at=now,
    )
    row.next_run_at = compute_next_run_at(row, after=now)
    if row.status != "active":
        row.next_run_at = None
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_scheduled_task(
    db: Session,
    row: ScheduledTask,
    request: ScheduledTaskUpdateRequest,
    current_user: User,
) -> ScheduledTask:
    """更新定时任务的各项配置。

    按需更新请求中提供的字段，重新计算调度配置和下次执行时间。

    Args:
        db: 数据库会话。
        row: 待更新的定时任务行。
        request: 更新请求体。
        current_user: 当前登录用户。

    Returns:
        ScheduledTask: 更新后的定时任务行。

    Raises:
        HTTPException: 权限不足或字段无效时抛出。
    """
    _ensure_task_access(row, current_user)
    if request.agent_id is not None and request.agent_id != row.agent_id:
        _ensure_agent_access(db, request.tenant_id, request.agent_id, current_user)
        row.agent_id = request.agent_id
    if request.title is not None:
        row.title = _nonempty(request.title, "自动任务名称不能为空", 80)
    if request.prompt is not None:
        row.prompt = _nonempty(request.prompt, "自动任务描述不能为空", 10000)
    if request.description is not None:
        row.description = request.description.strip() or None
    if request.timezone is not None:
        row.timezone = request.timezone or DEFAULT_TIMEZONE
    if request.schedule_type is not None:
        row.schedule_type = request.schedule_type
    if request.schedule is not None or request.schedule_type is not None or request.timezone is not None:
        row.schedule_json = normalize_schedule(row.schedule_type, request.schedule or row.schedule_json, row.timezone)
        row.rrule = request.rrule if request.rrule is not None else build_rrule(row.schedule_type, row.schedule_json)
    elif request.rrule is not None:
        row.rrule = request.rrule.strip() or None
    if request.status is not None:
        row.status = request.status
    if request.concurrency_policy is not None:
        row.concurrency_policy = request.concurrency_policy
    if request.misfire_policy is not None:
        row.misfire_policy = request.misfire_policy
    if request.max_runs is not None:
        row.max_runs = request.max_runs
    if request.end_at is not None:
        row.end_at = parse_user_datetime(request.end_at, row.timezone) if request.end_at else None
    if request.metadata is not None:
        row.metadata_json = request.metadata
    row.updated_at = utc_now()
    row.next_run_at = compute_next_run_at(row, after=utc_now()) if row.status == "active" else None
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def detect_scheduled_task_draft(
    db: Session,
    tenant_id: str,
    agent_id: str,
    user_id: str,
    message: str,
    source_session_id: str | None = None,
    timezone: str | None = None,
) -> ScheduledTaskDraftRead | None:
    """从用户自然语言消息中通过 LLM 检测定时任务草案。

    调用 LLM 分析用户消息，如果检测到定时任务意图则返回可编辑的草案。
    如果 LLM 判断不需要创建任务或解析失败，则返回 None。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。
        agent_id: 关联的 agent ID。
        user_id: 用户 ID。
        message: 用户的自然语言消息。
        source_session_id: 来源会话 ID（可选）。
        timezone: 用户时区（可选，默认使用 ``DEFAULT_TIMEZONE``）。

    Returns:
        定时任务草案读取对象，如果不需要创建则返回 None。
    """
    ensure_tenant(db, tenant_id)
    agent = db.get(AgentProfile, agent_id)
    if not agent or agent.tenant_id != tenant_id or agent.is_overall or agent.status != "active":
        return None
    user_timezone = _safe_timezone(timezone)
    llm_draft = _detect_with_llm(db, tenant_id, agent_id, message, user_timezone)
    if llm_draft is None or not llm_draft.should_create:
        return None
    draft = llm_draft
    try:
        schedule_type = _normalize_schedule_type(draft.schedule_type)
        draft_timezone = _safe_timezone(draft.timezone, user_timezone)
        schedule = normalize_schedule(schedule_type, draft.schedule, draft_timezone)
    except HTTPException:
        return None
    title = (draft.title or _compact_title(message)).strip()[:80]
    prompt = (draft.prompt or _execution_goal_from_message(message)).strip()
    if not prompt:
        return None
    return ScheduledTaskDraftRead(
        should_create=True,
        tenant_id=tenant_id,
        agent_id=agent_id,
        title=title,
        prompt=prompt,
        description=draft.description,
        schedule_type=schedule_type,
        schedule=schedule,
        timezone=draft_timezone,
        rrule=draft.rrule or build_rrule(schedule_type, schedule),
        confidence=draft.confidence,
        reason=draft.reason,
        source_session_id=source_session_id,
    )


def due_scheduled_tasks(db: Session, now: datetime | None = None, limit: int = 10) -> list[ScheduledTask]:
    """扫描并领取到期的定时任务。

    使用乐观锁（``lease_until``）机制实现多 Worker 安全竞争：
    查找状态为 active 且 ``next_run_at <= now`` 且未被其他 Worker 领取的任务，
    通过 ``UPDATE ... SET lease_until`` 原子性地领取。

    Args:
        db: 数据库会话。
        now: 当前时间（默认为 UTC now）。
        limit: 最多领取的任务数。

    Returns:
        已成功领取的任务列表。
    """
    now = now or utc_now()
    candidate_ids = db.exec(
        select(ScheduledTask.id)
        .where(
            ScheduledTask.status == "active",
            ScheduledTask.next_run_at <= now,  # type: ignore[operator]
            or_(ScheduledTask.lease_until == None, ScheduledTask.lease_until < now),  # noqa: E711
        )
        .order_by(ScheduledTask.next_run_at)
        .limit(limit)
    ).all()
    lease_owner = f"{socket.gethostname()}:{new_id('worker')}"
    claimed: list[ScheduledTask] = []
    for task_id in candidate_ids:
        result = db.exec(
            update(ScheduledTask)
            .where(
                ScheduledTask.id == task_id,
                ScheduledTask.status == "active",
                ScheduledTask.next_run_at <= now,  # type: ignore[operator]
                or_(ScheduledTask.lease_until == None, ScheduledTask.lease_until < now),  # noqa: E711
            )
            .values(
                lease_owner=lease_owner,
                lease_until=now + timedelta(seconds=LEASE_SECONDS),
                updated_at=now,
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            continue
        row = db.get(ScheduledTask, task_id)
        if row:
            claimed.append(row)
    if claimed:
        db.commit()
        for row in claimed:
            db.refresh(row)
    return claimed


def execute_scheduled_task(
    db: Session,
    task: ScheduledTask,
    *,
    scheduled_for: datetime | None = None,
    manual: bool = False,
) -> ScheduledTaskRun:
    """同步执行定时任务。

    准备执行记录后，在当前线程中通过 AgentLoop 执行任务 prompt，
    阻塞直到执行完成。

    Args:
        db: 数据库会话。
        task: 待执行的定时任务。
        scheduled_for: 计划执行时间，默认取任务的 next_run_at。
        manual: 是否为手动触发。

    Returns:
        ScheduledTaskRun: 执行记录，包含结果摘要或错误信息。
    """
    scheduled_for = scheduled_for or task.next_run_at or utc_now()
    run = _prepare_scheduled_task_run(db, task, scheduled_for, manual)
    if run.status != "running" or not run.session_id:
        return run
    return _execute_prepared_scheduled_task(db, task, run, manual=manual)


def start_scheduled_task_async(
    db: Session,
    task: ScheduledTask,
    *,
    scheduled_for: datetime | None = None,
    manual: bool = False,
) -> ScheduledTaskRun:
    """异步启动定时任务执行。

    在当前线程准备执行记录和会话后，启动后台守护线程执行任务，
    立即返回执行记录（状态为 running）。

    Args:
        db: 数据库会话。
        task: 待执行的定时任务。
        scheduled_for: 计划执行时间，默认取任务的 next_run_at。
        manual: 是否为手动触发。

    Returns:
        ScheduledTaskRun: 执行记录（后台线程仍在运行）。
    """
    scheduled_for = scheduled_for or task.next_run_at or utc_now()
    run = _prepare_scheduled_task_run(db, task, scheduled_for, manual)
    if run.status == "running" and run.session_id:
        threading.Thread(
            target=_execute_prepared_scheduled_task_in_background,
            args=(task.id, run.id, manual),
            daemon=True,
        ).start()
    return run


def _prepare_scheduled_task_run(
    db: Session,
    task: ScheduledTask,
    scheduled_for: datetime,
    manual: bool,
) -> ScheduledTaskRun:
    """准备定时任务执行：检查并发策略、创建执行记录和独立会话。

    如果同一 scheduled_for 已存在执行记录则直接返回（幂等）。
    若并发策略为 forbid 且有正在执行的记录，则标记为 skipped。
    否则创建 running 记录并创建独立 ChatSession。

    Args:
        db: 数据库会话。
        task: 定时任务行。
        scheduled_for: 计划执行时间。
        manual: 是否为手动触发。

    Returns:
        ScheduledTaskRun: 准备好的执行记录（running/skipped/已存在）。
    """
    existing = db.exec(
        select(ScheduledTaskRun).where(
            ScheduledTaskRun.scheduled_task_id == task.id,
            ScheduledTaskRun.scheduled_for == scheduled_for,
        )
    ).first()
    if existing:
        return existing
    if task.concurrency_policy == "forbid":
        running = db.exec(
            select(ScheduledTaskRun).where(
                ScheduledTaskRun.scheduled_task_id == task.id,
                ScheduledTaskRun.status == "running",
            )
        ).first()
        if running:
            run = _create_run(db, task, scheduled_for, "skipped")
            run.error = "上一轮自动任务仍在执行，已按 forbid 策略跳过本次唤醒。"
            run.finished_at = utc_now()
            _finish_task_schedule(db, task, scheduled_for, "skipped", manual)
            db.add(run)
            db.commit()
            db.refresh(run)
            return run

    run = _create_run(db, task, scheduled_for, "running")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.exec(
            select(ScheduledTaskRun).where(
                ScheduledTaskRun.scheduled_task_id == task.id,
                ScheduledTaskRun.scheduled_for == scheduled_for,
            )
        ).first()
        if existing:
            return existing
        raise
    db.refresh(run)
    session = ChatSession(
        id=new_id("session"),
        tenant_id=task.tenant_id,
        user_id=task.created_by_user_id,
        agent_id=task.agent_id,
        title=f"自动任务：{task.title}",
        status="active",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    run.session_id = session.id
    run.updated_at = utc_now()
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _execute_prepared_scheduled_task_in_background(task_id: str, run_id: str, manual: bool) -> None:
    """在后台线程中执行准备好的定时任务。

    使用独立的数据库会话重新加载任务和执行记录，然后委托给
    ``_execute_prepared_scheduled_task`` 完成实际执行。

    Args:
        task_id: 定时任务 ID。
        run_id: 执行记录 ID。
        manual: 是否为手动触发。
    """
    with Session(engine) as db:
        task = db.get(ScheduledTask, task_id)
        run = db.get(ScheduledTaskRun, run_id)
        if not task or not run:
            return
        _execute_prepared_scheduled_task(db, task, run, manual=manual)


def _execute_prepared_scheduled_task(
    db: Session,
    task: ScheduledTask,
    run: ScheduledTaskRun,
    *,
    manual: bool,
) -> ScheduledTaskRun:
    """执行准备好的定时任务，通过 AgentLoop 流式处理。

    构造 ChatTurnRequest，逐条消费 AgentLoop 的流式输出并记录事件，
    成功后更新执行记录状态和 trace，失败时记录错误。
    最终释放乐观锁并更新任务调度。

    Args:
        db: 数据库会话。
        task: 定时任务行。
        run: 执行记录行。
        manual: 是否为手动触发。

    Returns:
        ScheduledTaskRun: 更新后的执行记录。
    """
    try:
        if not run.session_id:
            raise RuntimeError("自动任务缺少独立会话")
        request = ChatTurnRequest(
            tenant_id=task.tenant_id,
            session_id=run.session_id,
            agent_id=task.agent_id,
            user_id=task.created_by_user_id,
            message=automatic_task_message(task),
            channel="scheduled_task",
            interaction_mode="scheduled_task",
            client_timezone=task.timezone,
        )
        result: ChatTurnResponse | None = None
        for seq, item in enumerate(AgentLoop(db).handle_turn_stream(request), start=1):
            _record_scheduled_task_stream_event(db, run, run.session_id, seq, item)
            if item.get("event") in {"complete", "done"} and isinstance(item.get("data"), dict):
                result = ChatTurnResponse.model_validate(item["data"])
        if result is None:
            raise RuntimeError("自动任务执行未返回完整结果")
        run.status = "succeeded"
        run.result_summary = result.reply[:500]
        run.trace_json = {
            "router_decision": result.router_decision.model_dump(mode="json")
            if result.router_decision
            else None,
            "session_state": result.session_state.model_dump(mode="json"),
        }
        run.finished_at = utc_now()
        _finish_task_schedule(db, task, run.scheduled_for, "succeeded", manual)
    except Exception as exc:
        run.status = "failed"
        run.error = str(exc)
        run.finished_at = utc_now()
        if run.session_id:
            _record_scheduled_task_stream_event(
                db,
                run,
                run.session_id,
                0,
                {"event": "error", "data": {"message": str(exc), "sessionId": run.session_id}},
            )
        _finish_task_schedule(db, task, run.scheduled_for, "failed", manual)
    finally:
        task.lease_owner = None
        task.lease_until = None
        run.updated_at = utc_now()
        task.updated_at = utc_now()
        db.add(task)
        db.add(run)
        db.commit()
        db.refresh(run)
    return run


def _record_scheduled_task_stream_event(
    db: Session,
    run: ScheduledTaskRun,
    session_id: str,
    seq: int,
    item: dict[str, Any],
) -> None:
    """将 AgentLoop 流式输出的单个事件持久化为 AgentEvent。

    Args:
        db: 数据库会话。
        run: 执行记录行。
        session_id: 关联的会话 ID。
        seq: 事件序号。
        item: 流式事件字典，含 event 和 data 字段。
    """
    event = str(item.get("event") or "")
    data = item.get("data")
    if not isinstance(data, dict):
        data = {"value": data}
    payload = dict(data)
    payload.setdefault("sessionId", session_id)
    db.add(
        AgentEvent(
            tenant_id=run.tenant_id,
            session_id=session_id,
            event_type="scheduled_task_stream_event",
            payload_json={
                "run_id": run.id,
                "seq": seq,
                "event": event,
                "data": payload,
            },
            created_at=utc_now(),
        )
    )
    run.updated_at = utc_now()
    db.add(run)
    db.commit()


def automatic_task_message(task: ScheduledTask) -> str:
    """生成定时任务执行时发送给 agent 的消息内容。

    优先使用 prompt 字段，为空时回退到任务标题。

    Args:
        task: 定时任务行。

    Returns:
        str: 执行消息内容。
    """
    return task.prompt.strip() or task.title


def compute_next_run_at(task: ScheduledTask, after: datetime | None = None) -> datetime | None:
    """根据调度类型和时区计算下一次执行时间。

    支持 once/daily/weekly/monthly 四种类型：
    - once：仅在指定时间尚未过期时返回该时间。
    - daily：返回指定时间点的下一个日期。
    - weekly：返回指定星期几和时间点的最近未来时刻。
    - monthly：返回指定日期和时间点的最近未来月份。

    Args:
        task: 定时任务行。
        after: 计算基准时间，默认为当前 UTC 时间。

    Returns:
        datetime | None: 下次执行时间（UTC naive），无后续执行则返回 None。
    """
    if task.schedule_type == "once":
        run_at = parse_user_datetime(str((task.schedule_json or {}).get("run_at") or ""), task.timezone)
        return run_at if run_at and run_at > (after or utc_now()) else None
    after_local = _to_local(after or utc_now(), task.timezone)
    schedule = task.schedule_json or {}
    if task.schedule_type == "daily":
        candidate = datetime.combine(after_local.date(), _parse_time(str(schedule.get("time") or DEFAULT_TASK_TIME)))
        candidate = candidate.replace(tzinfo=_tz(task.timezone))
        if candidate <= after_local:
            candidate += timedelta(days=1)
        return _to_utc_naive(candidate)
    if task.schedule_type == "weekly":
        weekdays = _normalize_weekdays(schedule.get("weekdays") or [after_local.weekday()])
        target_time = _parse_time(str(schedule.get("time") or DEFAULT_TASK_TIME))
        best: datetime | None = None
        for offset in range(0, 8):
            day = after_local.date() + timedelta(days=offset)
            if day.weekday() not in weekdays:
                continue
            candidate = datetime.combine(day, target_time).replace(tzinfo=_tz(task.timezone))
            if candidate <= after_local:
                continue
            if not best or candidate < best:
                best = candidate
        return _to_utc_naive(best) if best else None
    if task.schedule_type == "monthly":
        target_time = _parse_time(str(schedule.get("time") or DEFAULT_TASK_TIME))
        day_of_month = _normalize_day_of_month(schedule.get("day_of_month") or 1)
        year = after_local.year
        month = after_local.month
        for _ in range(14):
            day = min(day_of_month, calendar.monthrange(year, month)[1])
            candidate = datetime(year, month, day, target_time.hour, target_time.minute, tzinfo=_tz(task.timezone))
            if candidate > after_local:
                return _to_utc_naive(candidate)
            month += 1
            if month > 12:
                year += 1
                month = 1
    return None


def normalize_schedule(schedule_type: str, schedule: dict[str, Any], timezone: str) -> dict[str, Any]:
    """规范化调度配置，校验并补全必填字段。

    根据调度类型提取并验证对应字段（once 的 run_at、daily 的 time、
    weekly 的 time+weekdays、monthly 的 time+day_of_month）。

    Args:
        schedule_type: 调度类型（once/daily/weekly/monthly）。
        schedule: 原始调度配置字典。
        timezone: 时区字符串。

    Returns:
        dict[str, Any]: 规范化后的调度配置。

    Raises:
        HTTPException: 调度类型不支持或必填字段缺失时抛出。
    """
    schedule_type = _normalize_schedule_type(schedule_type)
    _tz(timezone)
    raw = schedule or {}
    if schedule_type == "once":
        run_at = raw.get("run_at") or raw.get("datetime") or raw.get("start_at")
        parsed = parse_user_datetime(str(run_at or ""), timezone)
        if not parsed:
            raise HTTPException(status_code=400, detail="一次性自动任务需要填写执行时间")
        return {"run_at": _to_local(parsed, timezone).isoformat()}
    if schedule_type == "daily":
        return {"time": _format_time(_parse_time(str(raw.get("time") or DEFAULT_TASK_TIME)))}
    if schedule_type == "weekly":
        return {
            "time": _format_time(_parse_time(str(raw.get("time") or DEFAULT_TASK_TIME))),
            "weekdays": _normalize_weekdays(raw.get("weekdays") or [0]),
        }
    if schedule_type == "monthly":
        return {
            "time": _format_time(_parse_time(str(raw.get("time") or DEFAULT_TASK_TIME))),
            "day_of_month": _normalize_day_of_month(raw.get("day_of_month") or 1),
        }
    raise HTTPException(status_code=400, detail="不支持的自动任务调度类型")


def build_rrule(schedule_type: str, schedule: dict[str, Any]) -> str | None:
    """根据调度类型和配置生成 RRULE 字符串。

    once 类型返回 None（不重复），其他类型生成对应的 RFC 5545 RRULE。

    Args:
        schedule_type: 调度类型。
        schedule: 调度配置字典。

    Returns:
        str | None: RRULE 字符串，once 类型返回 None。
    """
    time_text = str(schedule.get("time") or DEFAULT_TASK_TIME)
    hour, minute = time_text.split(":", 1)
    if schedule_type == "once":
        return None
    if schedule_type == "daily":
        return f"FREQ=DAILY;BYHOUR={int(hour)};BYMINUTE={int(minute)};BYSECOND=0"
    if schedule_type == "weekly":
        byday = ",".join(["MO", "TU", "WE", "TH", "FR", "SA", "SU"][int(day)] for day in schedule.get("weekdays", [0]))
        return f"FREQ=WEEKLY;BYDAY={byday};BYHOUR={int(hour)};BYMINUTE={int(minute)};BYSECOND=0"
    if schedule_type == "monthly":
        return (
            f"FREQ=MONTHLY;BYMONTHDAY={int(schedule.get('day_of_month') or 1)};"
            f"BYHOUR={int(hour)};BYMINUTE={int(minute)};BYSECOND=0"
        )
    return None


def parse_user_datetime(value: str, timezone: str = DEFAULT_TIMEZONE) -> datetime | None:
    """将用户输入的日期时间字符串解析为 UTC naive datetime。

    支持带时区和不带时区的 ISO 格式，不带时区时按指定时区解释。

    Args:
        value: 日期时间字符串。
        timezone: 解析无时区字符串时使用的时区。

    Returns:
        datetime | None: UTC naive datetime，解析失败或空值返回 None。
    """
    text = (value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_tz(timezone))
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _create_run(db: Session, task: ScheduledTask, scheduled_for: datetime, status: str) -> ScheduledTaskRun:
    """创建定时任务执行记录行（未提交）。

    Args:
        db: 数据库会话。
        task: 定时任务行。
        scheduled_for: 计划执行时间。
        status: 初始状态（running/skipped）。

    Returns:
        ScheduledTaskRun: 新建但尚未 commit 的执行记录。
    """
    run = ScheduledTaskRun(
        tenant_id=task.tenant_id,
        scheduled_task_id=task.id,
        agent_id=task.agent_id,
        user_id=task.created_by_user_id,
        scheduled_for=scheduled_for,
        status=status,
        started_at=utc_now() if status == "running" else None,
    )
    db.add(run)
    return run


def _finish_task_schedule(db: Session, task: ScheduledTask, scheduled_for: datetime, status: str, manual: bool) -> None:
    """执行完成后更新任务调度状态。

    更新 last_run_at、last_status、run_count，并在非手动触发时
    计算下一次执行时间；达到 max_runs 或 end_at 时标记为 completed。

    Args:
        db: 数据库会话。
        task: 定时任务行。
        scheduled_for: 本次执行的计划时间。
        status: 本次执行状态（succeeded/failed/skipped）。
        manual: 是否为手动触发。
    """
    now = utc_now()
    task.last_run_at = now
    task.last_status = status
    task.run_count += 1
    if not manual:
        next_run = compute_next_run_at(task, after=scheduled_for + timedelta(seconds=1))
        if task.max_runs is not None and task.run_count >= task.max_runs:
            task.status = "completed"
            task.next_run_at = None
        elif task.end_at and next_run and next_run > task.end_at:
            task.status = "completed"
            task.next_run_at = None
        else:
            task.next_run_at = next_run
            if task.schedule_type == "once" and next_run is None:
                task.status = "completed"
    db.add(task)


def _detect_with_llm(
    db: Session,
    tenant_id: str,
    agent_id: str,
    message: str,
    timezone: str,
) -> _LLMScheduledTaskDraft | None:
    """调用 LLM 从用户消息中解析定时任务草案。

    使用 agent 对应的 LLM 模型，传入解析提示词和用户消息，
    返回结构化的草案对象。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。
        agent_id: agent ID。
        message: 用户消息。
        timezone: 用户时区。

    Returns:
        _LLMScheduledTaskDraft | None: LLM 解析的草案，模型不可用或解析失败返回 None。
    """
    model_config = model_for_agent(db, tenant_id, agent_id, "router") or model_for_agent(db, tenant_id, agent_id)
    if not model_config:
        return None
    try:
        with llm_operation("scheduled_task.detect"):
            raw = LLMClient(model_config).generate_json(
                SCHEDULE_DRAFT_PROMPT,
                {
                    "now": _to_local(utc_now(), timezone).isoformat(),
                    "default_timezone": timezone,
                    "user_message": message,
                },
            )
        return _LLMScheduledTaskDraft.model_validate(raw)
    except (LLMError, ValidationError):
        return None


def _execution_goal_from_message(message: str) -> str:
    """从原始消息中提取执行目标文本（去首尾空白）。

    Args:
        message: 原始用户消息。

    Returns:
        str: 去除空白后的消息文本。
    """
    return message.strip()


def _compact_title(message: str) -> str:
    """从消息中生成紧凑的任务标题（最多 28 字符）。

    合并多余空白、去除首尾标点后截取，为空时回退为"自动任务"。

    Args:
        message: 原始用户消息。

    Returns:
        str: 生成的标题文本。
    """
    text = _execution_goal_from_message(message)
    text = re.sub(r"\s+", " ", text).strip(" ，,。")
    return (text[:28] or "自动任务").strip()


def _normalize_schedule_type(value: str) -> str:
    """校验调度类型是否合法。

    Args:
        value: 调度类型字符串。

    Returns:
        str: 合法的调度类型。

    Raises:
        HTTPException: 不支持的调度类型时抛出 400。
    """
    if value not in SCHEDULE_TYPES:
        raise HTTPException(status_code=400, detail="不支持的自动任务调度类型")
    return value


def _normalize_weekdays(value: Any) -> list[int]:
    """规范化星期列表，校验取值范围为 0-6。

    Args:
        value: 星期值或列表（0=周一，6=周日）。

    Returns:
        list[int]: 去重排序后的星期列表。

    Raises:
        HTTPException: 星期值不在 0-6 范围时抛出 400。
    """
    if not isinstance(value, list):
        value = [value]
    days = sorted({int(item) for item in value if str(item).strip() != ""})
    if not days or any(day < 0 or day > 6 for day in days):
        raise HTTPException(status_code=400, detail="每周自动任务需要 0-6 的星期设置")
    return days


def _normalize_day_of_month(value: Any) -> int:
    """规范化月内日期，校验取值范围为 1-31。

    Args:
        value: 月内日期值。

    Returns:
        int: 合法的月内日期。

    Raises:
        HTTPException: 日期不在 1-31 范围时抛出 400。
    """
    day = int(value)
    if day < 1 or day > 31:
        raise HTTPException(status_code=400, detail="每月执行日需要在 1 到 31 之间")
    return day


def _parse_time(value: str) -> time:
    """将字符串解析为 time 对象，支持 H:mm 或 HH:mm 格式。

    Args:
        value: 时间字符串（如 "9:00" 或 "09:00"）。

    Returns:
        time: 解析后的时间对象。

    Raises:
        HTTPException: 格式不合法或时分超出范围时抛出 400。
    """
    text = value.strip()
    match = re.fullmatch(r"(\d{1,2})(?::(\d{1,2}))?", text)
    if not match:
        raise HTTPException(status_code=400, detail="时间格式需要为 HH:mm")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise HTTPException(status_code=400, detail="时间格式需要为 HH:mm")
    return time(hour, minute)


def _format_time(value: time) -> str:
    """将 time 对象格式化为 HH:mm 字符串。

    Args:
        value: time 对象。

    Returns:
        str: 格式化后的时间字符串（如 "09:30"）。
    """
    return f"{value.hour:02d}:{value.minute:02d}"


def _tz(value: str) -> ZoneInfo:
    """将时区字符串转换为 ZoneInfo 对象。

    Args:
        value: IANA 时区字符串，为空时使用默认时区。

    Returns:
        ZoneInfo: 时区对象。

    Raises:
        HTTPException: 时区无效时抛出 400。
    """
    try:
        return ZoneInfo(value or DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(status_code=400, detail="无效时区") from exc


def _safe_timezone(value: str | None, fallback: str = DEFAULT_TIMEZONE) -> str:
    """安全获取有效时区字符串，无效时回退到 fallback。

    Args:
        value: 待验证的时区字符串。
        fallback: 回退时区。

    Returns:
        str: 有效的时区字符串。
    """
    candidate = (value or "").strip() or fallback
    try:
        _tz(candidate)
        return candidate
    except HTTPException:
        _tz(fallback)
        return fallback


def _to_local(value: datetime, timezone: str) -> datetime:
    """将 UTC（或无时区）datetime 转换为指定时区的本地时间。

    Args:
        value: 原始 datetime，无时区时视为 UTC。
        timezone: 目标时区字符串。

    Returns:
        datetime: 转换后的带时区本地时间。
    """
    source = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return source.astimezone(_tz(timezone))


def _to_utc_naive(value: datetime) -> datetime:
    """将带时区的 datetime 转换为 UTC naive datetime。

    Args:
        value: 带时区的 datetime。

    Returns:
        datetime: UTC naive datetime（不含 tzinfo）。
    """
    return value.astimezone(UTC).replace(tzinfo=None)


def _nonempty(value: str, message: str, max_length: int) -> str:
    """校验字符串非空并截断到最大长度。

    Args:
        value: 原始字符串。
        message: 为空时的错误提示。
        max_length: 最大字符长度。

    Returns:
        str: 去除首尾空白并截断后的字符串。

    Raises:
        HTTPException: 字符串为空时抛出 400。
    """
    text = (value or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail=message)
    return text[:max_length]


def _dt(value: datetime | None) -> str | None:
    """将 datetime 转换为 ISO 格式字符串，None 值保持不变。

    Args:
        value: datetime 对象或 None。

    Returns:
        str | None: ISO 格式字符串或 None。
    """
    return value.isoformat() if value else None


def _ensure_agent_access(db: Session, tenant_id: str, agent_id: str, current_user: User) -> AgentProfile:
    """校验当前用户是否有权为指定 agent 设置定时任务。

    管理员、agent 所有者或 agent 已发布到广场的用户有权限。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。
        agent_id: agent ID。
        current_user: 当前登录用户。

    Returns:
        AgentProfile: 校验通过的 agent 档案。

    Raises:
        HTTPException: agent 不存在（404）或无权限（403）时抛出。
    """
    agent = db.get(AgentProfile, agent_id)
    if not agent or agent.tenant_id != tenant_id or agent.is_overall or agent.status != "active":
        raise HTTPException(status_code=404, detail="员工不可用")
    if _is_admin_user(current_user):
        return agent
    metadata = agent.metadata_json or {}
    owns_agent = _agent_owned_by_user(agent, current_user)
    in_gallery = metadata.get("published_to_gallery") is True
    if not (owns_agent or in_gallery):
        raise HTTPException(status_code=403, detail="无权为该员工设置自动任务")
    return agent


def _ensure_task_access(row: ScheduledTask, current_user: User) -> None:
    """校验当前用户是否有权访问指定定时任务。

    管理员或任务创建者有权限。

    Args:
        row: 定时任务行。
        current_user: 当前登录用户。

    Raises:
        HTTPException: 非创建者且非管理员时抛出 403。
    """
    if _is_admin_user(current_user):
        return
    if row.created_by_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权访问该自动任务")

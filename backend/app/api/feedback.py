"""用户反馈分析 API 模块。

提供对聊天消息反馈（点赞/点踩）的聚合查询和重新分析功能。

- ``GET /api/enterprise/feedback/summary``：获取反馈汇总统计（总数、点赞数、
  点踩数、归因桶分布、Top 摘要等）。
- ``GET /api/enterprise/feedback/sessions``：按评分列出反馈会话列表，
  支持按 agent 过滤，返回每会话的反馈统计和最新分析结果。
- ``GET /api/enterprise/feedback/sessions/{session_id}``：获取指定会话的
  反馈详情（含消息列表和反馈列表）。
- ``POST /api/enterprise/feedback/{feedback_id}/reanalyze``：重新触发
  指定反馈的 LLM 归因分析。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.api.chat import message_read, session_read
from app.db import get_session
from app.db.models import ChatSession, Message, MessageFeedback, User, utc_now
from app.feedback import FEEDBACK_BUCKET_LABELS, enqueue_feedback_analysis, feedback_analysis_read, feedback_summary
from app.security.auth import get_current_user
from app.security.tenant import ensure_tenant

router = APIRouter(prefix="/api/enterprise/feedback", tags=["enterprise:feedback"])


# 0. 函数说明：汇总反馈统计（按评分、桶、状态分组），返回总体反馈仪表盘数据
#    URL：GET /api/feedback/summary
@router.get("/summary")
def get_feedback_summary(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=5000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    # 1. 鉴权：校验租户匹配
    _ensure_request_tenant(tenant_id, current_user)
    # 2. 校验租户存在
    ensure_tenant(db, tenant_id)
    # 3. 取出当前用户有权限的会话ID集合（按agent_id可选过滤）
    owned_session_ids = _owned_session_ids(db, tenant_id, current_user, agent_id)
    # 4. 没有可用会话：返回空汇总（避免不必要的DB查询）
    if not owned_session_ids:
        return feedback_summary([])
    # 5. 批量查反馈（按租户+会话集合，按更新时间倒序，取前limit条）
    rows = list(
        db.exec(
            select(MessageFeedback)
            .where(
                MessageFeedback.tenant_id == tenant_id,
                MessageFeedback.session_id.in_(owned_session_ids),  # type: ignore[attr-defined]   # 批量过滤会话
            )
            .order_by(MessageFeedback.updated_at.desc())
            .limit(limit)
        ).all()
    )
    # 6. 调用feedback_summary聚合统计后返回
    return feedback_summary(rows)


# 0. 函数说明：列出有反馈的会话（默认只列"踩"反馈），按会话聚合分析结果
#    URL：GET /api/feedback/sessions
@router.get("/sessions")
def list_feedback_sessions(
    tenant_id: str = Query(...),
    rating: str = Query(default="down"),
    agent_id: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[dict]:
    # 1. 鉴权：校验租户匹配
    _ensure_request_tenant(tenant_id, current_user)
    # 2. 校验租户存在
    ensure_tenant(db, tenant_id)
    # 3. 取出当前用户有权限的会话ID集合（按agent_id可选过滤）
    owned_session_ids = _owned_session_ids(db, tenant_id, current_user, agent_id)
    # 4. 没有可用会话：直接返回空列表（避免不必要的DB查询）
    if not owned_session_ids:
        return []
    # 5. 按条件查反馈：租户 + 评分（默认"down"=踩）+ 会话ID集合
    feedback_rows = list(
        db.exec(
            select(MessageFeedback)
            .where(
                MessageFeedback.tenant_id == tenant_id,
                MessageFeedback.rating == rating,
                MessageFeedback.session_id.in_(owned_session_ids),  # type: ignore[attr-defined]   # 批量过滤会话
            )
            .order_by(MessageFeedback.updated_at.desc())
            .limit(limit)
        ).all()
    )
    # 6. 按会话ID分组：把同一会话的多条反馈收集到一起
    grouped: dict[str, list[MessageFeedback]] = {}
    for row in feedback_rows:
        grouped.setdefault(row.session_id, []).append(row)

    # 7. 构造每个会话的汇总行
    results: list[dict] = []
    for session_id, rows in grouped.items():
        # 7.1 校验会话存在 + 租户归属
        chat_session = db.get(ChatSession, session_id)
        if not chat_session or chat_session.tenant_id != tenant_id:
            continue    # 会话不存在或租户不对 → 跳过
        # 7.2 校验用户归属（只能看自己的会话）
        if chat_session.user_id != current_user.id:
            continue
        # 7.3 校验agent过滤
        if agent_id and chat_session.agent_id != agent_id:
            continue
        # 7.4 找最新一条反馈
        latest = max(rows, key=lambda item: item.updated_at)
        # 7.5 最新反馈的分析详情
        latest_analysis = feedback_analysis_read(latest)
        # 7.6 查最新反馈对应的消息原文（用于前端展示）
        latest_message = db.get(Message, latest.message_id)
        # 7.7 查会话所属用户（用于前端展示用户名）
        user = db.get(User, chat_session.user_id) if chat_session.user_id else None
        # 7.8 统计"踩"反馈（按分析桶分类）
        down_rows = [item for item in rows if item.rating == "down"]
        bucket_counts: dict[str, int] = {}
        for item in down_rows:
            bucket = item.analysis_bucket or "unknown"    # 没分桶就归到"unknown"
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        # 7.9 取出现次数最多的桶作为primary_bucket
        primary_bucket = max(bucket_counts.items(), key=lambda item: item[1])[0] if bucket_counts else None
        # 7.10 追加会话汇总行
        results.append(
            {
                "session_id": chat_session.id,
                "tenant_id": chat_session.tenant_id,
                "agent_id": chat_session.agent_id,
                "user_id": chat_session.user_id,
                "username": user.username if user else None,           # 用户名（可能为空）
                "display_name": user.display_name if user else None,    # 显示名（可能为空）
                "title": chat_session.title,
                "summary": chat_session.summary,
                "status": chat_session.status,
                "feedback_count": len(rows),                           # 该会话的反馈总数
                "latest_feedback_at": latest.updated_at.isoformat(),
                "latest_message_id": latest.message_id,
                "latest_message": latest_message.content if latest_message else "",
                "analysis_status": latest_analysis["status"],
                "analysis_bucket": latest_analysis["bucket"],
                "analysis_bucket_label": latest_analysis["bucket_label"],
                "analysis_summary": latest_analysis["summary"],
                "primary_bucket": primary_bucket,                       # 主要问题桶（出现最多的）
                "primary_bucket_label": FEEDBACK_BUCKET_LABELS.get(primary_bucket or "unknown", primary_bucket or "unknown"),  # 桶的中文标签
                "bucket_counts": bucket_counts,                         # 各桶详细计数
                "updated_at": chat_session.updated_at.isoformat(),
            }
        )
    # 8. 按最新反馈时间倒序返回
    return sorted(results, key=lambda item: item["latest_feedback_at"], reverse=True)


# 0. 函数说明：获取某个会话的反馈详情（会话信息+所有消息+所有反馈+每条消息的反馈）
#    URL：GET /api/feedback/sessions/{session_id}
@router.get("/sessions/{session_id}")
def get_feedback_session_detail(
    session_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    # 1. 鉴权：校验租户匹配
    _ensure_request_tenant(tenant_id, current_user)
    # 2. 校验租户存在
    ensure_tenant(db, tenant_id)
    # 3. 校验会话存在且归属当前用户（权限校验）
    chat_session = _get_owned_chat_session(db, tenant_id, current_user, session_id)

    # 4. 取出该会话所有消息（按时间正序）
    messages = list(
        db.exec(
            select(Message)
            .where(Message.tenant_id == tenant_id, Message.session_id == session_id)
            .order_by(Message.created_at)
        ).all()
    )
    # 5. 取出该会话所有反馈（按更新时间倒序）
    feedback_rows = list(
        db.exec(
            select(MessageFeedback)
            .where(MessageFeedback.tenant_id == tenant_id, MessageFeedback.session_id == session_id)
            .order_by(MessageFeedback.updated_at.desc())
        ).all()
    )
    # 6. 构造"message_id → feedback"的索引（便于O(1)查找）
    feedback_by_message = {row.message_id: row for row in feedback_rows}
    # 7. 查会话所属用户（用于前端展示）
    user = db.get(User, chat_session.user_id) if chat_session.user_id else None
    # 8. 拼接返回：会话信息 + 消息+反馈详情 + 反馈列表
    return {
        "session": {
            **session_read(chat_session).model_dump(),
            "username": user.username if user else None,           # 用户名（可空）
            "display_name": user.display_name if user else None,    # 显示名（可空）
        },
        # 8.1 每条消息附上对应feedback（用索引查找O(1)）
        "messages": [_message_with_feedback(message, feedback_by_message.get(message.id)) for message in messages],
        # 8.2 所有反馈的列表（按更新时间倒序）
        "feedback": [
            {
                "id": row.id,
                "message_id": row.message_id,
                "user_id": row.user_id,
                "rating": row.rating,
                "analysis": feedback_analysis_read(row),    # 分析详情
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
            }
            for row in feedback_rows
        ],
    }


# 0. 函数说明：对一条反馈重新跑LLM分析（清空旧分析结果+重新入队分析任务）
#    URL：POST /api/feedback/{feedback_id}/reanalyze
@router.post("/{feedback_id}/reanalyze")
def reanalyze_feedback(
    feedback_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    # 1. 鉴权：校验租户匹配
    _ensure_request_tenant(tenant_id, current_user)
    # 2. 校验租户存在
    ensure_tenant(db, tenant_id)
    # 3. 按主键查反馈 + 校验租户归属（找不到或不对都返回404）
    row = db.get(MessageFeedback, feedback_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Feedback not found")    # 不存在/不是这个租户的 → 404
    # 4. 校验关联会话的归属（防止越权触发别人的会话分析）
    _get_owned_chat_session(db, tenant_id, current_user, row.session_id)
    # 5. 清空旧的分析结果，标记为pending（重新跑分析）
    now = utc_now()
    row.analysis_status = "pending"                                # 状态：待分析
    row.analysis_bucket = None                                     # 清空分析桶
    row.analysis_reason = None                                     # 清空分析原因
    row.analysis_summary = None                                    # 清空分析摘要
    row.analysis_confidence = None                                 # 清空分析置信度
    row.analysis_json = {"retry_requested_at": now.isoformat()}    # 记录重试时间
    row.analyzed_at = None                                         # 清空分析时间
    row.updated_at = now                                           # 更新时间
    db.add(row)
    db.commit()
    db.refresh(row)
    # 6. 异步入队分析任务（LLM重新分析）
    job = enqueue_feedback_analysis(row.tenant_id, row.id, row.session_id)
    # 7. 返回job_id供前端轮询
    return {
        "feedback_id": row.id,
        "analysis_status": row.analysis_status,    # 现在是pending
        "job_id": job.id,                          # 异步分析任务ID
        "updated_at": row.updated_at.isoformat(),
    }


# 0. 函数说明：把Message转成DTO并附加feedback详情（合并成单条响应行）
#    URL：无（辅助函数）
def _message_with_feedback(message: Message, feedback: MessageFeedback | None) -> dict:
    # 1. 把Message转成MessageRead DTO并dump成dict（带上feedback rating）
    payload = message_read(message, feedback.rating if feedback else None).model_dump()
    # 2. 有反馈时：附加3个反馈相关字段
    if feedback:
        payload["feedback_id"] = feedback.id                                  # 反馈ID
        payload["feedback_updated_at"] = feedback.updated_at.isoformat()       # 反馈更新时间
        payload["feedback_analysis"] = feedback_analysis_read(feedback)        # 分析结果详情
    return payload


# 0. 函数说明：取出当前用户在该租户下所有"属于自己的"ChatSession ID（按agent_id可选过滤）
#    URL：无（辅助函数）
def _owned_session_ids(
    db: Session,
    tenant_id: str,
    current_user: User,
    agent_id: str | None = None,
) -> list[str]:
    # 1. 构造基础条件：租户 + 当前用户
    conditions = [ChatSession.tenant_id == tenant_id, ChatSession.user_id == current_user.id]
    # 2. 如果指定了agent_id：追加agent过滤
    if agent_id:
        conditions.append(ChatSession.agent_id == agent_id)
    # 3. 只查ID字段（节省带宽）+ 返回所有匹配会话的ID列表
    return list(db.exec(select(ChatSession.id).where(*conditions)).all())


# 0. 函数说明：按主键查ChatSession + 校验当前用户是会话所有者（否则404）
#    URL：无（辅助函数）
def _get_owned_chat_session(db: Session, tenant_id: str, current_user: User, session_id: str) -> ChatSession:
    # 1. 按主键查ChatSession
    row = db.get(ChatSession, session_id)
    # 2. 多维校验：存在 + 租户归属 + 用户归属（任一不符 → 404）
    if not row or row.tenant_id != tenant_id or row.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Session not found")    # 不是这个租户的/不是这个用户的 → 404
    return row


# 0. 函数说明：校验请求的tenant_id和当前用户的tenant_id一致（防跨租户越权）
#    URL：无（辅助函数）
def _ensure_request_tenant(tenant_id: str, current_user: User) -> None:
    # 1. 请求的租户ID必须和当前用户的租户ID一致（不一致 → 403 越权）
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")    # 跨租户访问 → 403

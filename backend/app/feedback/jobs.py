"""反馈分析异步任务模块。

将用户反馈（点赞/点踩）的分析流程封装为异步任务，
避免在用户交互路径中阻塞等待 LLM 调用。
任务通过 ``enqueue_feedback_analysis`` 提交到异步队列，
由 ``run_feedback_analysis_job`` 在后台执行。
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

from app.async_jobs import AsyncJob, enqueue_async_job
from app.db import engine
from app.feedback.service import FeedbackAnalysisService
from app.observability import EventLog


def enqueue_feedback_analysis(tenant_id: str, feedback_id: str, session_id: str | None = None) -> AsyncJob:
    """提交一条反馈分析的异步任务到任务队列。

    Args:
        tenant_id: 租户 ID。
        feedback_id: 反馈记录 ID。
        session_id: 关联的会话 ID（可选，用于事件记录）。

    Returns:
        已入队的 ``AsyncJob`` 实例。
    """
    return enqueue_async_job(
        "feedback.analyze",
        run_feedback_analysis_job,
        {"tenant_id": tenant_id, "feedback_id": feedback_id, "session_id": session_id},
        metadata={"tenant_id": tenant_id, "feedback_id": feedback_id, "session_id": session_id},
    )


def run_feedback_analysis_job(payload: dict[str, Any]) -> None:
    """执行反馈分析异步任务。

    在独立的数据库会话中调用 ``FeedbackAnalysisService`` 对指定反馈进行 LLM 归因分析，
    分析完成后记录事件。如果反馈记录不存在，则记录错误事件。

    Args:
        payload: 任务负载，包含 ``tenant_id``、``feedback_id`` 和可选的 ``session_id``。
    """
    tenant_id = str(payload.get("tenant_id") or "")
    feedback_id = str(payload.get("feedback_id") or "")
    session_id = str(payload.get("session_id") or "")
    with Session(engine) as db:
        events = EventLog(db)
        row = FeedbackAnalysisService(db).analyze_feedback(feedback_id)
        if row:
            events.record(
                row.tenant_id,
                row.session_id,
                "feedback_analysis_completed",
                {
                    "feedback_id": row.id,
                    "message_id": row.message_id,
                    "rating": row.rating,
                    "bucket": row.analysis_bucket,
                    "status": row.analysis_status,
                    "confidence": row.analysis_confidence,
                },
            )
            db.commit()
            return
        # 反馈记录不存在，记录错误事件
        if tenant_id and session_id:
            events.record(
                tenant_id,
                session_id,
                "feedback_analysis_error",
                {"feedback_id": feedback_id, "message": "Feedback row not found"},
            )
            db.commit()

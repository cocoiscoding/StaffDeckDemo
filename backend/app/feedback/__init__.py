"""反馈分析包。

提供用户反馈数据的采集、分桶（bucket）分类、摘要生成与异步分析能力，
支持通过后台任务对反馈进行结构化处理。

导出对象:
    FEEDBACK_BUCKET_LABELS: 反馈分桶标签的预定义集合。
    FeedbackAnalysisService: 反馈分析服务，封装核心业务逻辑。
    enqueue_feedback_analysis: 将反馈分析任务投递到后台队列。
    feedback_analysis_read: 读取反馈分析结果。
    feedback_summary: 生成反馈摘要。
"""

from app.feedback.jobs import enqueue_feedback_analysis
from app.feedback.service import (
    FEEDBACK_BUCKET_LABELS,
    FeedbackAnalysisService,
    feedback_analysis_read,
    feedback_summary,
)

__all__ = [
    "FEEDBACK_BUCKET_LABELS",
    "FeedbackAnalysisService",
    "enqueue_feedback_analysis",
    "feedback_analysis_read",
    "feedback_summary",
]

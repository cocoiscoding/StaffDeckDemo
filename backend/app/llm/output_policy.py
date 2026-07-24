"""LLM 输出令牌策略模块。

定义各业务操作（operation）对应的最大输出令牌（output tokens）上限。
内部控制面调用不继承用户可见回复的预算，而长文本/代码生成类操作则保留更大的配额。
"""

from __future__ import annotations


# 内部控制面调用不应继承用户可见回复的预算。
# 长文本/代码生成类操作有意保留更大的配额。
#: 各操作类型对应的输出令牌上限映射表。
#: key 为操作标识符（如 "response.generate"），value 为允许的最大输出令牌数。
OPERATION_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "router.scene": 4096,  # 场景路由：判断用户意图属于哪个处理场景
    "step_agent.run": 4096,  # 步骤代理执行：单步推理与工具调用
    "step_agent.repair": 4096,  # 步骤代理修复：纠正上一步的错误输出
    "response.generate": 4096,  # 回复生成：非流式的最终用户回复
    "response.generate_stream": 4096,  # 流式回复生成
    "context.compact": 2048,  # 上下文压缩：精简过长的对话历史
    "reflection.review": 2048,  # 反思审查：对生成结果进行自检
    "general_skill.select": 2048,  # 通用技能选择：匹配最佳通用技能
    "general_skill.plan": 8192,  # 通用技能规划：生成分步执行计划（需较大配额）
    "general_skill.repair": 8192,  # 通用技能修复：纠正计划或输出错误
    "general_skill.review": 2048,  # 通用技能审查
    "general_skill.reply": 2048,  # 通用技能最终回复
    "knowledge.document_route": 2048,  # 知识文档路由
    "knowledge.bucket_route": 512,  # 知识分桶路由（轻量判断，配额较小）
    "knowledge.discovery": 4096,  # 知识发现：从文档中提取建议
    "knowledge.ingest_bucket": 8192,  # 知识分桶摄入：生成分桶摘要（需较大配额）
    "memory.capture": 1024,  # 记忆捕获：从对话中提取关键事实
    "session.title": 512,  # 会话标题生成（轻量操作）
    "scheduled_task.detect": 1024,  # 定时任务检测：从消息中识别时间意图
    "feedback.analyze": 1024,  # 反馈分析：对用户反馈进行分类
}

def operation_output_tokens(operation: str, configured_tokens: int) -> int:
    """根据操作类型计算实际允许的输出令牌数。

    将用户配置的令牌上限与该操作类型预定义的上限取较小值，
    确保内部控制面操作不会消耗过多的输出预算。

    Args:
        operation: 操作类型标识符，例如 ``"response.generate"``、``"memory.capture"``。
        configured_tokens: 用户配置的默认输出令牌上限。

    Returns:
        实际允许的最大输出令牌数，取 ``configured_tokens`` 与操作上限中的较小值；
        若操作类型未在映射表中注册，则直接返回 ``configured_tokens``。
    """
    configured = max(1, int(configured_tokens or 1))  # 确保至少为 1，避免传 0 或 None
    limit = OPERATION_MAX_OUTPUT_TOKENS.get(operation)
    return configured if limit is None else min(configured, limit)

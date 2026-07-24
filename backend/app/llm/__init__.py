"""大语言模型（LLM）客户端包。

封装对底层大语言模型的调用，统一处理鉴权、协议适配、错误重试与流式输出，
对上层屏蔽不同模型供应商的差异。

导出对象:
    LLMClient: LLM 客户端，封装模型调用与响应解析。
    LLMError: LLM 调用过程中抛出的异常类型。
"""

from app.llm.client import LLMClient, LLMError

__all__ = ["LLMClient", "LLMError"]

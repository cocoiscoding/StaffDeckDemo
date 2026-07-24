"""知识库包。

提供知识库的版本管理、文档摄入（ingest）、分桶（bucket）、分块（chunk）、
概念抽取（concept）与发现建议（discovery）等完整知识管理能力。

导出对象:
    KnowledgeService: 知识库核心服务，封装知识摄入、检索与管理的全部业务逻辑。
"""

from app.knowledge.service import KnowledgeService

__all__ = ["KnowledgeService"]

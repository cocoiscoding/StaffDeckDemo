"""知识检索核心服务模块。

本模块实现了 StaffDeck 知识管理系统的核心业务逻辑，涵盖：

1. **知识入库（Ingest）**：从上传的文件中解析文本、构建章节导航树、通过 LLM 规划
   OKF（Open Knowledge Framework）Wiki 页面、生成引用来源（Chunk）、发现可确认的
   SOP / 工具建议。入库过程是多阶段的、可取消的、有完整进度追踪的。
2. **知识检索（Search）**：支持基于 LLM 或词法的相关性路由，按 文档 → 内部索引
   （Bucket）→ 章节展开 → 引用来源（Chunk）→ 证据包（Evidence Pack）的流水线
   返回结构化检索结果。
3. **知识发现（Discovery）**：在入库过程中自动从文档内容发现可确认的技能（Skill）
   和工具（Tool）建议，进入人工审核队列。

核心概念：
- **KnowledgeDocument**：一篇被入库的原始文档（文件）。
- **KnowledgeBucket**：文档被拆分后的「内部索引」或「知识主题」，一个文档对应多个。
- **KnowledgeChunk**：从 Bucket 中进一步切出的「引用来源」片段，供检索引用。
- **KnowledgeConcept**：OKF 概念层中的 Wiki 页面，是面向检索的语义单元。
- **KnowledgeDiscoverySuggestion**：入库过程中自动发现的技能 / 工具建议。

典型调用路径：
    service = KnowledgeService(db)
    job = service.create_ingest_job(payload)
    service.run_ingest_job(job.id)
    response = service.search(request, model_config)
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import paths
from app.db import engine
from app.db.models import (
    KnowledgeBucket,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    KnowledgeIngestJob,
    ModelConfig,
    Skill,
    Tool,
    utc_now,
)
from app.knowledge.parser import KnowledgeParseError, extract_text
from app.llm.model_config_resolver import resolve_model_config_for_runtime
from app.knowledge.schema import (
    KnowledgeBucketRead,
    KnowledgeChunkRead,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
)
from app.knowledge.okf import (
    build_okf_for_document,
    okf_citations_for_concepts,
    search_concepts,
    selected_concept_cards,
    upsert_concepts,
)
from app.knowledge.citations import CITATION_EXCERPT_CHAR_LIMIT
from app.llm import LLMClient, LLMError
from app.observability.spans import llm_operation, observed_span
from app.skills.skill_schema import SkillCard, SkillGraphEdge, SkillGraphNode


# ---------------------------------------------------------------------------
# LLM 提示词模板路径（Markdown 文件，运行时读取）
# ---------------------------------------------------------------------------
PROMPT_DIR = paths.resource_dir() / "app" / "llm" / "prompts"
BUCKET_PROMPT = PROMPT_DIR / "knowledge_bucket_prompt.md"          # 入库时规划 Wiki 页面的提示词
DISCOVERY_PROMPT = PROMPT_DIR / "knowledge_discovery_prompt.md"    # 发现 SOP/工具建议的提示词
SEARCH_PROMPT = PROMPT_DIR / "knowledge_search_prompt.md"          # 检索时选择 Bucket 的提示词
DOCUMENT_ROUTE_PROMPT = PROMPT_DIR / "knowledge_document_route_prompt.md"  # 检索时选择文档的提示词

# ---------------------------------------------------------------------------
# 文本切分与检索相关常量
# ---------------------------------------------------------------------------
SECTION_TARGET_CHARS = 1400     # 单个章节节点的目标字符数上限
EVIDENCE_CHUNK_CHARS = 900      # 单个引用来源（Chunk）的字符数上限
BUCKET_SECTION_CHARS = 6000     # 单个 Bucket（知识主题）存储的正文字符数上限
PARAGRAPH_GROUP_CHARS = 4200    # 纯文本切分时的段落组字符数上限
SEARCH_DOCUMENT_LIMIT = 40      # 检索时加载的文档候选数量上限
SEARCH_BUCKET_LIMIT = 80        # 检索时加载的 Bucket 候选数量上限

# 入库任务终态：成功 / 失败 / 已取消，处于这些状态的任务不再被处理
TERMINAL_INGEST_STATUSES = {"succeeded", "failed", "cancelled"}
# 入库任务取消中状态：取消请求已收到但尚未完成清理
CANCELLING_INGEST_STATUSES = {"cancel_requested", "cancelled"}
# 取消请求超过此宽限期仍未被 finalize，视为过期并强制完成取消
CANCEL_REQUEST_STALE_AFTER = timedelta(seconds=15)

# 检索时的最低相关性分数阈值：低于此分数的候选将被过滤掉
SEARCH_MIN_DOCUMENT_SCORE = 2.0
SEARCH_MIN_BUCKET_SCORE = 2.0
SEARCH_MIN_CHUNK_SCORE = 2.0
SEARCH_MIN_EVIDENCE_SCORE = 2.0

# 入库流水线阶段定义：每个阶段包含 key（阶段标识）、label（中文展示名）、
# progress（该阶段完成时的累计进度，0.0 ~ 1.0）。前端据此渲染进度条。
INGEST_STAGES: list[dict[str, Any]] = [
    {"key": "queued", "label": "排队中", "progress": 0.0},
    {"key": "parsing", "label": "解析原始资料", "progress": 0.08},
    {"key": "normalizing", "label": "规范化 Source", "progress": 0.16},
    {"key": "documenting", "label": "写入 Source Document", "progress": 0.24},
    {"key": "bucketing", "label": "规划 Wiki 页面", "progress": 0.36},
    {"key": "bucket_writing", "label": "写入 OKF Wiki", "progress": 0.48},
    {"key": "chunking", "label": "生成引用来源", "progress": 0.62},
    {"key": "summarizing", "label": "刷新 PageIndex", "progress": 0.74},
    {"key": "discovering", "label": "发现 SOP/工具", "progress": 0.88},
    {"key": "done", "label": "完成入库", "progress": 1.0},
]

# 阶段 key → 阶段定义的快速查找映射
INGEST_STAGE_BY_KEY = {stage["key"]: stage for stage in INGEST_STAGES}
logger = logging.getLogger(__name__)


@dataclass
class IngestPayload:
    """入库任务的输入载荷。

    封装创建入库任务所需的全部信息，包括文件内容和元数据。

    Attributes:
        tenant_id: 租户 ID。
        knowledge_base_id: 目标知识库 ID。
        filename: 上传文件的原始文件名（含扩展名）。
        content_base64: 文件二进制内容的 Base64 编码字符串。
        knowledge_base_version_id: 知识库版本 ID，为 None 时自动推导默认值。
        title: 文档标题，为 None 时使用文件名（去扩展名）。
        metadata: 附加元数据，将存入文档的 metadata_json。
    """

    tenant_id: str
    knowledge_base_id: str
    filename: str
    content_base64: str
    knowledge_base_version_id: str | None = None
    title: str | None = None
    metadata: dict[str, Any] | None = None


class KnowledgeDiscoveryValidationError(ValueError):
    """Raised when a model-produced discovery cannot safely enter the review queue."""


class KnowledgeDiscoveryConflictError(ValueError):
    """Raised when a discovery cannot transition from pending to confirmed."""


def validate_discovered_skill(payload: dict[str, Any]) -> SkillCard:
    """校验 LLM 生成的技能草稿是否安全可入库。

    该函数执行多层校验：
    1. 拒绝未知字段（防止 LLM 产出意外键污染数据模型）。
    2. 通过 Pydantic 模型验证字段类型和必填项。
    3. 检查 skill_id、name 以及所有节点的必填字段非空。
    4. 从开始节点出发做正向 BFS，确保所有节点可达。
    5. 从结束节点出发做反向 BFS，确保所有节点都能到达终态。

    Args:
        payload: LLM 生成的技能草稿原始字典。

    Returns:
        校验通过的 SkillCard 对象。

    Raises:
        KnowledgeDiscoveryValidationError: 当草稿包含未知字段、缺少必填项、
            存在不可达节点或死胡同节点时抛出。
    """
    # 第 1 层：拒绝顶层未知字段
    unknown_skill_fields = sorted(set(payload) - set(SkillCard.model_fields))
    if unknown_skill_fields:
        raise KnowledgeDiscoveryValidationError(
            f"技能草稿包含未知字段：{', '.join(unknown_skill_fields)}。"
        )
    # 检查节点列表中的未知字段
    for index, node in enumerate(payload.get("nodes", [])):
        if not isinstance(node, dict):
            continue
        unknown_node_fields = sorted(set(node) - set(SkillGraphNode.model_fields))
        if unknown_node_fields:
            raise KnowledgeDiscoveryValidationError(
                f"技能草稿节点 nodes.{index} 包含未知字段：{', '.join(unknown_node_fields)}。"
            )
    # 检查连线列表中的未知字段
    for index, edge in enumerate(payload.get("edges", [])):
        if not isinstance(edge, dict):
            continue
        unknown_edge_fields = sorted(set(edge) - set(SkillGraphEdge.model_fields))
        if unknown_edge_fields:
            raise KnowledgeDiscoveryValidationError(
                f"技能草稿连线 edges.{index} 包含未知字段：{', '.join(unknown_edge_fields)}。"
            )
    # 第 2 层：Pydantic 模型验证，收集前 5 个错误以友好提示
    try:
        card = SkillCard.model_validate(payload)
    except ValidationError as exc:
        details = []
        for error in exc.errors(include_url=False)[:5]:
            field = ".".join(str(item) for item in error.get("loc", ())) or "skill"
            details.append(f"{field}: {error.get('msg', '字段不合法')}")
        suffix = f"：{'；'.join(details)}" if details else "。"
        raise KnowledgeDiscoveryValidationError(
            f"技能草稿不符合 StaffDeck SkillCard 格式{suffix}"
        ) from exc

    # 第 3 层：检查 skill_id 和 name 非空
    if not card.skill_id.strip() or not card.name.strip():
        raise KnowledgeDiscoveryValidationError("技能草稿的 skill_id 和 name 不能为空。")
    # 检查每个节点的必填字段
    incomplete_nodes = [
        node.node_id or "<empty>"
        for node in card.nodes
        if not node.node_id.strip() or not node.name.strip() or not node.instruction.strip()
    ]
    if incomplete_nodes:
        raise KnowledgeDiscoveryValidationError(
            f"技能草稿节点缺少 node_id、name 或 instruction：{', '.join(incomplete_nodes)}。"
        )

    # 第 4 层：构建邻接表，用于正向和反向可达性检查
    node_ids = {node.node_id for node in card.nodes}
    outgoing: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    reverse: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for edge in card.edges:
        outgoing[edge.source_node_id].add(edge.next_node_id)
        reverse[edge.next_node_id].add(edge.source_node_id)

    # 正向 BFS：从开始节点出发，确认所有节点可达
    reachable: set[str] = set()
    pending = [card.start_node_id]
    while pending:
        node_id = pending.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        pending.extend(outgoing[node_id] - reachable)
    unreachable = sorted(node_ids - reachable)
    if unreachable:
        raise KnowledgeDiscoveryValidationError(
            f"技能草稿包含无法从开始节点到达的节点：{', '.join(unreachable)}。"
        )

    # 第 5 层：反向 BFS：从所有结束节点出发，确认不存在死胡同
    reaches_terminal: set[str] = set()
    pending = list(card.terminal_node_ids)
    while pending:
        node_id = pending.pop()
        if node_id in reaches_terminal:
            continue
        reaches_terminal.add(node_id)
        pending.extend(reverse[node_id] - reaches_terminal)
    dead_ends = sorted(node_ids - reaches_terminal)
    if dead_ends:
        raise KnowledgeDiscoveryValidationError(
            f"技能草稿包含无法到达结束节点的节点：{', '.join(dead_ends)}。"
        )
    return card


class KnowledgeIngestCancelled(RuntimeError):
    """Raised inside the ingest worker when a persisted job is cancelled."""


class KnowledgeService:
    """知识管理核心服务类。

    提供知识入库（文件解析 → 章节构建 → Wiki 规划 → 引用来源生成 → 发现建议）、
    知识检索（多级路由：文档 → Bucket → 章节 → Chunk → 证据包）、以及知识发现
    建议的确认/拒绝等全部业务操作。

    Attributes:
        db: SQLModel 会话，用于所有数据库读写操作。
    """

    def __init__(self, db: Session):
        self.db = db

    def create_ingest_job(self, payload: IngestPayload) -> KnowledgeIngestJob:
        """创建一条入库任务并持久化到数据库。

        将文件内容（Base64 编码）、标题、元数据等存入任务的 metadata_json，
        然后由后台 worker 异步执行实际的入库流程。

        Args:
            payload: 入库任务输入载荷。

        Returns:
            已创建并刷新的 KnowledgeIngestJob 对象（status="queued"）。
        """
        job = KnowledgeIngestJob(
            tenant_id=payload.tenant_id,
            knowledge_base_id=payload.knowledge_base_id,
            knowledge_base_version_id=payload.knowledge_base_version_id
            or _default_knowledge_base_version_id(payload.knowledge_base_id),
            filename=payload.filename,
            status="queued",
            stage="queued",
            progress=0.0,
            metadata_json={
                "content_base64": payload.content_base64,
                "title": payload.title,
                "metadata": payload.metadata or {},
            },
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def cancel_ingest_job(self, job_id: str, tenant_id: str) -> KnowledgeIngestJob | None:
        """请求取消指定的入库任务。

        取消行为取决于当前任务状态：
        - 终态（succeeded/failed/cancelled）：直接返回当前状态。
        - queued 或 cancel_requested：立即标记为已取消。
        - 运行中：标记为 cancel_requested，由 worker 在下一个检查点响应。

        Args:
            job_id: 入库任务 ID。
            tenant_id: 租户 ID，用于权限校验。

        Returns:
            更新后的 KnowledgeIngestJob；若任务不存在或不属于该租户则返回 None。
        """
        job = self.db.get(KnowledgeIngestJob, job_id)
        if not job or job.tenant_id != tenant_id:
            return None
        if job.status in TERMINAL_INGEST_STATUSES:
            return job
        if job.status == "queued":
            self._finalize_cancelled_job(job, "入库任务已取消")
            return job
        if job.status == "cancel_requested":
            self._finalize_cancelled_job(job, "入库任务已取消")
            return job

        metadata = dict(job.metadata_json or {})
        metadata["stage_label"] = "取消中"
        metadata["stage_detail"] = "已收到取消请求，正在停止当前入库阶段"
        metadata["cancel_requested_at"] = utc_now().isoformat()
        metadata["ingest_steps"] = _ingest_steps_for(job.stage, float(job.progress or 0.0), "cancel_requested")
        job.metadata_json = metadata
        self._update_job(job, status="cancel_requested", error=None)
        return job

    def finalize_stale_cancel_requested_jobs(
        self,
        tenant_id: str,
        grace_period: timedelta = CANCEL_REQUEST_STALE_AFTER,
    ) -> list[KnowledgeIngestJob]:
        """批量清理过期的取消请求任务。

        遍历指定租户下所有处于 cancel_requested 状态的任务，
        将超过宽限期仍未完成清理的任务强制标记为已取消。

        Args:
            tenant_id: 租户 ID。
            grace_period: 宽限期，超过此时长未更新的取消请求被视为过期。

        Returns:
            被强制完成取消的任务列表。
        """
        rows = self.db.exec(
            select(KnowledgeIngestJob)
            .where(KnowledgeIngestJob.tenant_id == tenant_id)
            .where(KnowledgeIngestJob.status == "cancel_requested")
        ).all()
        finalized: list[KnowledgeIngestJob] = []
        for job in rows:
            if self.finalize_stale_cancel_requested_job(job, grace_period):
                finalized.append(job)
        return finalized

    def finalize_stale_cancel_requested_job(
        self,
        job: KnowledgeIngestJob,
        grace_period: timedelta = CANCEL_REQUEST_STALE_AFTER,
    ) -> KnowledgeIngestJob | None:
        """清理单个过期的取消请求任务。

        如果任务处于 cancel_requested 状态且超过宽限期未更新，
        则强制将其标记为已取消。

        Args:
            job: 待检查的入库任务。
            grace_period: 宽限期。

        Returns:
            若任务被成功取消则返回该任务，否则返回 None。
        """
        if job.status != "cancel_requested":
            return None
        last_update = job.updated_at or job.created_at
        if utc_now() - last_update < grace_period:
            return None
        self._finalize_cancelled_job(job, "入库任务已取消")
        return job

    def run_ingest_job(self, job_id: str) -> None:
        """在新数据库会话中执行入库任务（入口方法）。

        该方法会创建独立的 Session，避免与调用方的会话互相干扰，
        适合在后台 worker 中调用。

        Args:
            job_id: 入库任务 ID。
        """
        with Session(engine) as db:
            service = KnowledgeService(db)
            service._run_ingest_job(job_id)

    def _run_ingest_job(self, job_id: str) -> None:
        """执行入库任务的核心流水线。

        流水线阶段依次为：
        1. parsing — 解析文件格式并抽取正文文本。
        2. normalizing — 清理空行和段落。
        3. documenting — 构建章节导航树并写入 KnowledgeDocument。
        4. bucketing — 通过 LLM 或结构化方式规划 OKF Wiki 页面（Bucket）。
        5. chunking — 从 Bucket 生成引用来源（KnowledgeChunk）。
        6. OKF 概念构建与持久化。
        7. discovering — 自动发现可确认的 SOP/工具建议。
        8. done — 标记成功。

        每个阶段之间会检查取消状态，若被取消则抛出 KnowledgeIngestCancelled。
        任何异常都会被捕获并持久化为任务的失败状态。

        Args:
            job_id: 入库任务 ID。
        """
        job = self.db.get(KnowledgeIngestJob, job_id)
        if not job:
            return
        try:
            self._update_ingest_stage(
                job,
                "parsing",
                status="running",
                started_at=utc_now(),
                detail="正在识别文件格式并抽取正文",
            )
            metadata = job.metadata_json or {}
            content = base64.b64decode(str(metadata.get("content_base64") or ""))
            text, file_type = extract_text(job.filename, content)
            self._raise_if_ingest_cancelled(job)
            self._update_ingest_stage(
                job,
                "normalizing",
                detail=f"已抽取 {file_type} 文本，正在清理空行和段落",
            )
            normalized_text = _normalize_text(text)
            if not normalized_text:
                raise KnowledgeParseError("文档没有可用文本内容。")
            self._raise_if_ingest_cancelled(job)

            self._update_ingest_stage(
                job,
                "documenting",
                detail=f"已获得 {len(normalized_text):,} 字符，正在识别章节导航树",
                stats={"char_count": len(normalized_text), "file_type": file_type},
            )
            section_nodes = _build_section_nodes(normalized_text)
            self._raise_if_ingest_cancelled(job)
            document_card = _build_document_card(
                title=str(metadata.get("title") or Path(job.filename).stem),
                filename=job.filename,
                file_type=file_type,
                text=normalized_text,
                section_nodes=section_nodes,
            )
            document = KnowledgeDocument(
                tenant_id=job.tenant_id,
                knowledge_base_id=job.knowledge_base_id,
                knowledge_base_version_id=job.knowledge_base_version_id,
                filename=job.filename,
                file_type=file_type,
                title=str(metadata.get("title") or Path(job.filename).stem),
                status="processing",
                metadata_json={
                    **(metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {}),
                    "char_count": len(normalized_text),
                    "document_card": document_card,
                    "section_tree": section_nodes,
                    "section_stats": {
                        "section_count": len(section_nodes),
                        "paragraph_count": len(_paragraph_blocks(normalized_text)),
                    },
                },
            )
            self.db.add(document)
            self.db.commit()
            self.db.refresh(document)
            job.document_id = document.id
            self.db.add(job)
            self.db.commit()
            self.db.refresh(job)
            self._update_ingest_stage(
                job,
                "bucketing",
                detail="正在按目录结构、章节语义和任务用途规划 OKF Wiki 页面",
                document_id=document.id,
                stats={"section_count": len(section_nodes)},
            )

            buckets = self._build_buckets(
                job.tenant_id,
                job.knowledge_base_id,
                document,
                normalized_text,
                section_nodes,
                document_card,
                job,
            )
            self._update_ingest_stage(
                job,
                "bucket_writing",
                detail=f"已规划 {len(buckets)} 个知识主题，正在写入 OKF Wiki 与内部索引",
                stats={"bucket_count": len(buckets), "section_count": len(section_nodes)},
            )
            self._update_ingest_stage(
                job,
                "chunking",
                detail="正在从 OKF Wiki 与原始资料回填引用来源",
                stats={"bucket_count": len(buckets)},
            )
            chunk_count = self._build_chunks(job.tenant_id, job.knowledge_base_id, document, buckets, section_nodes, job)
            self._raise_if_ingest_cancelled(job)
            okf_concepts = build_okf_for_document(document, section_nodes, buckets)
            self._raise_if_ingest_cancelled(job)
            concept_rows = upsert_concepts(
                self.db,
                job.tenant_id,
                job.knowledge_base_id,
                document.knowledge_base_version_id,
                okf_concepts,
            )

            document.bucket_count = len(buckets)
            document.chunk_count = chunk_count
            document.status = "ready"
            document.metadata_json = {
                **(document.metadata_json or {}),
                "chunk_stats": {
                    "total_chunks": chunk_count,
                    "chunk_count": chunk_count,
                    "target_chars": EVIDENCE_CHUNK_CHARS,
                    "section_target_chars": SECTION_TARGET_CHARS,
                },
                "bucket_quality": [
                    {
                        "bucket_id": bucket.id,
                        "title": bucket.title,
                        "quality": (bucket.metadata_json or {}).get("quality", {}),
                    }
                    for bucket in buckets
                ],
                "okf": {
                    "version": "0.1",
                    "concept_count": len(concept_rows),
                    "concept_types": sorted({row.concept_type for row in concept_rows}),
                },
            }
            document.updated_at = utc_now()
            self.db.add(document)
            self._update_ingest_stage(
                job,
                "summarizing",
                detail=f"已生成 {chunk_count} 个引用来源，正在刷新 PageIndex 与来源摘要",
                stats={"concept_count": len(concept_rows), "bucket_count": len(buckets), "chunk_count": chunk_count},
            )
            self._update_ingest_stage(
                job,
                "discovering",
                detail="正在从 OKF Wiki 和引用来源发现可确认的 SOP/工具建议",
                stats={"bucket_count": len(buckets), "chunk_count": chunk_count},
            )

            self._discover_from_document(job.tenant_id, job.knowledge_base_id, document, buckets, job)
            self._update_ingest_stage(
                job,
                "done",
                status="succeeded",
                finished_at=utc_now(),
                detail=f"完成入库：{len(concept_rows)} 个 Wiki 页面，{len(buckets)} 个内部索引，{chunk_count} 个引用来源",
                stats={
                    "concept_count": len(concept_rows),
                    "bucket_count": len(buckets),
                    "chunk_count": chunk_count,
                },
            )
            self._clear_embedded_content(job)
        except KnowledgeIngestCancelled as exc:
            self._finalize_cancelled_job(job, str(exc) or "入库任务已取消")
        except Exception as exc:  # noqa: BLE001 - persist stable job failure.
            if job.document_id:
                document = self.db.get(KnowledgeDocument, job.document_id)
                if document:
                    document.status = "failed"
                    document.error = str(exc)
                    document.updated_at = utc_now()
                    self.db.add(document)
            self._update_ingest_stage(
                job,
                "failed",
                status="failed",
                error=str(exc),
                finished_at=utc_now(),
                detail=str(exc),
            )
            self._clear_embedded_content(job)

    def search(self, request: KnowledgeSearchRequest, model_config: ModelConfig | None = None) -> KnowledgeSearchResponse:
        """知识检索入口（带可观测性 Span 包裹）。

        在执行实际检索逻辑之前，会开启一个 observed_span 用于追踪查询的
        基本指标（查询字符数、最大返回数等）。

        Args:
            request: 知识检索请求对象。
            model_config: 可选的 LLM 模型配置。若提供，文档和 Bucket 的路由
                将使用 LLM 进行语义匹配；否则退化为词法相关性打分。

        Returns:
            包含选中文档、Bucket、Chunk、证据包等的结构化检索响应。
        """
        with observed_span(
            "knowledge_span",
            "knowledge.search",
            query_chars=len(request.query.strip()),
            max_chunks=request.max_chunks,
            max_buckets=request.max_buckets,
            max_depth=request.max_depth,
        ):
            return self._search(request, model_config)

    def _search(self, request: KnowledgeSearchRequest, model_config: ModelConfig | None = None) -> KnowledgeSearchResponse:
        """执行知识检索的核心逻辑。

        检索流水线依次经过以下阶段（每一步都生成可观测性 Span 和路由追踪日志）：
        1. 加载 OKF 概念并按查询语义选择最相关的概念（OKF 概念路由）。
        2. 加载文档并选择最相关的文档（LLM 路由或词法打分）。
        3. 加载 Bucket 并选择最相关的内部索引（LLM 路由或词法打分）。
        4. 展开选中 Bucket 关联的章节子树。
        5. 加载 Chunk 并按相关性 + 章节加成排序。
        6. 可选地构建证据包（Evidence Pack）。

        任何一步如果没有候选结果都会提前返回部分结果，而非抛出异常。

        Args:
            request: 知识检索请求对象。
            model_config: 可选的 LLM 模型配置。

        Returns:
            结构化检索响应。
        """
        query = request.query.strip()
        if not query:
            return KnowledgeSearchResponse()
        route_trace: list[dict[str, Any]] = []
        if request.agent_id and not request.knowledge_base_ids and not request.knowledge_base_version_ids:
            route_trace.append({"phase": "no_visible_knowledge", "message": "当前智能体没有可见知识"})
            return KnowledgeSearchResponse(trace=route_trace, route_trace=route_trace)

        with observed_span("knowledge_span", "knowledge.load_concepts") as span:
            concepts = self._load_concepts_for_search(request)
            span.finish(candidate_count=len(concepts))
        with observed_span(
            "knowledge_span", "knowledge.route_concepts", candidate_count=len(concepts)
        ) as span:
            selected_concepts = search_concepts(query, concepts, max(request.max_buckets, 4))
            span.finish(selected_count=len(selected_concepts))
        selected_concept_payload = selected_concept_cards(selected_concepts)
        okf_citations = okf_citations_for_concepts(selected_concepts)
        if concepts:
            route_trace.append(
                {
                    "phase": "okf_concept_route",
                    "message": "正在选择 OKF Wiki 页面",
                    "candidate_count": len(concepts),
                    "selected_count": len(selected_concepts),
                }
            )

        with observed_span("knowledge_span", "knowledge.load_documents") as span:
            documents = self._load_documents_for_search(request)
            span.finish(candidate_count=len(documents))
        if not documents and not selected_concepts:
            route_trace.append({"phase": "no_documents", "message": "没有可检索的知识文档或 OKF 概念"})
            return KnowledgeSearchResponse(trace=route_trace, route_trace=route_trace)
        if not documents:
            route_trace.append({"phase": "okf_only", "message": "仅命中 OKF Wiki 页面"})
            return KnowledgeSearchResponse(
                trace=route_trace,
                route_trace=route_trace,
                selected_concepts=selected_concept_payload,
                okf_citations=okf_citations,
            )

        route_trace.append(
            {
                "phase": "document_route",
                "message": "正在选择知识文档",
                "candidate_count": len(documents),
                "mode": request.mode,
            }
        )
        with observed_span(
            "knowledge_span",
            "knowledge.route_documents",
            candidate_count=len(documents),
            strategy="llm" if model_config else "lexical",
        ) as span:
            selected_document_ids: list[str] = []
            if model_config:
                selected_document_ids = self._select_documents_with_llm(
                    query, documents, 5, model_config, route_trace
                )
            else:
                selected_document_ids = [row.id for row in _score_documents(query, documents)[:5]]
                route_trace.append(
                    {
                        "phase": "document_route_lexical",
                        "message": "按检索相关性选择知识文档",
                        "selected_count": len(selected_document_ids),
                    }
                )
            span.finish(selected_count=len(selected_document_ids))
        concept_document_ids = [
            str(ref.get("document_id"))
            for concept in selected_concepts
            for ref in (concept.source_refs_json or [])
            if isinstance(ref, dict) and ref.get("document_id")
        ]
        selected_document_ids = _unique_strings(selected_document_ids + concept_document_ids[:3])

        selected_documents = [row for row in documents if row.id in set(selected_document_ids)]
        selected_document_cards = [_document_card_for_search(row) for row in selected_documents]
        if not selected_documents and not selected_concepts:
            route_trace.append({"phase": "document_route_no_match", "message": "没有足够相关的知识文档"})
            return KnowledgeSearchResponse(trace=route_trace, route_trace=route_trace)

        with observed_span(
            "knowledge_span", "knowledge.load_buckets", document_count=len(selected_document_ids)
        ) as span:
            buckets = self._load_buckets_for_search(request, selected_document_ids)
            span.finish(candidate_count=len(buckets))
        if not buckets:
            route_trace.append({"phase": "no_buckets", "message": "所选文档没有可展开的内部索引"})
            return KnowledgeSearchResponse(
                trace=route_trace,
                route_trace=route_trace,
                selected_documents=selected_document_cards,
                selected_concepts=selected_concept_payload,
                okf_citations=okf_citations,
            )

        route_trace.append(
            {
                "phase": "bucket_route",
                    "message": "正在选择内部索引",
                "candidate_count": len(buckets),
                "selected_document_ids": selected_document_ids,
            }
        )
        with observed_span(
            "knowledge_span",
            "knowledge.route_buckets",
            candidate_count=len(buckets),
            strategy="llm" if model_config else "lexical",
        ) as span:
            selected_ids: list[str] = []
            if model_config:
                selected_ids = self._select_buckets_with_llm(
                    query, buckets, request.max_buckets, model_config, route_trace
                )
            else:
                selected_ids = [bucket.id for bucket in _score_buckets(query, buckets)[: request.max_buckets]]
                route_trace.append(
                    {
                        "phase": "bucket_route_lexical",
                        "message": "按检索相关性选择内部索引",
                        "selected_count": len(selected_ids),
                    }
                )
            span.finish(selected_count=len(selected_ids))

        bucket_by_id = {bucket.id: bucket for bucket in buckets}
        selected_buckets = [bucket_by_id[bucket_id] for bucket_id in selected_ids if bucket_id in bucket_by_id]
        if not selected_buckets and not selected_concepts:
            route_trace.append({"phase": "bucket_route_no_match", "message": "没有足够相关的内部索引"})
            return KnowledgeSearchResponse(
                trace=route_trace,
                route_trace=route_trace,
                selected_documents=selected_document_cards,
            )
        with observed_span(
            "knowledge_span",
            "knowledge.expand_sections",
            document_count=len(selected_documents),
            bucket_count=len(selected_buckets),
        ) as span:
            expanded_sections = _expand_sections(
                selected_documents, selected_buckets, request.max_depth
            )
            span.finish(section_count=len(expanded_sections))
        route_trace.append(
            {
                "phase": "section_expand",
                "message": "正在展开章节",
                "section_count": len(expanded_sections),
            }
        )
        with observed_span(
            "knowledge_span", "knowledge.load_chunks", bucket_count=len(selected_ids)
        ) as span:
            chunks = self._load_chunks_for_buckets(
                request.tenant_id,
                selected_ids,
                max(request.max_chunks * 3, request.max_chunks),
            )
            span.finish(candidate_count=len(chunks))
        with observed_span(
            "knowledge_span", "knowledge.rank_chunks", candidate_count=len(chunks)
        ) as span:
            ranked_chunks = _rank_chunks(query, chunks, selected_buckets, expanded_sections)[
                : request.max_chunks
            ]
            span.finish(selected_count=len(ranked_chunks))
        with observed_span(
            "knowledge_span",
            "knowledge.build_evidence_pack",
            chunk_count=len(ranked_chunks),
            enabled=request.need_evidence_pack,
        ) as span:
            evidence_pack = (
                _build_evidence_pack(query, ranked_chunks) if request.need_evidence_pack else []
            )
            span.finish(evidence_count=len(evidence_pack))
        route_trace.extend(
            [
                {"phase": "read_chunks", "message": "读取引用来源", "chunk_count": len(ranked_chunks)},
                {"phase": "evidence_pack", "message": "整理引用来源包", "evidence_count": len(evidence_pack)},
            ]
        )
        return KnowledgeSearchResponse(
            selected_buckets=[bucket_read(row) for row in selected_buckets],
            chunks=[chunk_read(row) for row in ranked_chunks],
            trace=route_trace,
            route_trace=route_trace,
            selected_documents=selected_document_cards,
            selected_concepts=selected_concept_payload,
            expanded_sections=expanded_sections,
            okf_citations=okf_citations,
            evidence_pack=evidence_pack,
        )

    def confirm_discovery(self, suggestion: KnowledgeDiscoverySuggestion) -> dict[str, Any]:
        """确认一条知识发现建议，将其转化为实际资源。

        根据建议类型（tool 或 skill）创建对应的 Tool 或 Skill 记录，
        然后将建议状态更新为 confirmed。

        Args:
            suggestion: 待确认的知识发现建议。

        Returns:
            包含创建结果的字典，例如 ``{"status": "created", "tool_id": "..."}``。

        Raises:
            KnowledgeDiscoveryConflictError: 当建议状态非 pending、资源已存在
                或发生数据库唯一约束冲突时抛出。
            KnowledgeDiscoveryValidationError: 当建议 payload 缺少必填字段时抛出。
        """
        if suggestion.status != "pending":
            raise KnowledgeDiscoveryConflictError(
                f"只有待处理建议可以确认，当前状态为 {suggestion.status}。"
            )
        payload = suggestion.payload_json or {}
        try:
            if suggestion.suggestion_type == "tool":
                created = self._confirm_tool(suggestion, payload)
            elif suggestion.suggestion_type == "skill":
                created = self._confirm_skill(suggestion, payload)
            else:
                created = {"status": "confirmed"}
            suggestion.status = "confirmed"
            suggestion.updated_at = utc_now()
            self.db.add(suggestion)
            self.db.commit()
            return created
        except IntegrityError as exc:
            self.db.rollback()
            raise KnowledgeDiscoveryConflictError("资源已存在或建议已被其他请求处理，请刷新后重试。") from exc
        except Exception:
            self.db.rollback()
            raise

    def reject_discovery(self, suggestion: KnowledgeDiscoverySuggestion) -> None:
        """拒绝一条知识发现建议。

        将建议状态从 pending 更新为 rejected。

        Args:
            suggestion: 待拒绝的知识发现建议。

        Raises:
            KnowledgeDiscoveryConflictError: 当建议状态非 pending 时抛出。
        """
        if suggestion.status != "pending":
            raise KnowledgeDiscoveryConflictError(
                f"只有待处理建议可以拒绝，当前状态为 {suggestion.status}。"
            )
        suggestion.status = "rejected"
        suggestion.updated_at = utc_now()
        self.db.add(suggestion)
        self.db.commit()

    def _build_buckets(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        document: KnowledgeDocument,
        text: str,
        section_nodes: list[dict[str, Any]],
        document_card: dict[str, Any],
        job: KnowledgeIngestJob,
    ) -> list[KnowledgeBucket]:
        """为文档构建知识主题（Bucket）列表。

        Bucket 的规划策略：
        1. 基于文档章节结构的 structure_bucket_specs（总是执行）。
        2. 基于 LLM 语义理解的 llm_buckets（当配置了默认模型时）。
        3. 两类规格合并去重；若仍为空则使用 fallback 兜底。
        每个 Bucket 写入数据库后返回刷新后的列表。

        Args:
            tenant_id: 租户 ID。
            knowledge_base_id: 知识库 ID。
            document: 所属文档对象。
            text: 文档规范化后的全文。
            section_nodes: 章节节点列表。
            document_card: 文档摘要卡片。
            job: 入库任务对象（用于取消检查）。

        Returns:
            创建的 KnowledgeBucket 列表。
        """
        model_config = self._default_model_config(tenant_id)
        structure_buckets = _structure_bucket_specs(section_nodes)
        llm_buckets = self._bucket_with_llm(section_nodes, model_config) if model_config else []
        self._raise_if_ingest_cancelled(job)
        bucket_specs = _unique_bucket_specs(structure_buckets + _normalize_llm_bucket_specs(llm_buckets, section_nodes))
        if not bucket_specs:
            bucket_specs = _fallback_bucket_specs(_split_sections(text), section_nodes)
        self.db.exec(delete(KnowledgeBucket).where(KnowledgeBucket.document_id == document.id))
        self.db.exec(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
        self.db.exec(delete(KnowledgeDiscoverySuggestion).where(KnowledgeDiscoverySuggestion.document_id == document.id))
        rows: list[KnowledgeBucket] = []
        for index, spec in enumerate(bucket_specs):
            self._raise_if_ingest_cancelled(job)
            content = str(spec.get("content") or "")
            section_ids = [str(item) for item in spec.get("section_ids", []) if item]
            if not content and section_ids:
                section_by_id = {str(node.get("section_id")): node for node in section_nodes}
                content = "\n\n".join(
                    str(section_by_id[section_id].get("content") or "")
                    for section_id in section_ids
                    if section_id in section_by_id
                )
            content = content or text[:BUCKET_SECTION_CHARS]
            quality = _bucket_quality(spec, section_ids, content)
            row = KnowledgeBucket(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_base_version_id=document.knowledge_base_version_id,
                document_id=document.id,
                bucket_key=str(spec.get("bucket_key") or f"bucket_{index + 1}"),
                title=str(spec.get("title") or f"知识主题 {index + 1}"),
                summary=str(spec.get("summary") or content[:300]),
                token_estimate=max(1, len(content) // 2),
                metadata_json={
                    "content": content[:BUCKET_SECTION_CHARS],
                    "bucket_type": str(spec.get("bucket_type") or "structure"),
                    "concept_type": str(spec.get("concept_type") or "Topic"),
                    "section_ids": section_ids,
                    "section_paths": spec.get("section_paths") if isinstance(spec.get("section_paths"), list) else [],
                    "representative_chunk_ids": [],
                    "applicable_query_types": spec.get("applicable_query_types") if isinstance(spec.get("applicable_query_types"), list) else [],
                    "quality": quality,
                    "document_card": {
                        "title": document_card.get("title"),
                        "summary": document_card.get("summary"),
                    },
                },
            )
            self.db.add(row)
            rows.append(row)
        self.db.commit()
        for row in rows:
            self.db.refresh(row)
        return rows

    def _build_chunks(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        document: KnowledgeDocument,
        buckets: list[KnowledgeBucket],
        section_nodes: list[dict[str, Any]],
        job: KnowledgeIngestJob,
    ) -> int:
        """为每个 Bucket 生成引用来源（Chunk）。

        从 Bucket 关联的章节中提取正文内容，按 EVIDENCE_CHUNK_CHARS 上限切分为
        多个 Chunk 并写入数据库。每个 Chunk 包含来源路径、摘要、上下文窗口等元数据。
        最后回填每个 Bucket 的 representative_chunk_ids 和 chunk_count。

        Args:
            tenant_id: 租户 ID。
            knowledge_base_id: 知识库 ID。
            document: 所属文档对象。
            buckets: Bucket 列表。
            section_nodes: 章节节点列表。
            job: 入库任务对象（用于取消检查）。

        Returns:
            创建的 Chunk 总数。
        """
        count = 0
        chunk_ids_by_bucket: dict[str, list[str]] = {}
        section_by_id = {str(node.get("section_id")): node for node in section_nodes}
        for bucket in buckets:
            self._raise_if_ingest_cancelled(job)
            metadata = dict(bucket.metadata_json or {})
            section_ids = [str(item) for item in metadata.get("section_ids", []) if item]
            section_sources = [section_by_id[section_id] for section_id in section_ids if section_id in section_by_id]
            if not section_sources:
                section_sources = [
                    {
                        "section_id": f"{bucket.bucket_key}_content",
                        "path": bucket.title,
                        "title": bucket.title,
                        "content": str(metadata.get("content") or ""),
                    }
                ]
            local_index = 0
            for section in section_sources:
                self._raise_if_ingest_cancelled(job)
                content = str(section.get("content") or "")
                parts = _chunk_text(content, EVIDENCE_CHUNK_CHARS)
                for part in parts:
                    self._raise_if_ingest_cancelled(job)
                    source_path = f"{document.filename} / {section.get('path') or bucket.title} / evidence {local_index + 1}"
                    row = KnowledgeChunk(
                        tenant_id=tenant_id,
                        knowledge_base_id=knowledge_base_id,
                        knowledge_base_version_id=document.knowledge_base_version_id,
                        document_id=document.id,
                        bucket_id=bucket.id,
                        chunk_index=local_index,
                        content=part,
                        summary=_summarize_text(part, 180),
                        source_ref=source_path,
                        metadata_json={
                            "node_type": "evidence_chunk",
                            "section_id": section.get("section_id"),
                            "section_path": section.get("path"),
                            "section_title": section.get("title"),
                            "bucket_title": bucket.title,
                            "source_span": section.get("source_span") or {},
                            "context_window": _summarize_text(content, 260),
                        },
                    )
                    self.db.add(row)
                    self.db.flush()
                    chunk_ids_by_bucket.setdefault(bucket.id, []).append(row.id)
                    count += 1
                    local_index += 1
        for bucket in buckets:
            metadata = dict(bucket.metadata_json or {})
            metadata["representative_chunk_ids"] = chunk_ids_by_bucket.get(bucket.id, [])[:3]
            metadata["chunk_count"] = len(chunk_ids_by_bucket.get(bucket.id, []))
            bucket.metadata_json = metadata
            bucket.updated_at = utc_now()
            self.db.add(bucket)
        self.db.commit()
        return count

    def _discover_from_document(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        document: KnowledgeDocument,
        buckets: list[KnowledgeBucket],
        job: KnowledgeIngestJob,
    ) -> None:
        """从文档和 Bucket 内容中自动发现技能 / 工具建议。

        将文档信息和 Bucket 摘要发送给 LLM，获取 discoveries 列表。
        对每条发现建议进行类型和内容校验后，写入 KnowledgeDiscoverySuggestion 表，
        等待人工审核。若未配置模型或 LLM 调用失败则静默跳过。

        Args:
            tenant_id: 租户 ID。
            knowledge_base_id: 知识库 ID。
            document: 文档对象。
            buckets: Bucket 列表。
            job: 入库任务对象（用于取消检查）。
        """
        model_config = self._default_model_config(tenant_id)
        if not model_config:
            return
        self._raise_if_ingest_cancelled(job)
        payload = {
            "document": {
                "id": document.id,
                "filename": document.filename,
                "title": document.title,
                "file_type": document.file_type,
            },
            "buckets": [
                {
                    "id": bucket.id,
                    "title": bucket.title,
                    "summary": bucket.summary,
                    "excerpt": str((bucket.metadata_json or {}).get("content") or "")[:2400],
                }
                for bucket in buckets
            ],
        }
        try:
            with llm_operation("knowledge.discovery", bucket_count=len(buckets)):
                raw = LLMClient(model_config).generate_json(
                    DISCOVERY_PROMPT.read_text(encoding="utf-8"), payload
                )
        except (LLMError, Exception):
            return
        self._raise_if_ingest_cancelled(job)
        discoveries = raw.get("discoveries") if isinstance(raw, dict) else None
        if not isinstance(discoveries, list):
            return
        for item in discoveries:
            self._raise_if_ingest_cancelled(job)
            if not isinstance(item, dict):
                continue
            suggestion_type = str(item.get("suggestion_type") or "").strip()
            if suggestion_type not in {"skill", "tool", "warning"}:
                continue
            title = str(item.get("title") or "").strip() or "未命名建议"
            discovery_payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            status = "pending"
            reason = _optional_str(item.get("reason"))
            if suggestion_type == "skill":
                skill_payload = (
                    discovery_payload.get("draft_skill")
                    if isinstance(discovery_payload.get("draft_skill"), dict)
                    else discovery_payload
                )
                try:
                    card = validate_discovered_skill(skill_payload)
                except KnowledgeDiscoveryValidationError as exc:
                    status = "invalid"
                    reason = f"{reason + ' ' if reason else ''}草稿校验失败：{exc}"
                    logger.warning(
                        "Rejected invalid knowledge skill discovery tenant=%s document=%s title=%s: %s",
                        tenant_id,
                        document.id,
                        title,
                        exc,
                    )
                else:
                    discovery_payload = {"draft_skill": card.model_dump(mode="json")}
            row = KnowledgeDiscoverySuggestion(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_base_version_id=document.knowledge_base_version_id,
                document_id=document.id,
                bucket_id=_optional_str(item.get("bucket_id")),
                suggestion_type=suggestion_type,
                title=title,
                status=status,
                payload_json=discovery_payload,
                source_refs_json=item.get("source_refs") if isinstance(item.get("source_refs"), list) else [],
                reason=reason,
            )
            self.db.add(row)
        self.db.commit()

    def _bucket_with_llm(
        self, section_nodes: list[dict[str, Any]], model_config: ModelConfig | None
    ) -> list[dict[str, Any]]:
        """通过 LLM 对章节进行语义分桶。

        将章节摘要和摘要发送给 LLM，获取语义化的 Bucket 规划。
        若未配置模型或 LLM 调用失败，返回空列表（由调用方回退到结构化分桶）。

        Args:
            section_nodes: 章节节点列表（最多取前 60 个发给 LLM）。
            model_config: LLM 模型配置。

        Returns:
            LLM 返回的 Bucket 规格列表；失败时返回空列表。
        """
        if not model_config:
            return []
        payload = {
            "sections": [
                {
                    "section_id": node.get("section_id"),
                    "path": node.get("path"),
                    "title": node.get("title"),
                    "summary": node.get("summary"),
                    "excerpt": str(node.get("content") or "")[:1800],
                }
                for node in section_nodes[:60]
            ]
        }
        try:
            with llm_operation("knowledge.ingest_bucket", section_count=len(section_nodes)):
                raw = LLMClient(model_config).generate_json(
                    BUCKET_PROMPT.read_text(encoding="utf-8"), payload
                )
        except (LLMError, Exception):
            return []
        buckets = raw.get("buckets") if isinstance(raw, dict) else None
        return [item for item in buckets if isinstance(item, dict)] if isinstance(buckets, list) else []

    def _load_documents_for_search(self, request: KnowledgeSearchRequest) -> list[KnowledgeDocument]:
        """为检索加载候选文档列表。

        按租户、知识库、版本、指定文档 ID 过滤，只返回 status="ready" 的文档，
        按更新时间倒序排列，最多返回 SEARCH_DOCUMENT_LIMIT 条。

        Args:
            request: 检索请求对象。

        Returns:
            候选文档列表。
        """
        stmt = select(KnowledgeDocument).where(
            KnowledgeDocument.tenant_id == request.tenant_id,
            KnowledgeDocument.status == "ready",
        )
        if request.knowledge_base_ids:
            stmt = stmt.where(KnowledgeDocument.knowledge_base_id.in_(request.knowledge_base_ids))
        if request.knowledge_base_version_ids:
            stmt = stmt.where(KnowledgeDocument.knowledge_base_version_id.in_(request.knowledge_base_version_ids))
        if request.document_ids:
            stmt = stmt.where(KnowledgeDocument.id.in_(request.document_ids))
        return self.db.exec(stmt.order_by(KnowledgeDocument.updated_at.desc()).limit(SEARCH_DOCUMENT_LIMIT)).all()

    def _load_concepts_for_search(self, request: KnowledgeSearchRequest) -> list[KnowledgeConcept]:
        """为检索加载候选 OKF 概念列表。

        按租户、知识库、版本、指定文档 ID 过滤，只返回 status="active" 的概念，
        按更新时间倒序排列，最多返回 120 条。

        Args:
            request: 检索请求对象。

        Returns:
            候选概念列表。
        """
        stmt = select(KnowledgeConcept).where(
            KnowledgeConcept.tenant_id == request.tenant_id,
            KnowledgeConcept.status == "active",
        )
        if request.knowledge_base_ids:
            stmt = stmt.where(KnowledgeConcept.knowledge_base_id.in_(request.knowledge_base_ids))
        if request.knowledge_base_version_ids:
            stmt = stmt.where(KnowledgeConcept.knowledge_base_version_id.in_(request.knowledge_base_version_ids))
        if request.document_ids:
            stmt = stmt.where(KnowledgeConcept.document_id.in_(request.document_ids))
        return self.db.exec(stmt.order_by(KnowledgeConcept.updated_at.desc()).limit(120)).all()

    def _load_buckets_for_search(self, request: KnowledgeSearchRequest, document_ids: list[str]) -> list[KnowledgeBucket]:
        """为检索加载指定文档下的候选 Bucket 列表。

        按租户、指定文档 ID 集合过滤，按创建时间升序排列，最多返回 SEARCH_BUCKET_LIMIT 条。

        Args:
            request: 检索请求对象。
            document_ids: 已选中的文档 ID 列表。

        Returns:
            候选 Bucket 列表；若 document_ids 为空则返回空列表。
        """
        if not document_ids:
            return []
        stmt = select(KnowledgeBucket).where(
            KnowledgeBucket.tenant_id == request.tenant_id,
            KnowledgeBucket.document_id.in_(document_ids),
        )
        if request.knowledge_base_ids:
            stmt = stmt.where(KnowledgeBucket.knowledge_base_id.in_(request.knowledge_base_ids))
        if request.knowledge_base_version_ids:
            stmt = stmt.where(KnowledgeBucket.knowledge_base_version_id.in_(request.knowledge_base_version_ids))
        return self.db.exec(stmt.order_by(KnowledgeBucket.created_at.asc()).limit(SEARCH_BUCKET_LIMIT)).all()

    def _select_documents_with_llm(
        self,
        query: str,
        documents: list[KnowledgeDocument],
        max_documents: int,
        model_config: ModelConfig,
        trace: list[dict[str, Any]],
    ) -> list[str]:
        """使用 LLM 从候选文档中选择与查询最相关的文档。

        将查询和文档摘要卡片发送给 LLM，获取选中的文档 ID 列表。
        只返回候选集合中存在的 ID，并截断到 max_documents 条。
        若 LLM 调用失败，在 trace 中记录失败原因并返回空列表。

        Args:
            query: 用户查询字符串。
            documents: 候选文档列表。
            max_documents: 最多选择的文档数量。
            model_config: LLM 模型配置。
            trace: 路由追踪日志列表（会被原地追加）。

        Returns:
            选中的文档 ID 列表。
        """
        payload = {
            "query": query,
            "max_documents": max_documents,
            "documents": [_document_card_for_route(row) for row in documents],
        }
        try:
            with llm_operation("knowledge.document_route", candidate_count=len(documents)):
                raw = LLMClient(model_config).generate_json(
                    DOCUMENT_ROUTE_PROMPT.read_text(encoding="utf-8"), payload
                )
        except (LLMError, Exception) as exc:
            trace.append({"phase": "document_route_failed", "message": str(exc)})
            return []
        ids = raw.get("selected_document_ids") if isinstance(raw, dict) else None
        if not isinstance(ids, list):
            return []
        allowed = {row.id for row in documents}
        return [str(item) for item in ids if str(item) in allowed][:max_documents]

    def _select_buckets_with_llm(
        self,
        query: str,
        buckets: list[KnowledgeBucket],
        max_buckets: int,
        model_config: ModelConfig,
        trace: list[dict[str, Any]],
    ) -> list[str]:
        """使用 LLM 从候选 Bucket 中选择与查询最相关的 Bucket。

        将查询和 Bucket 元数据（标题、摘要、质量等）发送给 LLM，
        获取选中的 Bucket ID 列表。只返回候选集合中存在的 ID，并截断到 max_buckets 条。
        若 LLM 调用失败，在 trace 中记录失败原因并返回空列表。

        Args:
            query: 用户查询字符串。
            buckets: 候选 Bucket 列表（最多取前 80 个发给 LLM）。
            max_buckets: 最多选择的 Bucket 数量。
            model_config: LLM 模型配置。
            trace: 路由追踪日志列表（会被原地追加）。

        Returns:
            选中的 Bucket ID 列表。
        """
        payload = {
            "query": query,
            "max_buckets": max_buckets,
            "buckets": [
                {
                    "id": bucket.id,
                    "title": bucket.title,
                    "summary": bucket.summary,
                    "document_id": bucket.document_id,
                    "bucket_type": (bucket.metadata_json or {}).get("bucket_type"),
                    "section_paths": (bucket.metadata_json or {}).get("section_paths", []),
                    "quality": (bucket.metadata_json or {}).get("quality", {}),
                }
                for bucket in buckets[:80]
            ],
        }
        try:
            with llm_operation("knowledge.bucket_route", candidate_count=len(buckets)):
                raw = LLMClient(model_config).generate_json(
                    SEARCH_PROMPT.read_text(encoding="utf-8"), payload
                )
        except (LLMError, Exception) as exc:
            trace.append({"phase": "bucket_selection_failed", "message": str(exc)})
            return []
        ids = raw.get("selected_bucket_ids") if isinstance(raw, dict) else None
        if not isinstance(ids, list):
            return []
        allowed = {bucket.id for bucket in buckets}
        return [str(item) for item in ids if str(item) in allowed][:max_buckets]

    def _load_chunks_for_buckets(self, tenant_id: str, bucket_ids: list[str], max_chunks: int) -> list[KnowledgeChunk]:
        """加载指定 Bucket 下的引用来源（Chunk）列表。

        按 Bucket ID 和 chunk_index 排序返回，最多 max_chunks 条。

        Args:
            tenant_id: 租户 ID。
            bucket_ids: Bucket ID 列表。
            max_chunks: 最多返回的 Chunk 数量。

        Returns:
            Chunk 列表；若 bucket_ids 为空则返回空列表。
        """
        if not bucket_ids:
            return []
        return self.db.exec(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.tenant_id == tenant_id, KnowledgeChunk.bucket_id.in_(bucket_ids))
            .order_by(KnowledgeChunk.bucket_id, KnowledgeChunk.chunk_index)
            .limit(max_chunks)
        ).all()

    def _confirm_tool(self, suggestion: KnowledgeDiscoverySuggestion, payload: dict[str, Any]) -> dict[str, Any]:
        """将工具类发现建议确认为实际的 Tool 记录。

        校验必填字段（name、url），检查名称是否已存在，然后创建 Tool 行。

        Args:
            suggestion: 知识发现建议对象。
            payload: 工具定义的 payload 字典。

        Returns:
            ``{"status": "created", "tool_id": "<新创建的工具ID>"}``

        Raises:
            KnowledgeDiscoveryValidationError: 当缺少 name 或 url 时抛出。
            KnowledgeDiscoveryConflictError: 当工具名称已存在时抛出。
        """
        name = str(payload.get("name") or payload.get("tool_name") or "").strip()
        if not name:
            raise KnowledgeDiscoveryValidationError("工具建议缺少 name。")
        url = str(payload.get("url") or "").strip()
        if not url:
            raise KnowledgeDiscoveryValidationError("工具建议缺少 url。")
        existing = self.db.exec(
            select(Tool).where(Tool.tenant_id == suggestion.tenant_id, Tool.name == name)
        ).first()
        if existing:
            raise KnowledgeDiscoveryConflictError(f"工具名称 {name} 已存在，请修改建议后重试。")
        row = Tool(
            tenant_id=suggestion.tenant_id,
            name=name,
            display_name=_optional_str(payload.get("display_name")) or suggestion.title,
            description=_optional_str(payload.get("description") or suggestion.reason),
            bucket=str(payload.get("bucket") or "知识自发现工具").strip() or "知识自发现工具",
            method=str(payload.get("method") or "POST").upper(),
            url=url,
            headers_json=payload.get("headers") if isinstance(payload.get("headers"), dict) else {},
            auth_json=payload.get("auth") if isinstance(payload.get("auth"), dict) else {},
            input_schema=payload.get("input_schema") if isinstance(payload.get("input_schema"), dict) else {},
            output_schema=payload.get("output_schema") if isinstance(payload.get("output_schema"), dict) else {},
            allowed_skills_json=payload.get("allowed_skills") if isinstance(payload.get("allowed_skills"), list) else [],
            enabled=True,
        )
        self.db.add(row)
        self.db.flush()
        self.db.refresh(row)
        return {"status": "created", "tool_id": row.id}

    def _confirm_skill(self, suggestion: KnowledgeDiscoverySuggestion, payload: dict[str, Any]) -> dict[str, Any]:
        """将技能类发现建议确认为实际的 Skill 记录。

        先通过 validate_discovered_skill 校验草稿合法性，
        再检查 skill_id 是否已存在，最后创建 Skill 行。

        Args:
            suggestion: 知识发现建议对象。
            payload: 包含 draft_skill 的 payload 字典。

        Returns:
            ``{"status": "created", "skill_id": "<新创建的技能ID>"}``

        Raises:
            KnowledgeDiscoveryValidationError: 当技能草稿校验失败时抛出。
            KnowledgeDiscoveryConflictError: 当 skill_id 已存在时抛出。
        """
        skill_payload = payload.get("draft_skill") if isinstance(payload.get("draft_skill"), dict) else payload
        card = validate_discovered_skill(skill_payload)
        existing = self.db.exec(
            select(Skill).where(Skill.tenant_id == suggestion.tenant_id, Skill.skill_id == card.skill_id)
        ).first()
        if existing:
            raise KnowledgeDiscoveryConflictError(
                f"技能 ID {card.skill_id} 已存在，不能通过知识发现覆盖现有技能。"
            )
        row = Skill(
            tenant_id=suggestion.tenant_id,
            skill_id=card.skill_id,
            version=card.version,
            name=card.name,
            business_domain=card.business_domain,
            description=card.description,
            content_json=card.model_dump(mode="json"),
            status="draft",
        )
        self.db.add(row)
        self.db.flush()
        self.db.refresh(row)
        return {"status": "created", "skill_id": row.id}

    def _default_model_config(self, tenant_id: str) -> ModelConfig | None:
        """获取租户的默认 LLM 模型配置。

        查询该租户下标记为默认且已启用的 ModelConfig，
        并通过 resolver 解析运行时所需的具体配置。

        Args:
            tenant_id: 租户 ID。

        Returns:
            解析后的 ModelConfig；若不存在则返回 None。
        """
        row = self.db.exec(
            select(ModelConfig).where(
                ModelConfig.tenant_id == tenant_id,
                ModelConfig.is_default == True,  # noqa: E712 - SQLModel expression.
                ModelConfig.enabled == True,  # noqa: E712
            )
        ).first()
        if row is None:
            return None
        return resolve_model_config_for_runtime(self.db, tenant_id, row.id)

    def ensure_default_knowledge_base(self, tenant_id: str) -> KnowledgeBase:
        """确保租户拥有至少一个知识库，若不存在则创建默认知识库。

        查询该租户下最早创建的知识库；若不存在，则创建一个名为"默认知识库"的
        KnowledgeBase 记录。

        Args:
            tenant_id: 租户 ID。

        Returns:
            已存在或新创建的 KnowledgeBase 对象。
        """
        existing = self.db.exec(
            select(KnowledgeBase)
            .where(KnowledgeBase.tenant_id == tenant_id)
            .order_by(KnowledgeBase.created_at.asc())
        ).first()
        if existing:
            return existing
        row = KnowledgeBase(
            id=f"kb_{tenant_id}_default",
            tenant_id=tenant_id,
            name="默认知识库",
            description="系统默认知识库",
            status="active",
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _update_job(self, job: KnowledgeIngestJob, **changes: Any) -> None:
        """更新入库任务字段并立即提交。

        将传入的变更应用到 job 对象上，刷新 updated_at，然后提交并刷新。

        Args:
            job: 入库任务对象。
            **changes: 要更新的字段键值对（如 status="running"）。
        """
        for key, value in changes.items():
            setattr(job, key, value)
        job.updated_at = utc_now()
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)

    def _raise_if_ingest_cancelled(self, job: KnowledgeIngestJob) -> None:
        """检查入库任务是否已被取消，若是则抛出 KnowledgeIngestCancelled。

        先从数据库刷新任务状态（确保读到最新值），然后检查是否处于取消状态。
        该方法在入库流水线的各个阶段之间被调用，作为取消检查点。

        Args:
            job: 入库任务对象。

        Raises:
            KnowledgeIngestCancelled: 当任务状态为 cancel_requested 或 cancelled 时抛出。
        """
        self.db.refresh(job)
        if job.status in CANCELLING_INGEST_STATUSES:
            raise KnowledgeIngestCancelled("入库任务已取消")

    def _finalize_cancelled_job(self, job: KnowledgeIngestJob, detail: str) -> None:
        """完成取消流程：删除已产生的部分文档数据，清理嵌入内容，标记任务为已取消。

        如果任务在取消前已经创建了文档，会级联删除该文档及其关联的
        Bucket、Chunk、Concept、DiscoverySuggestion。

        Args:
            job: 入库任务对象。
            detail: 取消原因描述。
        """
        cancelled_document_id = job.document_id
        if cancelled_document_id:
            self._delete_partial_ingest_document(job)
        metadata = dict(job.metadata_json or {})
        metadata.pop("content_base64", None)
        metadata["stage_label"] = "已取消"
        metadata["stage_detail"] = detail
        metadata["cancelled_at"] = utc_now().isoformat()
        if cancelled_document_id:
            metadata["cancelled_document_id"] = cancelled_document_id
        metadata["ingest_steps"] = _ingest_steps_for(job.stage, float(job.progress or 0.0), "cancelled")
        job.metadata_json = metadata
        self._update_job(
            job,
            status="cancelled",
            stage="cancelled",
            progress=float(job.progress or 0.0),
            error=None,
            finished_at=utc_now(),
            document_id=None,
        )

    def _delete_partial_ingest_document(self, job: KnowledgeIngestJob) -> None:
        """删除入库取消或失败时产生的部分文档数据。

        级联删除与文档关联的 DiscoverySuggestion、Concept、Chunk、Bucket，
        然后删除文档本身，并将 job.document_id 置空。

        Args:
            job: 入库任务对象。
        """
        document_id = job.document_id
        if not document_id:
            return
        for model in (KnowledgeDiscoverySuggestion, KnowledgeConcept, KnowledgeChunk, KnowledgeBucket):
            self.db.exec(delete(model).where(model.document_id == document_id))
        document = self.db.get(KnowledgeDocument, document_id)
        if document:
            self.db.delete(document)
        job.document_id = None
        self.db.add(job)
        self.db.commit()

    def _update_ingest_stage(
        self,
        job: KnowledgeIngestJob,
        stage: str,
        detail: str = "",
        stats: dict[str, Any] | None = None,
        **changes: Any,
    ) -> None:
        """更新入库任务的阶段和进度。

        根据 stage 名称查找对应进度值，更新 metadata_json 中的阶段标签、详情、
        统计信息和步骤列表，然后调用 _update_job 提交。

        Args:
            job: 入库任务对象。
            stage: 阶段标识（如 "parsing"、"bucketing"、"done"）。
            detail: 当前阶段的人类可读描述。
            stats: 当前阶段的统计信息（如 chunk_count、bucket_count）。
            **changes: 额外要更新的字段（如 status="running"、finished_at=...）。
        """
        if changes.get("status") not in {"failed", "cancelled"}:
            self._raise_if_ingest_cancelled(job)
        stage_def = INGEST_STAGE_BY_KEY.get(stage)
        progress = float(stage_def["progress"]) if stage_def else float(job.progress or 0.0)
        metadata = dict(job.metadata_json or {})
        metadata["stage_label"] = str(stage_def["label"] if stage_def else stage)
        metadata["stage_detail"] = detail
        if stats is not None:
            metadata["stage_stats"] = stats
        metadata["ingest_steps"] = _ingest_steps_for(stage, progress, changes.get("status") or job.status)
        job.metadata_json = metadata
        self._update_job(job, stage=stage, progress=progress, **changes)

    def _clear_embedded_content(self, job: KnowledgeIngestJob) -> None:
        """清除任务 metadata_json 中嵌入的文件内容（Base64），释放存储空间。

        入库完成后或失败后，文件内容不再需要保存在任务记录中，应清除以减小体积。

        Args:
            job: 入库任务对象。
        """
        metadata = dict(job.metadata_json or {})
        metadata.pop("content_base64", None)
        job.metadata_json = metadata
        job.updated_at = utc_now()
        self.db.add(job)
        self.db.commit()


def bucket_read(row: KnowledgeBucket) -> KnowledgeBucketRead:
    """将 KnowledgeBucket ORM 行转换为 API 响应模型 KnowledgeBucketRead。

    Args:
        row: KnowledgeBucket 数据库行。

    Returns:
        可序列化的 KnowledgeBucketRead 对象。
    """
    metadata = row.metadata_json or {}
    return KnowledgeBucketRead(
        id=row.id,
        tenant_id=row.tenant_id,
        knowledge_base_id=row.knowledge_base_id,
        document_id=row.document_id,
        bucket_key=row.bucket_key,
        title=row.title,
        summary=row.summary,
        token_estimate=row.token_estimate,
        chunk_count=int(metadata.get("chunk_count") or len(metadata.get("representative_chunk_ids") or []) or 0),
        status="ready",
        metadata=metadata,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def chunk_read(row: KnowledgeChunk) -> KnowledgeChunkRead:
    """将 KnowledgeChunk ORM 行转换为 API 响应模型 KnowledgeChunkRead。

    Args:
        row: KnowledgeChunk 数据库行。

    Returns:
        可序列化的 KnowledgeChunkRead 对象。
    """
    return KnowledgeChunkRead(
        id=row.id,
        tenant_id=row.tenant_id,
        knowledge_base_id=row.knowledge_base_id,
        document_id=row.document_id,
        bucket_id=row.bucket_id,
        chunk_index=row.chunk_index,
        content=row.content,
        summary=row.summary,
        source_ref=row.source_ref,
        metadata=row.metadata_json or {},
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _normalize_text(text: str) -> str:
    """规范化文本：统一换行符、去除行尾空白、压缩连续空行。

    处理步骤：
    1. 将 \\r\\n 和 \\r 统一为 \\n。
    2. 去除每行行尾空白。
    3. 连续空行压缩为单个空行。
    4. 去除首尾空白。

    Args:
        text: 原始文本。

    Returns:
        规范化后的文本。
    """
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    compact: list[str] = []
    blank = False
    for line in lines:
        if not line.strip():
            if not blank:
                compact.append("")
            blank = True
            continue
        compact.append(line)
        blank = False
    return "\n".join(compact).strip()


def _split_sections(text: str) -> list[str]:
    """将全文按段落组拆分为多个章节文本块。

    按段落分隔，遇到标题或超过 PARAGRAPH_GROUP_CHARS 上限时开新块，
    每个段落组内部还会先拆分超大段落。用于无法识别标题层级时的兜底分章。

    Args:
        text: 规范化后的全文。

    Returns:
        章节文本块列表；若文本为空则返回 ``[text[:BUCKET_SECTION_CHARS]]``。
    """
    paragraphs = _paragraph_blocks(text)
    if not paragraphs:
        return [text[:BUCKET_SECTION_CHARS]]

    sections: list[str] = []
    current: list[str] = []
    current_len = 0

    for paragraph in paragraphs:
        parts = _split_large_paragraph(paragraph, BUCKET_SECTION_CHARS)
        for part in parts:
            is_heading = _looks_like_heading(part)
            projected = current_len + len(part) + (2 if current else 0)
            if current and (is_heading or projected > PARAGRAPH_GROUP_CHARS):
                sections.append("\n\n".join(current).strip())
                current = []
                current_len = 0
            current.append(part)
            current_len += len(part) + (2 if current_len else 0)

    if current:
        sections.append("\n\n".join(current).strip())
    return [section for section in sections if section.strip()] or [text[:BUCKET_SECTION_CHARS]]


def _chunk_text(text: str, max_chars: int) -> list[str]:
    """将文本按 max_chars 上限切分为多个 Chunk。

    优先按段落和句子边界切分（保持语义完整性），当无法找到合适边界时
    退化为硬切分。确保每个 Chunk 的字符数不超过 max_chars。

    Args:
        text: 待切分文本。
        max_chars: 每个 Chunk 的字符数上限。

    Returns:
        Chunk 文本列表；输入为空时返回 ``[text.strip()]``。
    """
    paragraphs = _paragraph_blocks(text)
    if paragraphs:
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for paragraph in paragraphs:
            for part in _split_large_paragraph(paragraph, max_chars):
                projected = current_len + len(part) + (2 if current else 0)
                if current and projected > max_chars:
                    chunks.append("\n\n".join(current).strip())
                    current = []
                    current_len = 0
                current.append(part)
                current_len += len(part) + (2 if current_len else 0)
        if current:
            chunks.append("\n\n".join(current).strip())
        return [chunk for chunk in chunks if chunk.strip()]

    chunks: list[str] = []
    cursor = 0
    while cursor < len(text):
        end = min(len(text), cursor + max_chars)
        if end < len(text):
            boundary = max(text.rfind("\n\n", cursor, end), text.rfind("\n", cursor, end))
            if boundary > cursor + max_chars // 2:
                end = boundary
        chunk = text[cursor:end].strip()
        if chunk:
            chunks.append(chunk)
        cursor = max(end, cursor + 1)
    return chunks or [text.strip()]


def _paragraph_blocks(text: str) -> list[str]:
    """将文本拆分为段落块列表。

    先规范化文本，再按空行分隔为段落块。如果只有一个块（即文档没有空行），
    则退化为按行分隔，确保长文档仍能被增量处理。

    Args:
        text: 原始文本。

    Returns:
        段落块列表；文本为空时返回空列表。
    """
    normalized = _normalize_text(text)
    if not normalized:
        return []
    blocks = [block.strip() for block in re.split(r"\n\s*\n+", normalized) if block.strip()]
    if len(blocks) > 1:
        return blocks
    # Plain exported documents often lose blank lines. Fall back to single lines
    # so long documents still get incremental discovery instead of one huge block.
    return [line.strip() for line in normalized.splitlines() if line.strip()]


def _looks_like_heading(text: str) -> bool:
    """判断文本是否看起来像标题行。

    判定标准：以 # 开头（Markdown 标题），或长度不超过 48 字符且以中英文冒号结尾。

    Args:
        text: 待检测文本。

    Returns:
        是标题返回 True，否则返回 False。
    """
    stripped = text.strip()
    return stripped.startswith("#") or (
        len(stripped) <= 48 and stripped.endswith(("：", ":"))
    )


def _split_large_paragraph(text: str, max_chars: int) -> list[str]:
    """将超长段落按句子边界拆分为多个片段。

    先按中英文句末标点（。！？.!?；;）切分为句子，再按 max_chars 上限累积组装。
    若无法切分为多个句子（如纯英文长串），则退化为硬切分。

    Args:
        text: 待拆分的段落文本。
        max_chars: 每个片段的字符数上限。

    Returns:
        拆分后的文本片段列表。
    """
    if len(text) <= max_chars:
        return [text]
    sentences = [item.strip() for item in re.split(r"(?<=[。！？.!?；;])\s*", text) if item.strip()]
    if len(sentences) <= 1:
        return _hard_split_text(text, max_chars)
    parts: list[str] = []
    current: list[str] = []
    current_len = 0
    for sentence in sentences:
        if len(sentence) > max_chars:
            if current:
                parts.append("".join(current).strip())
                current = []
                current_len = 0
            parts.extend(_hard_split_text(sentence, max_chars))
            continue
        if current and current_len + len(sentence) > max_chars:
            parts.append("".join(current).strip())
            current = []
            current_len = 0
        current.append(sentence)
        current_len += len(sentence)
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part.strip()]


def _hard_split_text(text: str, max_chars: int) -> list[str]:
    """硬切分：按固定字符数等长拆分文本，不考虑语义边界。

    Args:
        text: 待切分文本。
        max_chars: 每个片段的字符数上限。

    Returns:
        切分后的文本片段列表。
    """
    return [text[index:index + max_chars].strip() for index in range(0, len(text), max_chars) if text[index:index + max_chars].strip()]


def _ingest_steps_for(stage: str, progress: float, status: str) -> list[dict[str, Any]]:
    """生成入库步骤的进度状态列表，供前端渲染进度条。

    根据当前阶段和进度值，为每个入库阶段标注状态：
    - 正在运行的阶段标记为 "running"。
    - 已完成的阶段标记为 "done"。
    - 未开始的阶段标记为 "pending"。
    - 失败或取消时，已完成阶段标记为 "done"，其余为 "pending"。

    Args:
        stage: 当前阶段标识。
        progress: 当前累计进度值（0.0 ~ 1.0）。
        status: 当前任务状态（如 "running"、"failed"、"cancelled"）。

    Returns:
        带有 status 字段的阶段步骤列表。
    """
    if stage == "failed" or status in {"failed", "cancelled"}:
        return [
            {
                **item,
                "status": "done" if float(item["progress"]) < progress else "pending",
            }
            for item in INGEST_STAGES
        ]
    return [
        {
            **item,
            "status": (
                "running"
                if item["key"] == stage
                else "done"
                if float(item["progress"]) < progress or (stage == "done" and item["key"] == "done")
                else "pending"
            ),
        }
        for item in INGEST_STAGES
    ]


def _build_section_nodes(text: str) -> list[dict[str, Any]]:
    """从全文构建层级化的章节导航树。

    通过正则识别标题（Markdown 标题、中文章节编号、数字编号等），
    利用栈维护父子层级关系。正文内容在标题之间累积，超过 SECTION_TARGET_CHARS
    时自动分拆为同级别的新节点。每个节点包含 section_id、level、parent_id、
    path、summary、content、source_span、anchor_entities 等字段。

    Args:
        text: 规范化后的全文。

    Returns:
        章节节点列表；若无法识别标题则将全文作为一个默认节点返回。
    """
    paragraphs = _paragraph_blocks(text)
    if not paragraphs:
        return [
            {
                "section_id": "sec_1",
                "node_type": "section",
                "level": 1,
                "title": "全文",
                "parent_id": None,
                "path": "全文",
                "section_order": 1,
                "summary": _summarize_text(text, 260),
                "content": text,
                "source_span": {"start_paragraph": 0, "end_paragraph": 0},
                "anchor_entities": _extract_anchor_entities(text),
            }
        ]

    nodes: list[dict[str, Any]] = []
    stack: dict[int, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    current_parts: list[str] = []
    current_start = 0

    def flush(end_paragraph: int) -> None:
        nonlocal current, current_parts, current_start
        if current is None:
            return
        content = "\n\n".join(part for part in current_parts if part.strip()).strip()
        if not content:
            content = str(current.get("title") or "")
        current["content"] = content
        current["summary"] = _section_summary(current.get("title"), content)
        current["anchor_entities"] = _extract_anchor_entities(content)
        current["source_span"] = {"start_paragraph": current_start, "end_paragraph": end_paragraph}
        nodes.append(current)
        current = None
        current_parts = []

    def start_section(title: str, level: int, paragraph_index: int, parent_id: str | None = None) -> dict[str, Any]:
        section_id = f"sec_{len(nodes) + 1}"
        parent = next((stack[item] for item in sorted(stack, reverse=True) if item < level), None)
        resolved_parent_id = parent_id if parent_id is not None else (parent.get("section_id") if parent else None)
        parent_path = str(parent.get("path") or "") if parent else ""
        path = f"{parent_path} / {title}" if parent_path else title
        return {
            "section_id": section_id,
            "node_type": "section",
            "level": level,
            "title": title,
            "parent_id": resolved_parent_id,
            "path": path,
            "section_order": len(nodes) + 1,
            "summary": "",
            "content": "",
            "source_span": {"start_paragraph": paragraph_index, "end_paragraph": paragraph_index},
            "anchor_entities": [],
        }

    for index, paragraph in enumerate(paragraphs):
        heading = _heading_info(paragraph)
        if heading:
            flush(index - 1)
            level, title = heading
            current = start_section(title, level, index)
            current_parts = [paragraph]
            current_start = index
            stack = {key: value for key, value in stack.items() if key < level}
            stack[level] = current
            continue

        if current is None:
            current = start_section(f"段落组 {len(nodes) + 1}", 1, index)
            current_parts = []
            current_start = index

        projected = sum(len(part) for part in current_parts) + len(paragraph)
        if current_parts and projected > SECTION_TARGET_CHARS:
            parent_id = str(current.get("parent_id") or "")
            title = str(current.get("title") or f"段落组 {len(nodes) + 1}")
            level = int(current.get("level") or 1)
            flush(index - 1)
            current = start_section(title, level, index, parent_id or None)
            current_parts = []
            current_start = index
        current_parts.append(paragraph)

    flush(len(paragraphs) - 1)
    if not nodes:
        return _build_section_nodes(text[:SECTION_TARGET_CHARS])
    return nodes


def _build_document_card(
    title: str,
    filename: str,
    file_type: str,
    text: str,
    section_nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    """构建文档摘要卡片，包含标题、摘要、大纲、关键实体和适用场景。

    从章节节点中提取大纲和用例，从全文中提取关键实体，
    生成供检索路由和前端展示使用的文档卡片。

    Args:
        title: 文档标题。
        filename: 文件名。
        file_type: 文件类型。
        text: 全文内容。
        section_nodes: 章节节点列表。

    Returns:
        文档摘要卡片字典。
    """
    outline = [
        {
            "section_id": node.get("section_id"),
            "title": node.get("title"),
            "path": node.get("path"),
            "level": node.get("level"),
            "summary": node.get("summary"),
        }
        for node in section_nodes[:60]
    ]
    summary_source = "\n".join(str(node.get("summary") or "") for node in section_nodes[:12])
    entities = _extract_anchor_entities(text[:12000])
    use_cases = [
        str(node.get("title"))
        for node in section_nodes
        if node.get("title")
    ][:8]
    return {
        "title": title,
        "filename": filename,
        "file_type": file_type,
        "summary": _summarize_text(summary_source or text, 520),
        "outline": outline,
        "applicable_scenarios": use_cases,
        "key_entities": entities[:24],
        "char_count": len(text),
        "section_count": len(section_nodes),
    }


def _heading_info(text: str) -> tuple[int, str] | None:
    """从文本行中解析标题层级和标题文本。

    支持的标题格式：
    - Markdown 标题（``# 标题``，层级 = # 数量，1~6）。
    - 中文章节编号（``第X章/节/篇/部 标题``，层级 = 1）。
    - 数字编号标题（``1.2.3 标题``，层级 = 点数 + 1，最多 5）。
    - 冒号结尾的短行（``XXX：``，层级 = 2）。

    Args:
        text: 待检测的文本行。

    Returns:
        ``(level, title)`` 元组；若不是标题则返回 None。
    """
    stripped = text.strip()
    if not stripped:
        return None
    markdown = re.match(r"^(#{1,6})\s+(.+)$", stripped)
    if markdown:
        return len(markdown.group(1)), markdown.group(2).strip()
    chapter = re.match(r"^第[一二三四五六七八九十百千万0-9]+[章节篇部分]\s*[：:\-、]?\s*(.+)?$", stripped)
    if chapter:
        return 1, (chapter.group(1) or stripped).strip()
    numbered = re.match(r"^(\d+(?:\.\d+){0,4})[、.\s]+(.{2,80})$", stripped)
    if numbered:
        return min(numbered.group(1).count(".") + 1, 5), numbered.group(2).strip()
    if _looks_like_heading(stripped):
        return 2, stripped.strip("# ：:")
    return None


def _section_summary(title: Any, content: str) -> str:
    """生成章节摘要：标题前缀 + 正文摘要。

    如果标题不在摘要中出现，则在前面加上标题前缀。
    结果截断到 320 字符。

    Args:
        title: 章节标题。
        content: 章节正文。

    Returns:
        章节摘要字符串。
    """
    prefix = str(title or "").strip()
    summary = _summarize_text(content, 260)
    if prefix and prefix not in summary:
        return f"{prefix}：{summary}"[:320]
    return summary


def _summarize_text(text: str, max_chars: int) -> str:
    """生成文本摘要：压缩空白后按 max_chars 截断。

    尽量在中文句号、分号处截断以保持语义完整，找不到合适边界时直接按字符数截断，
    末尾追加省略号。

    Args:
        text: 原始文本。
        max_chars: 摘要的最大字符数。

    Returns:
        截断后的摘要字符串。
    """
    compact = re.sub(r"\s+", " ", text or "").strip()
    if len(compact) <= max_chars:
        return compact
    end = compact.rfind("。", 0, max_chars)
    if end < max_chars // 2:
        end = compact.rfind("；", 0, max_chars)
    if end < max_chars // 2:
        end = max_chars
    return compact[:end].strip() + "..."


def _extract_anchor_entities(text: str) -> list[str]:
    """从文本中提取锚点实体（关键词）用于索引和路由。

    通过三种正则模式提取候选：
    - 英文/数字标识符（2 字符以上）。
    - 中文短语（2~12 字符）。
    - 版本号格式的数字串。

    去重后最多返回 40 个。

    Args:
        text: 原始文本。

    Returns:
        去重后的锚点实体列表。
    """
    candidates: list[str] = []
    patterns = [
        r"[A-Za-z][A-Za-z0-9_.:/-]{2,}",
        r"[\u4e00-\u9fff]{2,12}",
        r"\d+(?:\.\d+)+",
    ]
    for pattern in patterns:
        candidates.extend(re.findall(pattern, text or ""))
    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        value = str(item).strip(".,，。；;：:()（）[]【】")
        if len(value) < 2 or value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= 40:
            break
    return result


def _structure_bucket_specs(section_nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """基于章节路径的顶级分组，生成结构化 Bucket 规格。

    按 section path 的第一段（顶级章节名）将章节分组，每组生成一个 Bucket。

    Args:
        section_nodes: 章节节点列表。

    Returns:
        结构化 Bucket 规格列表；输入为空时返回空列表。
    """
    if not section_nodes:
        return []
    top_groups: dict[str, list[dict[str, Any]]] = {}
    for node in section_nodes:
        path = str(node.get("path") or node.get("title") or "未命名章节")
        top = path.split(" / ")[0].strip() or "未命名章节"
        top_groups.setdefault(top, []).append(node)
    specs: list[dict[str, Any]] = []
    for index, (title, nodes) in enumerate(top_groups.items()):
        content = "\n\n".join(str(node.get("content") or "") for node in nodes)
        section_paths = [str(node.get("path") or node.get("title") or "") for node in nodes]
        specs.append(
            {
                "bucket_key": _safe_key(title, fallback=f"structure_{index + 1}"),
                "title": title,
                "summary": _summarize_text("\n".join(str(node.get("summary") or "") for node in nodes), 420),
                "content": content[:BUCKET_SECTION_CHARS],
                "section_ids": [str(node.get("section_id")) for node in nodes if node.get("section_id")],
                "section_paths": section_paths,
                "bucket_type": "structure",
                "concept_type": "Topic",
                "applicable_query_types": ["answer", "policy_check"],
            }
        )
    return specs


def _normalize_llm_bucket_specs(
    buckets: list[dict[str, Any]],
    section_nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """将 LLM 返回的 Bucket 规格规范化为统一的内部格式。

    处理 LLM 返回数据中的 section_indexes（索引）或 section_ids（字符串），
    映射到实际章节节点，合并正文内容，补全缺失的摘要和类型。

    Args:
        buckets: LLM 返回的原始 Bucket 列表。
        section_nodes: 章节节点列表（用于索引和 ID 映射）。

    Returns:
        规范化后的 Bucket 规格列表。
    """
    specs: list[dict[str, Any]] = []
    section_by_index = {index: node for index, node in enumerate(section_nodes)}
    section_by_id = {str(node.get("section_id")): node for node in section_nodes}
    for index, item in enumerate(buckets):
        section_ids = [str(value) for value in item.get("section_ids", []) if value]
        if not section_ids and isinstance(item.get("section_indexes"), list):
            section_ids = [
                str(section_by_index[int(value)].get("section_id"))
                for value in item.get("section_indexes", [])
                if isinstance(value, int) and value in section_by_index
            ]
        nodes = [section_by_id[section_id] for section_id in section_ids if section_id in section_by_id]
        content = "\n\n".join(str(node.get("content") or "") for node in nodes)
        title = str(item.get("title") or f"任务桶 {index + 1}")
        specs.append(
            {
                "bucket_key": str(item.get("bucket_key") or _safe_key(title, f"task_{index + 1}")),
                "title": title,
                "summary": str(item.get("summary") or _summarize_text(content, 420)),
                "content": content[:BUCKET_SECTION_CHARS],
                "section_ids": section_ids,
                "section_paths": [str(node.get("path") or node.get("title") or "") for node in nodes],
                "bucket_type": str(item.get("bucket_type") or "task"),
                "concept_type": str(item.get("concept_type") or "Topic"),
                "applicable_query_types": item.get("applicable_query_types")
                if isinstance(item.get("applicable_query_types"), list)
                else ["answer", "policy_check", "tool_discovery", "skill_discovery"],
            }
        )
    return specs


def _unique_bucket_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对 Bucket 规格列表去重。

    去重依据：bucket_key 或 section_ids 签名。已见过的 key 或相同的 section_ids
    组合会被跳过。

    Args:
        specs: 待去重的 Bucket 规格列表。

    Returns:
        去重后的 Bucket 规格列表。
    """
    result: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_sections: set[tuple[str, ...]] = set()
    for item in specs:
        key = str(item.get("bucket_key") or "").strip()
        section_ids = tuple(sorted(str(value) for value in item.get("section_ids", []) if value))
        signature = section_ids or (key,)
        if key in seen_keys or signature in seen_sections:
            continue
        seen_keys.add(key)
        seen_sections.add(signature)
        result.append(item)
    return result


def _unique_strings(items: list[str]) -> list[str]:
    """对字符串列表去重并保留顺序。

    Args:
        items: 待去重的字符串列表。

    Returns:
        去重后的字符串列表（保留首次出现的顺序）。
    """
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _bucket_quality(spec: dict[str, Any], section_ids: list[str], content: str) -> dict[str, Any]:
    """评估 Bucket 的质量指标。

    检查是否缺少来源章节、摘要、正文是否过短，生成质量状态和告警列表。

    Args:
        spec: Bucket 规格字典。
        section_ids: 关联的章节 ID 列表。
        content: Bucket 正文内容。

    Returns:
        包含 status、warnings、has_source_sections、has_summary、content_chars 的质量字典。
    """
    warnings: list[str] = []
    if not section_ids:
        warnings.append("missing_source_section")
    if not str(spec.get("summary") or "").strip():
        warnings.append("missing_summary")
    if len(content.strip()) < 40:
        warnings.append("content_too_short")
    return {
        "status": "warning" if warnings else "ready",
        "warnings": warnings,
        "has_source_sections": bool(section_ids),
        "has_summary": bool(str(spec.get("summary") or "").strip()),
        "content_chars": len(content or ""),
    }


def _fallback_bucket_specs(sections: list[str], section_nodes: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """生成兜底 Bucket 规格。

    当 LLM 和结构化分桶都没有产出时，使用此函数生成最简单的 Bucket。
    若有章节节点则回退到结构化分桶，否则按文本切分块生成。

    Args:
        sections: 文本切分块列表。
        section_nodes: 可选的章节节点列表。

    Returns:
        Bucket 规格列表。
    """
    if section_nodes:
        return _structure_bucket_specs(section_nodes)
    return [
        {
            "bucket_key": f"bucket_{index + 1}",
            "title": _guess_title(section, index),
            "summary": section[:360],
            "content": section,
            "bucket_type": "structure",
            "concept_type": "Topic",
            "section_ids": [],
            "section_paths": [],
        }
        for index, section in enumerate(sections)
    ]


def _document_card_for_search(row: KnowledgeDocument) -> dict[str, Any]:
    """从文档行提取检索结果用的文档卡片。

    Args:
        row: KnowledgeDocument 数据库行。

    Returns:
        包含 id、title、summary、outline、key_entities 等字段的字典。
    """
    metadata = row.metadata_json or {}
    card = metadata.get("document_card") if isinstance(metadata.get("document_card"), dict) else {}
    return {
        "id": row.id,
        "knowledge_base_id": row.knowledge_base_id,
        "title": card.get("title") or row.title or row.filename,
        "filename": row.filename,
        "file_type": row.file_type,
        "summary": card.get("summary") or "",
        "outline": card.get("outline") if isinstance(card.get("outline"), list) else [],
        "key_entities": card.get("key_entities") if isinstance(card.get("key_entities"), list) else [],
        "section_count": card.get("section_count"),
        "chunk_count": row.chunk_count,
        "updated_at": row.updated_at.isoformat(),
    }


def _document_card_for_route(row: KnowledgeDocument) -> dict[str, Any]:
    """从文档行提取用于 LLM 路由的精简文档卡片。

    相比 _document_card_for_search，此版本对标题、摘要等字段做了截断，
    outline 和 key_entities 转为标签列表，以减少 LLM token 消耗。

    Args:
        row: KnowledgeDocument 数据库行。

    Returns:
        精简后的文档卡片字典。
    """
    card = _document_card_for_search(row)
    return {
        "id": card["id"],
        "knowledge_base_id": card["knowledge_base_id"],
        "title": _summarize_text(str(card.get("title") or ""), 120),
        "filename": _summarize_text(str(card.get("filename") or ""), 120),
        "file_type": card.get("file_type"),
        "summary": _summarize_text(str(card.get("summary") or ""), 160),
        "outline": _route_labels(card.get("outline"), 2, 60),
        "key_entities": _route_labels(card.get("key_entities"), 3, 30),
        "section_count": card.get("section_count"),
        "chunk_count": card.get("chunk_count"),
    }


def _route_labels(value: object, limit: int, char_limit: int) -> list[str]:
    """从列表中提取标签字符串，用于 LLM 路由 payload。

    列表元素可以是字典（提取 path/title/name/heading/label 字段）或字符串。
    每个标签截断到 char_limit 字符，最多取 limit 个。

    Args:
        value: 原始列表数据。
        limit: 最多提取的标签数。
        char_limit: 每个标签的字符上限。

    Returns:
        标签字符串列表。
    """
    if not isinstance(value, list):
        return []
    labels: list[str] = []
    for item in value[:limit]:
        if isinstance(item, dict):
            label = next(
                (
                    str(item.get(key) or "").strip()
                    for key in ("path", "title", "name", "heading", "label")
                    if item.get(key)
                ),
                "",
            )
            if not label:
                label = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        else:
            label = str(item or "").strip()
        if label:
            labels.append(_summarize_text(label, char_limit))
    return labels


def _score_documents(query: str, documents: list[KnowledgeDocument]) -> list[KnowledgeDocument]:
    """对候选文档列表按词法相关性打分并排序。

    使用文档标题、文件名和文档卡片的 JSON 文本作为匹配字段。

    Args:
        query: 用户查询字符串。
        documents: 候选文档列表。

    Returns:
        按相关性降序排列的文档列表（过滤掉低于阈值的）。
    """
    scored: list[tuple[float, KnowledgeDocument]] = []
    for row in documents:
        score = _score_text(
            query,
            " ".join(
                [
                    row.title or "",
                    row.filename,
                    json.dumps((row.metadata_json or {}).get("document_card", {}), ensure_ascii=False)[:4000],
                ]
            ),
        )
        if score >= SEARCH_MIN_DOCUMENT_SCORE:
            scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [row for _score, row in scored]


def _score_buckets(query: str, buckets: list[KnowledgeBucket]) -> list[KnowledgeBucket]:
    """对候选 Bucket 列表按词法相关性打分并排序。

    使用 Bucket 标题、摘要和 metadata_json 的 JSON 文本作为匹配字段。

    Args:
        query: 用户查询字符串。
        buckets: 候选 Bucket 列表。

    Returns:
        按相关性降序排列的 Bucket 列表（过滤掉低于阈值的）。
    """
    scored: list[tuple[float, KnowledgeBucket]] = []
    for row in buckets:
        score = _score_text(
            query,
            " ".join(
                [
                    row.title,
                    row.summary,
                    json.dumps(row.metadata_json or {}, ensure_ascii=False)[:3000],
                ]
            ),
        )
        if score >= SEARCH_MIN_BUCKET_SCORE:
            scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [row for _score, row in scored]


def _rank_chunks(
    query: str,
    chunks: list[KnowledgeChunk],
    selected_buckets: list[KnowledgeBucket],
    expanded_sections: list[dict[str, Any]],
) -> list[KnowledgeChunk]:
    """对 Chunk 列表进行综合排序。

    排序权重 = 文本相关性分数 + 章节加成（若 Chunk 属于展开的章节则 +2）。
    次级排序按 Bucket 顺序和 chunk_index。
    过滤掉文本相关性低于阈值的 Chunk。

    Args:
        query: 用户查询字符串。
        chunks: 候选 Chunk 列表。
        selected_buckets: 已选中的 Bucket 列表（用于确定优先级）。
        expanded_sections: 已展开的章节列表（用于加成计算）。

    Returns:
        按综合权重降序排列的 Chunk 列表。
    """
    bucket_rank = {bucket.id: index for index, bucket in enumerate(selected_buckets)}
    section_ids = {str(item.get("section_id")) for item in expanded_sections if item.get("section_id")}

    scored: list[tuple[tuple[float, int, int], KnowledgeChunk]] = []
    for chunk in chunks:
        metadata = chunk.metadata_json or {}
        section_bonus = 2 if str(metadata.get("section_id")) in section_ids else 0
        text_score = _score_text(query, f"{chunk.summary or ''} {chunk.content}")
        if text_score < SEARCH_MIN_CHUNK_SCORE:
            continue
        scored.append(((text_score + section_bonus, -bucket_rank.get(chunk.bucket_id, 999), -chunk.chunk_index), chunk))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [chunk for _score, chunk in scored]


def _build_evidence_pack(query: str, chunks: list[KnowledgeChunk]) -> list[dict[str, Any]]:
    """从已排序的 Chunk 列表构建证据包。

    每个 Chunk 的相关性分数需达到 SEARCH_MIN_EVIDENCE_SCORE 阈值才被纳入。
    证据包中的每条记录包含 chunk_id、来源路径、摘要、正文摘录、相关性分数等。

    Args:
        query: 用户查询字符串。
        chunks: 已排序的 Chunk 列表。

    Returns:
        证据包列表。
    """
    evidence: list[dict[str, Any]] = []
    for chunk in chunks:
        score = _score_text(query, f"{chunk.summary or ''} {chunk.content}")
        if score < SEARCH_MIN_EVIDENCE_SCORE:
            continue
        evidence.append(
            {
                "chunk_id": chunk.id,
                "document_id": chunk.document_id,
                "bucket_id": chunk.bucket_id,
                "source_path": chunk.source_ref,
                "section_path": (chunk.metadata_json or {}).get("section_path"),
                "summary": chunk.summary,
                "content": chunk.content[:CITATION_EXCERPT_CHAR_LIMIT],
                "excerpt": chunk.content[:CITATION_EXCERPT_CHAR_LIMIT],
                "relevance_score": round(score, 2),
                "confidence_reason": "引用来源摘要、章节路径或正文与查询相关",
            }
        )
    return evidence


def _expand_sections(
    documents: list[KnowledgeDocument],
    buckets: list[KnowledgeBucket],
    max_depth: int,
) -> list[dict[str, Any]]:
    """展开选中 Bucket 关联的章节子树。

    从文档的 section_tree 中提取 Bucket 关联的章节，并按 max_depth
    递归收集子章节，形成展开的章节列表供检索使用。

    Args:
        documents: 文档列表（提供 section_tree）。
        buckets: 选中的 Bucket 列表（提供 section_ids）。
        max_depth: 子章节展开的最大深度。

    Returns:
        展开后的章节信息列表（去重）。
    """
    nodes_by_doc: dict[str, dict[str, dict[str, Any]]] = {}
    for document in documents:
        tree = (document.metadata_json or {}).get("section_tree")
        if isinstance(tree, list):
            nodes_by_doc[document.id] = {
                str(node.get("section_id")): node for node in tree if isinstance(node, dict) and node.get("section_id")
            }
    wanted: dict[tuple[str, str], dict[str, Any]] = {}
    for bucket in buckets:
        metadata = bucket.metadata_json or {}
        section_ids = [str(value) for value in metadata.get("section_ids", []) if value]
        doc_nodes = nodes_by_doc.get(bucket.document_id, {})
        for section_id in section_ids:
            node = doc_nodes.get(section_id)
            if node:
                _collect_section_with_children(bucket.document_id, node, doc_nodes, max_depth, wanted, bucket.title)
    return list(wanted.values())


def _collect_section_with_children(
    document_id: str,
    node: dict[str, Any],
    all_nodes: dict[str, dict[str, Any]],
    max_depth: int,
    result: dict[tuple[str, str], dict[str, Any]],
    reason: str,
) -> None:
    """递归收集章节及其子章节到结果字典。

    以 (document_id, section_id) 为键去重，每收集一个节点就递归收集其直接子节点，
    深度减一直到 max_depth 为 0。

    Args:
        document_id: 文档 ID。
        node: 当前章节节点。
        all_nodes: 文档下所有章节的 {section_id: node} 映射。
        max_depth: 剩余递归深度。
        result: 结果收集字典（会被原地修改）。
        reason: 命中原因描述。
    """
    section_id = str(node.get("section_id") or "")
    if not section_id:
        return
    result[(document_id, section_id)] = {
        "document_id": document_id,
        "section_id": section_id,
        "title": node.get("title"),
        "path": node.get("path"),
        "summary": node.get("summary"),
        "level": node.get("level"),
        "source_span": node.get("source_span") or {},
        "reason": f"命中内部索引：{reason}",
    }
    if max_depth <= 0:
        return
    children = [child for child in all_nodes.values() if child.get("parent_id") == section_id]
    for child in children:
        _collect_section_with_children(document_id, child, all_nodes, max_depth - 1, result, reason)


def _safe_key(text: str, fallback: str) -> str:
    """从文本生成安全的 bucket_key 标识符。

    优先提取 ASCII 单词并用下划线连接；若无 ASCII 字符则使用 hash 值。
    结果压缩连续下划线并截断到 64 字符。

    Args:
        text: 原始文本。
        fallback: 当生成结果为空时的兜底值。

    Returns:
        安全的 bucket_key 字符串。
    """
    ascii_words = re.findall(r"[A-Za-z0-9]+", text)
    if ascii_words:
        key = "_".join(ascii_words).lower()
    else:
        key = "bucket_" + str(abs(hash(text)) % 100000)
    key = re.sub(r"_+", "_", key).strip("_")
    return key[:64] or fallback


def _query_terms(query: str) -> list[str]:
    """从查询字符串中提取检索词列表。

    提取规则：
    - 英文/数字标识符（2 字符以上）。
    - 中文短语（2 字符以上）。
    - 对 3 字符以上的中文短语额外生成 4-gram、3-gram、2-gram 子串。

    结果去重后最多返回 96 个。

    Args:
        query: 用户查询字符串。

    Returns:
        去重后的检索词列表（已转为小写）。
    """
    terms: list[str] = []
    for term in re.findall(r"[A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,}", query or ""):
        normalized = term.lower()
        terms.append(normalized)
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", normalized):
            for size in (4, 3, 2):
                if len(normalized) <= size:
                    continue
                terms.extend(normalized[index : index + size] for index in range(0, len(normalized) - size + 1))
    result: list[str] = []
    seen: set[str] = set()
    for term in terms:
        normalized = term.lower().strip()
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result[:96]


def _score_text(query: str, text: str) -> float:
    """计算查询与文本的词法相关性分数。

    评分逻辑：
    - 完整查询子串匹配：+5.0
    - 每个检索词的出现次数 × 权重（中文 3+ 字符权重更高，英文 5+ 字符权重最高）
    - 单个检索词贡献上限为 8.0

    Args:
        query: 用户查询字符串。
        text: 待匹配的文本。

    Returns:
        相关性分数（浮点数，越大越相关）。
    """
    haystack = (text or "").lower()
    score = 0.0
    if query and query.lower() in haystack:
        score += 5.0
    for term in _query_terms(query):
        count = haystack.count(term)
        if count:
            term_weight = 2.0
            if re.fullmatch(r"[\u4e00-\u9fff]{3,}", term):
                term_weight = 2.5
            if re.fullmatch(r"[\u4e00-\u9fff]{4,}", term):
                term_weight = 3.0
            if len(term) >= 5:
                term_weight = 3.4
            score += min(8.0, count * term_weight)
    return score


def _guess_title(section: str, index: int) -> str:
    """从章节文本块中猜测标题。

    取第一个非空行去除 Markdown 前缀后作为标题，截断到 60 字符。

    Args:
        section: 章节文本块。
        index: 章节序号（用于兜底标题）。

    Returns:
        猜测的标题字符串。
    """
    first_line = next((line.strip("# ").strip() for line in section.splitlines() if line.strip()), "")
    return first_line[:60] if first_line else f"知识主题 {index + 1}"


def _optional_str(value: Any) -> str | None:
    """将任意值转为可选字符串：strip 后为空则返回 None。

    Args:
        value: 原始值。

    Returns:
        非空字符串或 None。
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _default_knowledge_base_version_id(knowledge_base_id: str) -> str:
    """根据知识库 ID 生成默认的知识库版本 ID。

    Args:
        knowledge_base_id: 知识库 ID。

    Returns:
        格式为 ``kbver_{knowledge_base_id}_1_0_0`` 的版本 ID。
    """
    return f"kbver_{knowledge_base_id}_1_0_0"

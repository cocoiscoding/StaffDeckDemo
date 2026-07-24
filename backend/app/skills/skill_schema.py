"""技能蒸馏与编辑模块的数据模型（Schema）定义。

本模块定义了技能（Skill）系统中的全部 Pydantic 数据模型，包括：
- 技能图谱核心结构：``SkillGraphNode``、``SkillGraphEdge``、``SkillCard``
- 工具建议：``ToolSuggestion``
- CRUD 请求/响应模型
- 蒸馏（Distill）请求/响应模型
- 改写（Rewrite）请求/响应模型
- 文件提取（FileExtract）请求/响应模型

SkillCard 是技能的完整结构化表示，包含一个有向图（nodes + edges），
代表对话流程中从起始节点到终态节点的推进逻辑。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SkillGraphNode(BaseModel):
    """技能图谱中的单个节点。

    每个节点代表对话流程中的一个处理步骤，可能是信息收集、工具调用、
    条件判断或最终回复。

    Attributes:
        node_id: 节点唯一标识。
        type: 节点类型，如 ``collect_info``、``decision``、``response``。
        name: 节点显示名称。
        instruction: 节点指令说明（给 LLM 的行为指导）。
        optional: 该节点是否可选（可跳过）。
        condition: 进入该节点的条件表达式（可选）。
        expected_user_info: 期望从用户获取的信息字段列表。
        allowed_actions: 允许的动作列表，如 ``ask_user``、``answer_user``、
            ``call_tool:xxx``、``handoff_human`` 等。
        knowledge_scope: 知识检索范围配置。
        retry_policy: 重试策略配置。
        metadata: 附加元数据。
    """

    node_id: str
    type: str = "collect_info"
    name: str
    instruction: str = ""
    optional: bool = False
    condition: Optional[str] = None
    expected_user_info: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)
    knowledge_scope: dict[str, Any] = Field(default_factory=dict)
    retry_policy: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillGraphEdge(BaseModel):
    """技能图谱中的有向边，表示节点间的推进关系。

    Attributes:
        source_node_id: 源节点 ID。
        next_node_id: 目标节点 ID。
        condition: 进入该边的条件表达式（可选，用于条件分支）。
        priority: 优先级（数值越大优先级越高），用于多出边的排序。
        label: 边的显示标签（可选）。
    """

    source_node_id: str
    next_node_id: str
    condition: Optional[str] = None
    priority: int = 0
    label: Optional[str] = None


class SkillCard(BaseModel):
    """技能卡片——技能的完整结构化表示。

    包含技能的元信息、触发条件、目标、所需信息、对话图谱（节点 + 边）、
    槽位填充策略、回复规则以及中断处理策略等。

    使用 ``model_validator`` 在模型校验后对图谱完整性做交叉验证。

    Attributes:
        skill_id: 技能唯一标识。
        name: 技能名称。
        version: 版本号。
        business_domain: 所属业务领域。
        description: 技能描述。
        trigger_intents: 触发意图列表。
        user_utterance_examples: 用户话术示例列表。
        goal: 技能目标列表。
        required_info: 必须收集的信息字段列表。
        slot_filling_policy: 槽位填充策略配置。
        response_rules: 回复规则列表。
        nodes: 技能图谱节点列表。
        edges: 技能图谱边列表。
        start_node_id: 起始节点 ID。
        terminal_node_ids: 终态节点 ID 列表。
        interruption_policy: 中断处理策略。
    """

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    name: str
    version: str = "1.0.0"
    business_domain: Optional[str] = None
    description: str = ""
    trigger_intents: list[str] = Field(default_factory=list)
    user_utterance_examples: list[str] = Field(default_factory=list)
    goal: list[str] = Field(default_factory=list)
    required_info: list[str] = Field(default_factory=list)
    slot_filling_policy: dict[str, Any] = Field(default_factory=dict)
    response_rules: list[str] = Field(default_factory=list)
    nodes: list[SkillGraphNode] = Field(default_factory=list)
    edges: list[SkillGraphEdge] = Field(default_factory=list)
    start_node_id: str
    terminal_node_ids: list[str] = Field(default_factory=list)
    interruption_policy: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_graph(self) -> "SkillCard":
        """在模型实例化后校验图谱结构的完整性和一致性。

        校验内容：
        1. 至少包含一个节点。
        2. 所有节点 ID 必须唯一。
        3. ``start_node_id`` 必须引用已存在的节点。
        4. ``terminal_node_ids`` 至少包含一个节点 ID，且都引用已存在的节点。
        5. 所有边的 ``source_node_id`` 和 ``next_node_id`` 都引用已存在的节点。

        Returns:
            校验通过后的 SkillCard 实例。

        Raises:
            ValueError: 图谱结构不合法时抛出，附带具体的错误描述。
        """
        if not self.nodes:
            raise ValueError("Skill graph requires at least one node.")
        node_ids = [node.node_id for node in self.nodes]
        duplicate_ids = sorted({node_id for node_id in node_ids if node_ids.count(node_id) > 1})
        if duplicate_ids:
            raise ValueError(f"Skill graph node_id must be unique: {', '.join(duplicate_ids)}")
        node_id_set = set(node_ids)
        if self.start_node_id not in node_id_set:
            raise ValueError("start_node_id must reference an existing node.")
        if not self.terminal_node_ids:
            raise ValueError("terminal_node_ids must contain at least one node id.")
        missing_terminal_ids = [node_id for node_id in self.terminal_node_ids if node_id not in node_id_set]
        if missing_terminal_ids:
            raise ValueError(f"terminal_node_ids reference missing nodes: {', '.join(missing_terminal_ids)}")
        for edge in self.edges:
            if edge.source_node_id not in node_id_set:
                raise ValueError(f"edge source_node_id references missing node: {edge.source_node_id}")
            if edge.next_node_id not in node_id_set:
                raise ValueError(f"edge next_node_id references missing node: {edge.next_node_id}")
        return self


class ToolSuggestion(BaseModel):
    """工具建议——LLM 从技能文档中抽取的工具提及信息。

    每个工具建议会尝试匹配到现有的已配置工具（``resolution_status="existing"``），
    或标记为需要新增的候选工具（``"new_candidate"``），
    或标记为信息不完整无法新增（``"incomplete"``）。

    Attributes:
        name: 工具名称。
        display_name: 显示名称。
        description: 工具描述。
        bucket: 工具所属分类。
        method: HTTP 方法。
        url: 接口地址。
        input_schema: 输入参数的 JSON Schema。
        output_schema: 输出结果的 JSON Schema。
        sample_arguments: 示例参数。
        source_excerpt: 原文中的引用片段。
        probe_result: 探测结果（可选）。
        reason: 工具建议的理由。
        resolution_status: 解析状态——``existing`` / ``new_candidate`` / ``incomplete``。
        matched_tool_id: 匹配到的现有工具 ID（如有）。
        matched_tool_name: 匹配到的现有工具名称（如有）。
        matched_tool_display_name: 匹配到的现有工具显示名（如有）。
        missing_reason: 当状态为 ``incomplete`` 时的缺失原因。
    """

    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    bucket: str = "技能自发现工具"
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    url: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    sample_arguments: dict[str, Any] = Field(default_factory=dict)
    source_excerpt: Optional[str] = None
    probe_result: Optional[dict[str, Any]] = None
    reason: str = ""
    resolution_status: Literal["existing", "new_candidate", "incomplete"] = "new_candidate"
    matched_tool_id: Optional[str] = None
    matched_tool_name: Optional[str] = None
    matched_tool_display_name: Optional[str] = None
    missing_reason: Optional[str] = None


class SkillCreateRequest(BaseModel):
    """技能创建请求体。"""

    tenant_id: str
    content: SkillCard
    status: Literal["draft", "published", "archived"] = "draft"


class SkillUpdateRequest(BaseModel):
    """技能更新请求体。"""

    tenant_id: str
    content: SkillCard
    status: Optional[Literal["draft", "published", "archived"]] = None


class SkillRead(BaseModel):
    """技能完整读取视图，包含统计信息和版本管理字段。

    除了 SkillCard 内容外，还包含调用次数、反馈率等运营数据，
    以及分支同步状态等版本管理字段。

    Attributes:
        call_count: 当前版本调用次数。
        positive_feedback_count: 当前版本正面反馈数。
        negative_feedback_count: 当前版本负面反馈数。
        positive_rate / negative_rate: 正面/负面反馈率。
        total_*: 跨所有版本的累计统计。
        recent_*: 近期统计。
        branch_*: 分支同步状态字段。
    """

    id: str
    tenant_id: str
    skill_id: str
    version: str
    name: str
    business_domain: Optional[str]
    description: Optional[str]
    content: SkillCard
    status: str
    call_count: int = 0
    positive_feedback_count: int = 0
    negative_feedback_count: int = 0
    positive_rate: float = 0.0
    negative_rate: float = 0.0
    total_call_count: int = 0
    total_positive_feedback_count: int = 0
    total_negative_feedback_count: int = 0
    total_positive_rate: float = 0.0
    total_negative_rate: float = 0.0
    recent_versions: list[str] = Field(default_factory=list)
    recent_call_count: int = 0
    recent_positive_feedback_count: int = 0
    recent_negative_feedback_count: int = 0
    recent_positive_rate: float = 0.0
    recent_negative_rate: float = 0.0
    agent_id: Optional[str] = None
    branch_status: Optional[str] = None
    branch_sync_state: Optional[str] = None
    branch_base_version: Optional[str] = None
    branch_head_version: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class SkillVersionRead(BaseModel):
    """技能单版本读取视图，比 SkillRead 更精简。

    用于版本历史列表中的单个版本展示。

    Attributes:
        call_count: 该版本调用次数。
        positive_feedback_count / negative_feedback_count: 反馈计数。
        positive_rate / negative_rate: 反馈率。
        agent_id: 关联的 Agent ID。
        branch_sync_state / branch_base_version: 分支同步状态。
    """

    id: str
    tenant_id: str
    skill_id: str
    version: str
    name: str
    business_domain: Optional[str]
    description: Optional[str]
    content: SkillCard
    status: str
    call_count: int = 0
    positive_feedback_count: int = 0
    negative_feedback_count: int = 0
    positive_rate: float = 0.0
    negative_rate: float = 0.0
    agent_id: Optional[str] = None
    branch_sync_state: Optional[str] = None
    branch_base_version: Optional[str] = None
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class SkillDistillRequest(BaseModel):
    """技能蒸馏请求体——从原始流程文档生成结构化技能。"""

    tenant_id: str
    title: str
    raw_content: str
    business_domain: Optional[str] = None
    model_config_id: Optional[str] = None
    available_tools: list[dict[str, Any]] = Field(default_factory=list)


class SkillDistillResponse(BaseModel):
    """技能蒸馏响应体。

    Attributes:
        draft_skill: 蒸馏生成的技能草稿。
        warnings: 处理过程中产生的警告列表。
        tool_suggestions: 工具建议列表。
    """

    draft_skill: SkillCard
    warnings: list[str] = Field(default_factory=list)
    tool_suggestions: list[ToolSuggestion] = Field(default_factory=list)


class SkillRewriteRequest(BaseModel):
    """技能改写请求体——在现有技能基础上做局部修改。

    支持指定改写目标路径（``target_path`` / ``target_paths``），
    仅修改指定部分（如 basic 字段、特定节点等），也可设为 ``all`` 做整体改写。

    Attributes:
        current_skill: 当前技能内容。
        instruction: 改写指令（用户自然语言描述）。
        model_config_id: 使用的模型配置 ID。
        target_path: 单一改写目标路径（兼容旧版）。
        target_paths: 多个改写目标路径列表。
        target_label: 改写目标的显示标签。
        conversation: 对话上下文（最近 12 条）。
        available_tools: 可用工具列表。
    """

    tenant_id: str
    current_skill: SkillCard
    instruction: str
    model_config_id: Optional[str] = None
    target_path: str = "all"
    target_paths: list[str] = Field(default_factory=list)
    target_label: Optional[str] = None
    conversation: list[dict[str, str]] = Field(default_factory=list)
    available_tools: list[dict[str, Any]] = Field(default_factory=list)


class SkillRewriteResponse(BaseModel):
    """技能改写响应体。

    Attributes:
        draft_skill: 改写后的技能草稿。
        assistant_message: 模型生成的附带说明消息。
        changed_paths: 实际发生变化的字段路径列表。
        warnings: 处理过程中产生的警告列表。
        tool_suggestions: 工具建议列表。
    """

    draft_skill: SkillCard
    assistant_message: str
    changed_paths: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    tool_suggestions: list[ToolSuggestion] = Field(default_factory=list)


class SkillFileExtractRequest(BaseModel):
    """技能文件提取请求体——从上传的文件中提取文本内容。"""

    filename: str
    content_base64: str


class SkillFileExtractResponse(BaseModel):
    """技能文件提取响应体。

    Attributes:
        filename: 原始文件名。
        text: 提取出的纯文本内容。
    """

    filename: str
    text: str

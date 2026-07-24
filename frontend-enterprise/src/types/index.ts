/**
 * @file 领域类型定义模块（Domain Types）。
 *
 * 该文件定义了前端所使用的全部 TypeScript 领域模型类型，涵盖知识库、数字员工、
 * 技能、工具、模型配置、定时任务、聊天对话、反馈分析等核心业务实体。
 * 这些类型对应后端 API 的响应结构，是前后端数据契约的前端体现。
 *
 * 核心领域划分：
 * - **知识库（Knowledge）**：知识库 → 文档 → 桶（Bucket）→ 分块（Chunk）→ 概念（Concept）的层级结构。
 * - **数字员工（Agent）**：即 AI 员工，绑定技能、知识库、工具等资源，代表一个可交互的智能体。
 * - **技能（Skill）**：包括领域技能（SOP 卡片）和通用技能（Agent Skills / Markdown）。
 * - **工具（Tool）**：HTTP API 工具和 MCP（Model Context Protocol）工具服务器。
 * - **聊天（Chat）**：会话、消息、附件、引用、事件追踪等。
 * - **反馈（Feedback）**：对话反馈统计与分析。
 * - **定时任务（Scheduled Task）**：周期性自动执行的数字员工任务。
 */

// ---------------------------------------------------------------------------
// 技能卡片类型
// ---------------------------------------------------------------------------

/**
 * 技能卡片（SOP）数据结构。
 * 一个技能卡片定义了完整的对话执行流程，包含触发意图、执行目标、节点流转图等。
 */
export type SkillCard = {
  /** 技能唯一标识 */
  skill_id: string;
  /** 技能名称 */
  name: string;
  /** 技能版本号 */
  version: string;
  /** 业务领域（可选） */
  business_domain?: string;
  /** 技能描述 */
  description: string;
  /** 触发意图列表（用于意图路由匹配） */
  trigger_intents: string[];
  /** 用户话术示例（用于少样本学习或展示） */
  user_utterance_examples: string[];
  /** 执行目标列表 */
  goal: string[];
  /** 所需信息列表 */
  required_info: string[];
  /** 流程图节点列表 */
  nodes: Array<Record<string, unknown>>;
  /** 流程图边列表 */
  edges: Array<Record<string, unknown>>;
  /** 起始节点 ID */
  start_node_id: string;
  /** 终止节点 ID 列表 */
  terminal_node_ids: string[];
  /** 中断策略配置 */
  interruption_policy: Record<string, string>;
  /** 响应规则列表 */
  response_rules: string[];
};

/**
 * 知识库导入任务状态。
 * 表示一个文档导入/切分作业的异步执行进度。
 */
export type KnowledgeIngestJobRead = {
  /** 任务 ID */
  id: string;
  /** 租户 ID */
  tenant_id: string;
  /** 所属知识库 ID */
  knowledge_base_id: string;
  /** 关联文档 ID（可选） */
  document_id?: string;
  /** 文件名 */
  filename: string;
  /** 任务状态（如 pending/running/completed/failed） */
  status: string;
  /** 当前处理阶段 */
  stage: string;
  /** 进度百分比（0-100） */
  progress: number;
  /** 错误信息（失败时） */
  error?: string;
  /** 任务元数据 */
  metadata?: Record<string, unknown>;
  /** 创建时间 */
  created_at: string;
  /** 开始执行时间 */
  started_at?: string;
  /** 完成时间 */
  finished_at?: string;
  /** 最后更新时间 */
  updated_at: string;
};

/**
 * 知识库信息。
 * 知识库是文档和知识的顶层容器，支持版本管理和分支同步。
 */
export type KnowledgeBaseRead = {
  id: string;
  tenant_id: string;
  /** 知识库名称 */
  name: string;
  /** 描述 */
  description?: string;
  /** 状态 */
  status: string;
  /** 当前版本号 */
  version?: string;
  /** 分支同步状态 */
  branch_sync_state?: string;
  /** 分支基准版本 */
  branch_base_version?: string;
  /** 分支头部版本 */
  branch_head_version?: string;
  metadata?: Record<string, unknown>;
  /** 文档数量 */
  document_count: number;
  /** 桶（Bucket）数量 */
  bucket_count: number;
  /** 分块（Chunk）数量 */
  chunk_count: number;
  created_at: string;
  updated_at: string;
};

/**
 * 知识库文档信息。
 * 每个文档对应一个上传的文件，被切分为多个桶和分块。
 */
export type KnowledgeDocumentRead = {
  id: string;
  tenant_id: string;
  knowledge_base_id: string;
  knowledge_base_version_id?: string;
  /** 文件名 */
  filename: string;
  /** 文件类型（如 pdf、md、txt） */
  file_type: string;
  /** 文档标题 */
  title?: string;
  /** 处理状态 */
  status: string;
  /** 桶数量 */
  bucket_count: number;
  /** 分块数量 */
  chunk_count: number;
  metadata?: Record<string, unknown>;
  /** 错误信息 */
  error?: string;
  created_at: string;
  updated_at: string;
};

/**
 * 知识库桶（Bucket）信息。
 * 桶是文档的逻辑分组单元，包含标题、摘要和 token 估算值。
 */
export type KnowledgeBucketRead = {
  id: string;
  tenant_id: string;
  knowledge_base_id: string;
  /** 所属文档 ID */
  document_id: string;
  /** 桶键（唯一标识） */
  bucket_key: string;
  /** 桶标题 */
  title: string;
  /** 桶摘要 */
  summary: string;
  /** Token 数估算 */
  token_estimate: number;
  /** 分块数量 */
  chunk_count: number;
  status: string;
  metadata?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

/**
 * 知识库分块（Chunk）信息。
 * 分块是最小的检索单元，包含实际内容和来源引用。
 */
export type KnowledgeChunkRead = {
  id: string;
  tenant_id: string;
  knowledge_base_id: string;
  document_id: string;
  /** 所属桶 ID */
  bucket_id: string;
  /** 分块序号（在桶内的位置） */
  chunk_index: number;
  /** 分块实际内容文本 */
  content: string;
  /** 分块摘要 */
  summary?: string;
  /** 来源引用（如页码、行号） */
  source_ref?: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

/**
 * 知识发现建议。
 * 系统从知识库内容中自动提取的技能、工具或警告建议。
 */
export type KnowledgeDiscoveryRead = {
  id: string;
  tenant_id: string;
  knowledge_base_id: string;
  document_id: string;
  bucket_id?: string;
  /** 建议类型：技能、工具、警告 */
  suggestion_type: 'skill' | 'tool' | 'warning';
  title: string;
  status: string;
  /** 建议详情 */
  payload: Record<string, unknown>;
  /** 来源引用列表 */
  source_refs: Array<Record<string, unknown>>;
  /** 生成原因 */
  reason?: string;
  created_at: string;
  updated_at: string;
};

/**
 * 知识概念（Concept）信息。
 * 概念是从知识库中提取的结构化知识节点，以 Markdown 格式存储。
 */
export type KnowledgeConceptRead = {
  id: string;
  tenant_id: string;
  knowledge_base_id: string;
  knowledge_base_version_id?: string;
  document_id?: string;
  /** 概念唯一标识 */
  concept_id: string;
  /** 概念类型 */
  concept_type: string;
  title: string;
  description?: string;
  /** 概念正文（Markdown） */
  content_md: string;
  /** 前置信息（frontmatter） */
  frontmatter: Record<string, unknown>;
  /** 关联链接 */
  links: Array<Record<string, unknown>>;
  /** 引用列表 */
  citations: Array<Record<string, unknown>>;
  /** 来源引用 */
  source_refs: Array<Record<string, unknown>>;
  status: string;
  created_at: string;
  updated_at: string;
};

/**
 * 知识搜索证据项。
 * 表示搜索结果中的一条证据，包含来源和摘录。
 */
export type KnowledgeSearchEvidence = {
  chunk_id: string;
  document_id: string;
  bucket_id: string;
  source_path?: string;
  section_path?: string;
  summary?: string;
  /** 证据摘录文本 */
  excerpt: string;
  /** 置信度原因说明 */
  confidence_reason?: string;
};

/**
 * 知识搜索响应。
 * 包含检索到的桶、分块、路由追踪、证据包等完整检索信息。
 */
export type KnowledgeSearchResponse = {
  /** 命中的桶列表 */
  selected_buckets: KnowledgeBucketRead[];
  /** 命中的分块列表 */
  chunks: KnowledgeChunkRead[];
  /** 检索追踪信息 */
  trace: Array<Record<string, unknown>>;
  /** 路由追踪信息 */
  route_trace: Array<Record<string, unknown>>;
  /** 命中的文档列表 */
  selected_documents: Array<Record<string, unknown>>;
  /** 展开的章节列表 */
  expanded_sections: Array<Record<string, unknown>>;
  /** 命中的概念列表 */
  selected_concepts: Array<Record<string, unknown>>;
  /** OKF 引用列表 */
  okf_citations: Array<Record<string, unknown>>;
  /** 证据包列表 */
  evidence_pack: KnowledgeSearchEvidence[];
};

// ---------------------------------------------------------------------------
// 数字员工（Agent）类型
// ---------------------------------------------------------------------------

/** 数字员工可绑定的资源类型 */
export type AgentResourceType = 'skill' | 'general_skill' | 'knowledge_base' | 'tool';

/**
 * 数字员工资源绑定关系。
 * 表示某个数字员工关联了哪个技能/知识库/工具。
 */
export type AgentResourceBindingRead = {
  id: string;
  tenant_id: string;
  /** 所属数字员工 ID */
  agent_id: string;
  /** 资源类型 */
  resource_type: AgentResourceType;
  /** 资源 ID */
  resource_id: string;
  /** 绑定状态 */
  status: 'active' | 'inactive' | string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

/**
 * 数字员工（Agent）完整档案。
 * 包含基本信息、人格设定、资源绑定列表等。
 * `is_overall` 为 true 时表示"开放广场"（公共默认员工）。
 */
export type AgentProfileRead = {
  id: string;
  tenant_id: string;
  name: string;
  description?: string;
  /** 人格提示词 */
  persona_prompt?: string;
  /** 是否为开放广场（公共默认员工） */
  is_overall: boolean;
  /** 状态 */
  status: 'active' | 'archived' | string;
  /** 元数据（含角色、头像、所有者等信息） */
  metadata: Record<string, unknown>;
  /** 绑定的资源列表 */
  resources: AgentResourceBindingRead[];
  created_at: string;
  updated_at: string;
};

// ---------------------------------------------------------------------------
// 工具（Tool）类型
// ---------------------------------------------------------------------------

/**
 * 工具建议（从知识库发现中提取的候选工具）。
 */
export type ToolSuggestion = {
  name: string;
  display_name?: string;
  description?: string;
  bucket: string;
  tool_type?: 'http' | 'mcp' | string;
  method: string;
  url: string;
  mcp_config?: Record<string, unknown>;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  sample_arguments?: Record<string, unknown>;
  source_excerpt?: string;
  /** 探测结果（实际调用测试） */
  probe_result?: ToolProbeResponse;
  reason: string;
  /** 解析状态：已存在、新候选、不完整 */
  resolution_status?: 'existing' | 'new_candidate' | 'incomplete';
  matched_tool_id?: string;
  matched_tool_name?: string;
  matched_tool_display_name?: string;
  missing_reason?: string;
};

/**
 * 工具探测（探针）响应。
 * 对候选工具发起一次真实调用以验证可用性和推断输出 Schema。
 */
export type ToolProbeResponse = {
  success: boolean;
  status_code?: number;
  data_preview?: unknown;
  /** 推断出的输出 Schema */
  inferred_output_schema: Record<string, unknown>;
  error?: {
    code: string;
    message: string;
  };
};

/**
 * 领域技能（SOP）完整信息。
 * 包含技能卡片内容和调用/反馈统计数据。
 */
export type SkillRead = {
  id: string;
  tenant_id: string;
  skill_id: string;
  name: string;
  version: string;
  business_domain?: string;
  description?: string;
  /** 技能卡片内容 */
  content: SkillCard;
  /** 状态：草稿、已发布、已归档 */
  status: 'draft' | 'published' | 'archived';
  call_count: number;
  positive_feedback_count: number;
  negative_feedback_count: number;
  positive_rate: number;
  negative_rate: number;
  total_call_count: number;
  total_positive_feedback_count: number;
  total_negative_feedback_count: number;
  total_positive_rate: number;
  total_negative_rate: number;
  /** 近期版本列表 */
  recent_versions: string[];
  /** 近期调用统计 */
  recent_call_count: number;
  recent_positive_feedback_count: number;
  recent_negative_feedback_count: number;
  recent_positive_rate: number;
  recent_negative_rate: number;
  /** 关联的数字员工 ID */
  agent_id?: string;
  branch_status?: string;
  branch_sync_state?: string;
  branch_base_version?: string;
  branch_head_version?: string;
  metadata?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

/** 技能历史版本记录 */
export type SkillVersionRead = SkillRead & {
  created_at: string;
};

/**
 * 通用技能（Agent Skill）信息。
 * 以 Markdown 格式定义的轻量技能，可包含多个文件。
 */
export type GeneralSkillRead = {
  id: string;
  tenant_id: string;
  /** URL 友好的标识符 */
  slug: string;
  name: string;
  description?: string;
  /** 主页地址 */
  homepage?: string;
  /** 技能 Markdown 正文 */
  skill_markdown: string;
  /** 技能附带文件列表 */
  skill_files: Array<{
    path: string;
    content: string;
    size?: number;
    mime_type?: string;
  }>;
  metadata: Record<string, unknown>;
  status: 'draft' | 'published' | 'archived';
  permissions: Record<string, unknown>;
  runtime_config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

/**
 * 通用技能运行响应。
 * 包含执行追踪、生成的代码、标准输出/错误和结构化结果。
 */
export type GeneralSkillRunResponse = {
  skill_slug: string;
  execution_trace: Array<Record<string, unknown>>;
  generated_code: string;
  stdout: string;
  stderr: string;
  structured_result: Record<string, unknown>;
  /** 最终回复文本 */
  reply: string;
};

/**
 * 模型配置信息。
 * 定义 LLM 的提供商、API 协议、参数等，供数字员工调用模型时使用。
 */
export type ModelConfigRead = {
  id: string;
  tenant_id: string;
  name: string;
  /** 模型提供商 */
  provider: string;
  /** API 协议类型 */
  api_protocol: 'openai_chat_completions' | 'anthropic_messages' | 'gemini_generate_content';
  base_url?: string;
  /** 脱敏后的 API Key（仅展示用） */
  api_key_masked: string;
  /** 模型名称 */
  model: string;
  temperature: number;
  max_output_tokens: number;
  /** 额外请求体参数 */
  extra_body: Record<string, unknown>;
  /** 协议选项 */
  protocol_options: Record<string, unknown>;
  /** 遗留未映射的选项 */
  legacy_unmapped_options: Record<string, unknown>;
  /** 信任状态 */
  trust_status: 'legacy_trusted' | 'unverified' | 'verified';
  /** 验证尝试状态 */
  verification_attempt_status: 'idle' | 'verifying' | 'succeeded' | 'failed';
  config_revision: number;
  security_revision: number;
  /** 是否为默认模型 */
  is_default: boolean;
  /** 是否启用 */
  enabled: boolean;
  updated_at: string;
};

/**
 * 人格配置（System Prompt）。
 */
export type PersonaRead = {
  tenant_id: string;
  system_prompt: string;
  updated_at: string;
};

/**
 * UI 配置。
 * 控制前端展示哪些调试追踪信息（思考链、技能追踪、工具追踪）及相关参数。
 */
export type UIConfigRead = {
  tenant_id: string;
  /** 是否展示思考追踪 */
  show_thinking_trace: boolean;
  /** 是否展示技能追踪 */
  show_skill_trace: boolean;
  /** 是否展示工具追踪 */
  show_tool_trace: boolean;
  /** 反思最大轮数 */
  reflection_max_rounds: number;
  /** Agent 循环最大动作数 */
  agent_loop_max_actions: number;
  updated_at: string;
};

/**
 * 员工记忆信息。
 * 记录数字员工在交互过程中积累的长期记忆。
 */
export type MemoryRead = {
  id: string;
  tenant_id: string;
  user_id: string;
  username?: string;
  session_id?: string;
  /** 记忆类型 */
  kind: string;
  /** 记忆内容 */
  content: string;
  /** 重要性权重 */
  importance: number;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

/**
 * 工具（HTTP / MCP）完整信息。
 */
export type ToolRead = {
  id: string;
  tenant_id: string;
  name: string;
  display_name?: string;
  description?: string;
  bucket: string;
  /** 工具类型：HTTP API 或 MCP */
  tool_type: 'http' | 'mcp' | string;
  /** HTTP 方法（GET/POST 等） */
  method: string;
  url: string;
  /** 请求头配置 */
  headers: Record<string, unknown>;
  /** 认证配置 */
  auth: Record<string, unknown>;
  /** MCP 配置 */
  mcp_config: Record<string, unknown>;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  /** 允许调用此工具的技能列表 */
  allowed_skills: string[];
  /** 关联的 MCP 服务器 ID */
  mcp_server_id?: string | null;
  enabled: boolean;
  metadata?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

// ---------------------------------------------------------------------------
// MCP 服务器类型
// ---------------------------------------------------------------------------

/** MCP 传输协议类型 */
export type MCPTransport = 'stdio' | 'streamable_http' | 'sse' | 'builtin';

/**
 * MCP 服务器连接配置。
 */
export type MCPServerConnection = {
  transport: MCPTransport;
  url?: string | null;
  headers: Record<string, string>;
  /** 本地启动命令（stdio 传输时使用） */
  command?: string | null;
  args: string[];
  env: Record<string, string>;
  cwd?: string | null;
};

/**
 * MCP 服务器信息。
 */
export type MCPServerRead = {
  id: string;
  tenant_id: string;
  name: string;
  display_name?: string;
  description?: string;
  bucket: string;
  /** 连接配置 */
  connection: MCPServerConnection;
  enabled: boolean;
  /** 最后同步时间 */
  last_synced_at?: string | null;
  /** 已发现的工具数量 */
  tool_count: number;
  created_at: string;
  updated_at: string;
};

/**
 * MCP 已发现的工具。
 */
export type MCPDiscoveredTool = {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  /** 是否已导入 */
  imported: boolean;
  tool_id?: string | null;
  enabled?: boolean | null;
};

/**
 * MCP 工具发现响应。
 */
export type MCPDiscoverResponse = {
  success: boolean;
  tools: MCPDiscoveredTool[];
  error?: { code: string; message: string } | null;
};

/**
 * MCP 同步响应。
 * 包含导入、更新、移除的工具名称列表。
 */
export type MCPSyncResponse = {
  success: boolean;
  imported: string[];
  updated: string[];
  removed: string[];
  error?: { code: string; message: string } | null;
};

// ---------------------------------------------------------------------------
// 定时任务类型
// ---------------------------------------------------------------------------

/**
 * 定时任务信息。
 * 定义数字员工的周期性自动执行计划（一次性、每日、每周、每月）。
 */
export type ScheduledTaskRead = {
  id: string;
  tenant_id: string;
  /** 执行此任务的数字员工 ID */
  agent_id: string;
  /** 创建者用户 ID */
  created_by_user_id: string;
  title: string;
  /** 任务提示词（自动执行时发送给数字员工的内容） */
  prompt: string;
  description?: string;
  /** 调度类型 */
  schedule_type: 'once' | 'daily' | 'weekly' | 'monthly' | string;
  /** 调度配置详情 */
  schedule: Record<string, unknown>;
  /** 时区 */
  timezone: string;
  /** RRULE 规则字符串（RFC 5545） */
  rrule?: string;
  /** 状态 */
  status: 'active' | 'paused' | 'completed' | 'archived' | string;
  /** 并发策略 */
  concurrency_policy: string;
  /** 误触策略（misfire policy） */
  misfire_policy: string;
  /** 最大执行次数 */
  max_runs?: number;
  /** 结束时间 */
  end_at?: string;
  /** 下次执行时间 */
  next_run_at?: string;
  /** 上次执行时间 */
  last_run_at?: string;
  /** 上次执行状态 */
  last_status?: string;
  /** 已执行次数 */
  run_count: number;
  /** 来源会话 ID（从对话中创建时记录） */
  source_session_id?: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

/**
 * 定时任务执行记录。
 */
export type ScheduledTaskRunRead = {
  id: string;
  tenant_id: string;
  scheduled_task_id: string;
  task_title?: string;
  task_status?: string;
  agent_id: string;
  user_id: string;
  session_id?: string;
  /** 计划执行时间 */
  scheduled_for: string;
  /** 执行状态 */
  status: string;
  started_at?: string;
  finished_at?: string;
  /** 执行结果摘要 */
  result_summary?: string;
  error?: string;
  /** 执行追踪信息 */
  trace: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

/**
 * 聊天对话轮次响应（非流式）。
 */
export type ChatTurnResponse = {
  /** AI 回复内容 */
  reply: string;
  session_id: string;
  /** 路由决策信息 */
  router_decision?: Record<string, unknown>;
  /** 步骤结果 */
  step_result?: Record<string, unknown>;
  tool_result?: Record<string, unknown>;
  /** 会话状态 */
  session_state: Record<string, unknown>;
};

// ---------------------------------------------------------------------------
// 聊天会话类型
// ---------------------------------------------------------------------------

/**
 * 聊天会话信息。
 */
export type ChatSession = {
  id: string;
  tenant_id: string;
  user_id?: string;
  /** 关联的数字员工 ID */
  agent_id?: string;
  title?: string;
  /** 当前激活的技能 ID */
  active_skill_id?: string;
  /** 当前激活的步骤 ID */
  active_step_id?: string;
  status: string;
  /** 会话摘要 */
  summary?: string;
  /** 最后一个待用户回答的问题 */
  last_agent_question?: string;
  /** 是否为定时任务会话 */
  is_scheduled?: boolean;
  updated_at: string;
};

/** 聊天附件类型分类 */
export type ChatAttachmentKind = 'text' | 'pdf' | 'image' | 'binary';

/**
 * 聊天附件信息。
 * 用户在对话中上传的文件，经后端解析后返回的结构化数据。
 */
export type ChatAttachmentRead = {
  id: string;
  filename: string;
  content_type: string;
  size: number;
  /** 附件类型分类 */
  kind: ChatAttachmentKind;
  /** 文本内容（文本类附件） */
  text?: string | null;
  /** 预览内容（图片类附件的缩略图） */
  preview?: string | null;
  /** 数据 URL（可直接用于 img src 等） */
  data_url?: string | null;
  /** Python 处理摘要 */
  python_summary?: string | null;
  error?: string | null;
};

/**
 * 知识引用信息。
 * 表示 AI 回复中引用的知识库来源，用于展示引用溯源。
 */
export type KnowledgeCitation = {
  id: string;
  label?: string;
  /** 引用类型 */
  kind?: 'evidence' | 'concept' | 'okf' | string;
  title?: string;
  source_path?: string;
  section_path?: string;
  content?: string;
  excerpt?: string;
  summary?: string;
  confidence_reason?: string;
  document_id?: string;
  bucket_id?: string;
  chunk_id?: string;
  concept_id?: string;
  concept_type?: string;
};

/**
 * 聊天消息。
 * 包含标准消息字段和前端特有的展示状态字段。
 */
export type ChatMessage = {
  id: string;
  /** 角色：用户、助手、系统、工具 */
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  metadata?: {
    /** 附件列表 */
    attachments?: ChatAttachmentRead[];
    /** 知识引用列表 */
    knowledge_citations?: KnowledgeCitation[];
    /** 知识检索查询信息 */
    knowledge_query?: Record<string, unknown>;
    [key: string]: unknown;
  };
  created_at: string;
  /** 反馈评分：赞 / 踩 */
  feedback_rating?: 'up' | 'down' | null;
  /** 所属对话轮次 ID */
  turn_id?: string | null;
  /** 对话轮次 ID（前端使用） */
  turnId?: string;
  /** 服务端消息 ID */
  serverMessageId?: string;
  /** 是否正在流式输出中 */
  isStreaming?: boolean;
  /** 是否为错误消息 */
  isError?: boolean;
};

/**
 * 聊天会话事件记录。
 * 记录会话过程中发生的各类事件（如技能触发、工具调用、思考步骤等）。
 */
export type ChatSessionEventRead = {
  id: string;
  created_at: string;
  run_id?: string;
  /** 事件序号 */
  seq?: number;
  /** 事件类型 */
  event: string;
  /** 事件数据 */
  data: Record<string, unknown>;
};

/**
 * 人工转接（Human Handoff）记录。
 * 当数字员工无法处理时，转接给人工处理的请求。
 */
export type HumanHandoffRead = {
  id: string;
  tenant_id: string;
  session_id: string;
  agent_id?: string | null;
  /** 请求者用户 ID */
  requester_user_id?: string | null;
  /** 被指派的客服用户 ID */
  assignee_user_id?: string | null;
  trigger_skill_id?: string | null;
  trigger_step_id?: string | null;
  /** 上下文摘要 */
  context_summary?: string | null;
  /** 待回答的问题 */
  pending_question?: string | null;
  status: string;
  /** 人工回复内容 */
  human_reply?: string | null;
  /** 恢复负载（人工处理后恢复会话所需的数据） */
  resume_payload?: Record<string, unknown> | null;
  metadata?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  /** 回答时间 */
  answered_at?: string | null;
};

/**
 * 定时任务草稿（从对话中自动识别提取）。
 */
export type ScheduledTaskDraftRead = {
  /** 是否应该创建此任务 */
  should_create: boolean;
  tenant_id: string;
  agent_id: string;
  title: string;
  prompt: string;
  description?: string;
  schedule_type: 'once' | 'daily' | 'weekly' | 'monthly' | string;
  schedule: Record<string, unknown>;
  timezone: string;
  rrule?: string;
  /** 置信度 */
  confidence: number;
  reason?: string;
  source_session_id?: string;
};

/**
 * 企业版聊天会话信息。
 */
export type EnterpriseChatSessionRead = {
  id: string;
  tenant_id: string;
  user_id?: string;
  agent_id?: string;
  title?: string;
  active_skill_id?: string;
  active_step_id?: string;
  status: string;
  summary?: string;
  last_agent_question?: string;
  created_at: string;
  updated_at: string;
};

/**
 * 企业版会话详情（含消息和事件列表）。
 */
export type EnterpriseSessionDetailRead = {
  session: EnterpriseChatSessionRead;
  messages: FeedbackMessageRead[];
  events: Array<{
    id: string;
    /** 事件类型 */
    event_type: string;
    payload: Record<string, unknown>;
    created_at: string;
  }>;
};

/**
 * 数字员工工作记录事件。
 */
export type AgentWorkRecordEventRead = {
  id: string;
  /** 事件类型 */
  kind: 'chat' | 'task' | 'sop' | 'tool' | 'knowledge' | 'skill';
  /** 事件阶段 */
  phase: 'reply' | 'last_run' | 'next_run' | 'assigned';
  timestamp: string;
  label: string;
};

/**
 * 数字员工工作记录。
 * 汇总某个员工在一段时间内的回复统计和事件列表。
 */
export type AgentWorkRecordRead = {
  agent_id: string;
  timezone: string;
  generated_at: string;
  /** 回复统计 */
  reply_stats: {
    total: number;
    today: number;
    /** 按天分组的回复数 */
    by_day: Record<string, number>;
  };
  events: AgentWorkRecordEventRead[];
};

/**
 * 追踪行（Trace Line）。
 * 表示执行过程中的一步追踪记录（思考、决策、技能、工具、代码、知识）。
 */
export type TraceLineRead = {
  id: string;
  /** 追踪类型 */
  kind: 'thinking' | 'decision' | 'skill' | 'tool' | 'code' | 'knowledge';
  text: string;
  detail?: string | null;
  code?: string | null;
  /** 代码语言 */
  language?: string | null;
  /** 输出内容 */
  output?: string | null;
  outputLanguage?: string | null;
  outputTitle?: string | null;
  /** 执行状态 */
  state: 'running' | 'completed' | 'failed';
  /** 是否可折叠 */
  collapsible?: boolean | null;
};

/**
 * 对话轮次追踪信息。
 */
export type TurnTraceRead = {
  turn_id: string;
  user_message_id?: string | null;
  started_at: string;
  completed_at?: string | null;
  /** 追踪行列表 */
  lines: TraceLineRead[];
};

/**
 * 追踪摘要信息。
 */
export type TraceSummary = {
  session_id: string;
  user_id?: string;
  active_skill_id?: string;
  active_step_id?: string;
  last_decision?: Record<string, unknown>;
  last_message?: string;
  last_message_time?: string;
  tool_call_count: number;
  status: string;
  updated_at: string;
};

// ---------------------------------------------------------------------------
// 反馈分析类型
// ---------------------------------------------------------------------------

/**
 * 反馈会话摘要。
 * 每行对应一个会话的反馈汇总信息。
 */
export type FeedbackSessionRead = {
  session_id: string;
  tenant_id: string;
  agent_id?: string;
  user_id?: string;
  username?: string;
  display_name?: string;
  title?: string;
  summary?: string;
  status: string;
  /** 反馈总数 */
  feedback_count: number;
  latest_feedback_at: string;
  latest_message_id: string;
  latest_message: string;
  /** 分析状态 */
  analysis_status?: string;
  /** 分析分类 */
  analysis_bucket?: string;
  analysis_bucket_label?: string;
  analysis_summary?: string;
  /** 主要分类 */
  primary_bucket?: string;
  primary_bucket_label?: string;
  /** 各分类计数 */
  bucket_counts?: Record<string, number>;
  updated_at: string;
};

/**
 * 单条反馈的分析结果。
 */
export type FeedbackAnalysisRead = {
  status?: string;
  bucket?: string;
  bucket_label?: string;
  reason?: string;
  summary?: string;
  confidence?: number;
  metadata?: Record<string, unknown>;
  analyzed_at?: string | null;
};

/**
 * 反馈消息（带反馈信息的聊天消息）。
 */
export type FeedbackMessageRead = {
  id: string;
  tenant_id: string;
  session_id: string;
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  created_at: string;
  /** 反馈 ID */
  feedback_id?: string;
  /** 反馈评分 */
  feedback_rating?: 'up' | 'down' | null;
  feedback_updated_at?: string;
  /** 反馈分析结果 */
  feedback_analysis?: FeedbackAnalysisRead;
};

/**
 * 反馈会话详情（含消息列表和反馈列表）。
 */
export type FeedbackSessionDetailRead = {
  session: Record<string, unknown>;
  messages: FeedbackMessageRead[];
  feedback: Array<Record<string, unknown>>;
};

/**
 * 反馈统计摘要。
 */
export type FeedbackSummaryRead = {
  total_feedback: number;
  /** 负反馈数 */
  down_count: number;
  /** 正反馈数 */
  up_count: number;
  /** 各分类计数 */
  bucket_counts: Array<{ bucket: string; label: string; count: number }>;
  /** 各状态计数 */
  status_counts: Record<string, number>;
  summary: string;
  /** 热门摘要列表 */
  top_summaries: Array<Record<string, unknown>>;
};

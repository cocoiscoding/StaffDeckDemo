/**
 * @file chatTypes.ts
 * @module pages/chat/chatTypes
 * @description
 * 聊天模块的核心类型定义文件。
 *
 * 本文件定义了聊天会话状态管理中使用的所有共享类型，包括：
 * - 会话消息的双槽位结构（SessionSlot）：区分服务端持久化消息与实时流式消息
 * - 流式响应的运行时状态（StreamSlot）：管理加载状态、流式累积文本、中止控制器等
 * - 思维链追踪相关类型（TraceSkill / TraceTool / TraceLine / TurnTrace）：
 *   用于渲染 AI 推理过程中的"执行记录"面板
 * - 输入框附件类型（ComposerAttachment）：扩展了上传状态等 UI 字段
 * - 交互模式与定时任务调度类型
 *
 * 此外还提供了三个工厂函数用于创建对应类型的空初始值。
 */

import type { ChatAttachmentRead, ChatMessage } from '@/types';

/**
 * 会话消息双槽位结构。
 *
 * 将一个会话的消息分为两个来源：
 * - serverMessages：从服务端 API 加载的持久化消息
 * - realtimeMessages：客户端本地创建的实时消息（如流式回复、本地中断提示）
 * 两者在渲染时合并显示。
 */
export type SessionSlot = {
  /** 服务端持久化消息列表 */
  serverMessages: ChatMessage[];
  /** 客户端实时创建的消息列表（流式输出、系统提示等） */
  realtimeMessages: ChatMessage[];
};

/**
 * 流式响应运行时状态。
 *
 * 在一次 AI 回复的流式传输过程中，此对象跟踪所有与该流相关的运行时状态，
 * 包括加载标志、阶段标识、计时器、已累积文本、中止控制器以及中继恢复信息。
 */
export type StreamSlot = {
  /** 是否正在等待/接收流式响应 */
  loading: boolean;
  /** 当前流式阶段描述文本（如"正在思考…"） */
  phase: string;
  /** 流式响应超时计时器 ID */
  timer: number | null;
  /** 流式传输中累积的文本内容 */
  accumulated: string;
  /** 当前流式轮次的唯一标识 */
  turnId: string | null;
  /** 被用户主动取消的轮次 ID */
  cancelledTurnId: string | null;
  /** 用于中止 fetch 请求的 AbortController */
  abortController: AbortController | null;
  /** 中继恢复（relay recovery）开始的时间戳 */
  relayRecoveryStartedAt: number | null;
  /** 中继恢复对应的轮次 ID */
  relayRecoveryTurnId: string | null;
};

/**
 * 思维链中的技能追踪信息。
 * 对应 AI 在推理过程中调用的某个技能（Skill）。
 */
export type TraceSkill = {
  /** 技能唯一标识 */
  skillId: string;
  /** 技能显示名称 */
  name?: string;
  /** 技能内步骤标识 */
  stepId?: string;
  /** 技能执行状态 */
  state?: string;
};

/**
 * 思维链中的工具调用追踪信息。
 * 对应 AI 在推理过程中调用的某个外部工具。
 */
export type TraceTool = {
  /** 工具唯一标识 */
  toolId: string;
  /** 工具调用唯一标识（用于关联请求与响应） */
  toolCallId?: string;
  /** 工具显示名称 */
  toolName: string;
  /** 工具原始名称（未经本地化处理） */
  rawToolName?: string;
  /** 工具调用是否成功 */
  success?: boolean;
  /** 工具调用是否出错 */
  isError?: boolean;
  /** 工具返回的内容（任意类型） */
  content?: unknown;
};

/**
 * 思维链图标名称联合类型。
 * 每个值对应执行记录面板中不同类型步骤的图标。
 */
export type CotTraceIconName = 'advance' | 'execute' | 'generated' | 'judge' | 'loading' | 'select' | 'tool';

/**
 * 执行记录中的单行追踪信息。
 *
 * 表示思维链面板中的一行展示内容，可以是思考过程、决策、技能调用、
 * 工具调用、代码执行或知识检索等不同类型。
 */
export type TraceLine = {
  /** 唯一标识 */
  id: string;
  /** 行类型：思考、决策、技能、工具、代码、知识 */
  kind: 'thinking' | 'decision' | 'skill' | 'tool' | 'code' | 'knowledge';
  /** 主要显示文本 */
  text: string;
  /** 附加详情说明 */
  detail?: string;
  /** 关联的代码片段（用于代码类型） */
  code?: string;
  /** 代码语言（如 python、javascript） */
  language?: string;
  /** 代码或工具的输出结果 */
  output?: string;
  /** 输出内容的语言标识 */
  outputLanguage?: string;
  /** 输出结果的标题 */
  outputTitle?: string;
  /** 执行状态：运行中 / 已完成 / 已失败 */
  state: 'running' | 'completed' | 'failed';
  /** 是否可折叠 */
  collapsible?: boolean;
  /** 指定使用的图标名称 */
  icon?: CotTraceIconName;
  /** 是否为占位行（临时插入，后续可能被替换） */
  placeholder?: boolean;
  /** 是否为临时行（尚未确认最终内容） */
  provisional?: boolean;
};

/**
 * 单个轮次的思维链追踪数据。
 * 包含该轮次的所有追踪行及起止时间。
 */
export type TurnTrace = {
  /** 该轮次的所有追踪行 */
  lines: TraceLine[];
  /** 轮次开始时间戳 */
  startedAt: number;
  /** 轮次完成时间戳（未完成时为 undefined） */
  completedAt?: number;
};

/**
 * 输入框附件类型。
 * 在服务端返回的附件基础上增加了上传状态和上传键等 UI 字段。
 */
export type ComposerAttachment = ChatAttachmentRead & {
  /** 上传状态：上传中 / 就绪 / 错误 */
  uploadStatus: 'uploading' | 'ready' | 'error';
  /** 上传操作的唯一键（用于在 UI 中标识和管理附件） */
  uploadKey: string;
};

/**
 * 编辑器交互模式。
 * - normal：普通即时对话
 * - scheduled_task：定时任务模式
 */
export type ComposerInteractionMode = 'normal' | 'scheduled_task';

/**
 * 定时任务的调度类型。
 * - once：一次性执行
 * - daily：每天执行
 * - weekly：每周执行
 * - monthly：每月执行
 */
export type DraftScheduleType = 'once' | 'daily' | 'weekly' | 'monthly';

/**
 * 创建一个空的会话消息槽位。
 *
 * @returns 初始化的 SessionSlot，两个消息数组均为空
 */
export function createEmptySlot(): SessionSlot {
  return { serverMessages: [], realtimeMessages: [] };
}

/**
 * 创建一个初始的流式响应状态槽位。
 *
 * @returns 初始化的 StreamSlot，所有状态字段均为默认/空值
 */
export function createStreamSlot(): StreamSlot {
  return {
    loading: false,
    phase: '',
    timer: null,
    accumulated: '',
    turnId: null,
    cancelledTurnId: null,
    abortController: null,
    relayRecoveryStartedAt: null,
    relayRecoveryTurnId: null,
  };
}

/**
 * 创建一个新的轮次思维链追踪对象。
 *
 * @returns 初始化的 TurnTrace，包含空行数组和当前时间戳
 */
export function createTurnTrace(): TurnTrace {
  return { lines: [], startedAt: Date.now() };
}

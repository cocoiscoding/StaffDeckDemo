/**
 * @file chatQueueStorage.ts
 * @module pages/chat/chatQueueStorage
 * @description
 * 离线消息队列的本地持久化存储模块。
 *
 * 当用户在 AI 正在回复的过程中再次发送消息时，该消息会被加入发送队列，
 * 等待当前流式回复完成后再依次发送。此模块负责将队列持久化到
 * localStorage 中，确保在页面刷新或浏览器重启后队列不会丢失。
 *
 * 核心概念：
 * - PreparedChatTurn：一条已准备好发送的聊天轮次（含文本、附件、交互模式等）
 * - 存储键格式：skill_agent_chat_queue:{tenantId}:{userId}
 * - 所有读写操作均包含数据验证和去重逻辑，防止脏数据污染队列
 * - 存储访问失败（如浏览器隐私策略阻止）时以尽力而为（best-effort）方式降级
 */

import type { ChatAttachmentRead } from '@/types';

import type { ComposerInteractionMode } from './chatTypes';

/** localStorage 存储键前缀 */
const CHAT_QUEUE_STORAGE_PREFIX = 'skill_agent_chat_queue';
/** 合法的交互模式集合，用于数据校验 */
const INTERACTION_MODES = new Set<ComposerInteractionMode>(['normal', 'scheduled_task']);

/**
 * 存储接口的最小子集类型。
 * 只需要 getItem / setItem / removeItem 三个方法，
 * 便于在测试中注入 mock 实现。
 */
type ChatQueueStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

/**
 * 一条已准备好的待发送聊天轮次。
 *
 * 表示用户已经提交、但因 AI 正在回复而暂存在队列中的一条消息。
 * 包含发送该消息所需的全部上下文信息。
 */
export type PreparedChatTurn = {
  /** 队列内唯一标识 */
  queueId: string;
  /** 目标会话 ID */
  conversationId: string;
  /** 目标数字员工 ID */
  agentId: string;
  /** 轮次 ID */
  turnId: string;
  /** 用户输入的文本内容 */
  text: string;
  /** 附件列表 */
  attachments: ChatAttachmentRead[];
  /** 交互模式（普通 / 定时任务） */
  interactionMode: ComposerInteractionMode;
  /** 模型配置 ID（可选，用于指定特定模型） */
  modelConfigId?: string;
  /** 创建时间（ISO 字符串） */
  createdAt: string;
};

/**
 * 类型守卫：判断一个值是否为普通对象（非数组、非 null）。
 *
 * @param value - 待检查的值
 * @returns 如果是普通对象则返回 true
 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

/**
 * 类型守卫：判断一个值是否为合法的队列附件。
 * 检查 id、filename、content_type、size 字段的类型，以及 kind 是否为有效值。
 *
 * @param value - 待检查的值
 * @returns 如果符合 ChatAttachmentRead 结构则返回 true
 */
function isQueuedAttachment(value: unknown): value is ChatAttachmentRead {
  if (!isRecord(value)) return false;
  return (
    typeof value.id === 'string'
    && typeof value.filename === 'string'
    && typeof value.content_type === 'string'
    && typeof value.size === 'number'
    && ['text', 'pdf', 'image', 'binary'].includes(String(value.kind || ''))
  );
}

/**
 * 类型守卫：判断一个值是否为合法的 PreparedChatTurn。
 * 逐一检查所有必填字段的类型和格式，确保从 localStorage 反序列化的数据是安全的。
 *
 * @param value - 待检查的值
 * @returns 如果符合 PreparedChatTurn 结构则返回 true
 */
function isPreparedChatTurn(value: unknown): value is PreparedChatTurn {
  if (!isRecord(value)) return false;
  return (
    typeof value.queueId === 'string'
    && typeof value.conversationId === 'string'
    && typeof value.agentId === 'string'
    && typeof value.turnId === 'string'
    && typeof value.text === 'string'
    && Array.isArray(value.attachments)
    && value.attachments.every(isQueuedAttachment)
    && typeof value.interactionMode === 'string'
    && INTERACTION_MODES.has(value.interactionMode as ComposerInteractionMode)
    && (value.modelConfigId === undefined || typeof value.modelConfigId === 'string')
    && typeof value.createdAt === 'string'
    && Number.isFinite(Date.parse(value.createdAt))
  );
}

/**
 * 根据租户 ID 和用户 ID 生成 localStorage 存储键。
 *
 * @param tenantId - 租户 ID，为空时使用 'default'
 * @param userId - 用户 ID，为空时使用 'anonymous'
 * @returns 格式为 skill_agent_chat_queue:{tenantId}:{userId} 的存储键
 */
export function chatQueueStorageKey(tenantId: string, userId: string): string {
  return `${CHAT_QUEUE_STORAGE_PREFIX}:${tenantId || 'default'}:${userId || 'anonymous'}`;
}

/**
 * 从存储中读取已排队的聊天轮次列表。
 *
 * 执行流程：
 * 1. 从存储中读取原始 JSON 字符串
 * 2. 解析并验证每一条记录是否符合 PreparedChatTurn 结构
 * 3. 基于 queueId:turnId 组合键去重
 * 4. 如果解析后数据有变化（被过滤了无效项），则自动回写修正后的数据
 * 5. 任何异常情况下清除存储并返回空数组
 *
 * @param storage - 存储接口实例
 * @param key - 存储键
 * @returns 验证通过的聊天轮次数组（可能为空）
 */
export function readQueuedChatTurns(storage: ChatQueueStorage, key: string): PreparedChatTurn[] {
  try {
    const raw = storage.getItem(key);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) throw new Error('Invalid chat queue payload');

    // 使用 Set 进行 queueId:turnId 维度的去重
    const seen = new Set<string>();
    const turns = parsed.filter((value): value is PreparedChatTurn => {
      if (!isPreparedChatTurn(value)) return false; // 丢弃不符合结构的数据
      const identity = `${value.queueId}:${value.turnId}`;
      if (seen.has(identity)) return false; // 丢弃重复项
      seen.add(identity);
      return true;
    });
    // 如果过滤后数据有变化，回写修正后的干净数据
    if (turns.length !== parsed.length) {
      writeQueuedChatTurns(storage, key, turns);
    }
    return turns;
  } catch {
    // 解析失败或存储不可用时，清除脏数据并返回空数组
    try {
      storage.removeItem(key);
    } catch {
      // Storage access can be blocked by the browser privacy policy.
    }
    return [];
  }
}

/**
 * 将聊天轮次列表写入存储。
 *
 * 如果列表为空则删除存储键；否则将其序列化为 JSON 写入。
 * 写入失败时（如存储配额已满）尝试清除存储，以尽力而为方式降级。
 *
 * @param storage - 存储接口实例
 * @param key - 存储键
 * @param turns - 待写入的聊天轮次数组
 * @returns 写入成功返回 true，失败返回 false
 */
export function writeQueuedChatTurns(
  storage: ChatQueueStorage,
  key: string,
  turns: PreparedChatTurn[],
): boolean {
  try {
    if (turns.length === 0) {
      storage.removeItem(key); // 空列表直接清除存储
    } else {
      storage.setItem(key, JSON.stringify(turns));
    }
    return true;
  } catch {
    // 写入失败时尝试清除存储
    try {
      storage.removeItem(key);
    } catch {
      // Storage cleanup is best-effort when the browser quota is unavailable.
    }
    return false;
  }
}

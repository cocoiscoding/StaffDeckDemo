/**
 * @file chatHelpers.tsx
 * @module pages/chat/chatHelpers
 * @description
 * 聊天模块的核心工具函数库（约 1850 行）。
 *
 * 本文件是聊天模块中最复杂的工具集，涵盖以下职责：
 *
 * 1. **存储与会话状态**：会话已读时间、模型选择、草稿会话键等 localStorage 管理
 * 2. **Markdown 渲染引擎**：自研轻量级 Markdown 解析器，支持标题、列表、代码块、
 *    表格、引用、链接、图片、粗体、行内代码等语法，无第三方依赖
 * 3. **消息合并与轮次（Turn）匹配**：将服务端消息与实时消息合并去重，
 *    通过并查集（Union-Find）算法构建轮次别名映射，确保用户消息与 AI 回复正确关联
 * 4. **思维链（Chain-of-Thought / Trace）解析**：将后端流式事件解析为结构化的
 *    执行追踪行（TraceLine），支持技能、工具、代码、知识检索等多种类型
 * 5. **知识引用提取**：从消息元数据中提取知识库引用，匹配正文中的 [n] 标记
 * 6. **定时任务草案**：解析和格式化定时任务的调度计划
 * 7. **剪贴板图片处理**：从粘贴内容中提取图片文件（支持 data URL、远程 URL、blob）
 * 8. **附件管理**：附件类型标签、大小格式化等辅助功能
 *
 * 设计原则：
 * - 纯函数为主，不持有可变状态，便于测试和复用
 * - 所有从外部数据源解析的函数都包含类型守卫和防御性检查
 * - Markdown 解析为自研实现，避免引入大型第三方库
 */

import type { ReactNode } from 'react';

import CodeBlock from '@/components/CodeBlock';
import { ApiError } from '@/api/client';
import type { StreamEvent } from '@/api/client';
import { formatClientDateTime } from '@/lib/timezone';
import type {
  ChatAttachmentRead,
  ChatMessage,
  ChatSession,
  ChatSessionEventRead,
  KnowledgeCitation,
  ScheduledTaskDraftRead,
  ScheduledTaskRead,
  UIConfigRead,
} from '@/types';

import {
  CHAT_MARKDOWN_CLASS,
  CHAT_MD_TABLE_CLASS,
  CHAT_MD_TABLE_SCROLL_CLASS,
} from './chatPageStyles';
import type {
  ComposerAttachment,
  CotTraceIconName,
  DraftScheduleType,
  SessionSlot,
  TraceLine,
  TraceSkill,
  TraceTool,
  TurnTrace,
} from './chatTypes';
export {
  SELECTED_AGENT_STORAGE_KEY,
  SESSION_FILTER_STORAGE_PREFIX,
  sessionFilterStorageKey,
} from '@/lib/agent-scope-storage';

/** 模型配置选择的 localStorage 存储键前缀 */
export const MODEL_CONFIG_STORAGE_PREFIX = 'skill_agent_selected_model_config';
/** 会话已读时间的 localStorage 存储键前缀 */
export const SESSION_READ_STORAGE_PREFIX = 'skill_agent_session_read_at';
/** 侧边栏折叠状态的 localStorage 存储键 */
export const SIDEBAR_COLLAPSED_STORAGE_KEY = 'skill_agent_sidebar_collapsed';
/** 运行中事件恢复窗口时间（10 分钟），超过此时间未收到事件则放弃恢复 */
export const RUNNING_EVENT_RECOVERY_WINDOW_MS = 600 * 1000;
/** 流式响应空闲超时时间（10 分钟），超时后主动终止流 */
export const CHAT_STREAM_IDLE_TIMEOUT_MS = 600 * 1000;
/** 流式空闲检测间隔（5 秒），定期检查流是否超时 */
export const CHAT_STREAM_IDLE_CHECK_INTERVAL_MS = 5 * 1000;
/** 流式心跳宽限时间（20 秒），心跳间隔超过此值视为异常 */
export const CHAT_STREAM_HEARTBEAT_GRACE_MS = 20 * 1000;
/** 思维链追踪恢复窗口时间（10 分钟），用于判断是否恢复未完成的追踪 */
export const CHAT_TRACE_RECOVERY_WINDOW_MS = 10 * 60 * 1000;
/** 流式终止事件名称集合，收到这些事件表示流式回复已结束 */
export const STREAM_TERMINAL_EVENTS = new Set(['complete', 'done', 'stream_end', 'stream_cancelled', 'stream_interrupted', 'error', 'error_occurred']);
/** 需要隐藏的通用技能追踪阶段（如"回复中"阶段不展示在执行记录中） */
export const HIDDEN_GENERAL_SKILL_TRACE_PHASES = new Set(['replying']);
/** 合法的定时任务调度类型集合 */
const DRAFT_SCHEDULE_TYPES = new Set<DraftScheduleType>(['once', 'daily', 'weekly', 'monthly']);
/** 调度类型的中文标签映射 */
const DRAFT_SCHEDULE_TYPE_LABELS: Record<DraftScheduleType, string> = {
  once: '一次性',
  daily: '每天',
  weekly: '每周',
  monthly: '每月',
};
/** 星期标签（周一到周日），用于每周调度类型的显示 */
const DRAFT_WEEKDAY_LABELS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];

/**
 * 生成会话已读时间的 localStorage 存储键。
 *
 * @param userId - 用户 ID，为空时使用 'anonymous'
 * @returns 格式为 skill_agent_session_read_at:{userId} 的存储键
 */
export function sessionReadStorageKey(userId: string): string {
  return `${SESSION_READ_STORAGE_PREFIX}:${userId || 'anonymous'}`;
}

/**
 * 从 localStorage 加载用户的所有会话已读时间记录。
 *
 * @param userId - 用户 ID
 * @returns 会话 ID 到已读时间字符串（ISO）的映射对象；读取失败时返回空对象
 */
export function loadSessionReadTimes(userId: string): Record<string, string> {
  try {
    const raw = window.localStorage.getItem(sessionReadStorageKey(userId));
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, string>;
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

/**
 * 将会话已读时间记录持久化到 localStorage。
 *
 * @param userId - 用户 ID
 * @param values - 会话 ID 到已读时间字符串的映射对象
 */
export function persistSessionReadTimes(userId: string, values: Record<string, string>): void {
  window.localStorage.setItem(sessionReadStorageKey(userId), JSON.stringify(values));
}

/**
 * 判断一个会话是否为定时任务会话。
 *
 * @param session - 聊天会话对象
 * @returns 如果会话标记为定时任务则返回 true
 */
export function isScheduledSession(session: ChatSession): boolean {
  return session.is_scheduled === true;
}

/**
 * 判断一个会话是否有未读的 AI 回复。
 *
 * 判断逻辑：当前会话有摘要内容、不在运行中状态、且更新时间晚于已读时间（含 1 秒容差）。
 * 当前活跃会话永远返回 false（用户正在查看）。
 *
 * @param session - 聊天会话对象
 * @param readTimes - 会话已读时间映射
 * @param activeSessionId - 当前活跃会话 ID
 * @returns 如果有未读回复则返回 true
 */
export function sessionHasUnreadReply(
  session: ChatSession,
  readTimes: Record<string, string>,
  activeSessionId?: string,
): boolean {
  if (session.id === activeSessionId) return false;
  const summary = session.summary || session.last_agent_question || '';
  if (!summary) return false;
  if (session.status === 'running' || session.status === 'executing') return false;
  const updatedAt = Date.parse(session.updated_at || '');
  const readAt = Date.parse(readTimes[session.id] || '');
  return Number.isFinite(updatedAt) && (!Number.isFinite(readAt) || updatedAt > readAt + 1000);
}

/**
 * 生成草稿会话的本地标识键（用于尚未持久化到服务端的临时会话）。
 *
 * @param agentId - 数字员工 ID
 * @returns 格式为 draft:{agentId} 的标识键
 */
export function draftConversationKey(agentId: string): string {
  return `draft:${agentId}`;
}

/**
 * 判断一个会话 ID 是否为草稿会话标识。
 *
 * @param id - 会话 ID
 * @returns 如果以 'draft:' 开头则返回 true
 */
export function isDraftConversationKey(id: string): boolean {
  return id.startsWith('draft:');
}

/**
 * 判断一个错误是否为"会话不存在"（404）错误。
 * 用于在会话被删除后优雅地处理引用错误。
 *
 * @param error - 待检查的错误对象
 * @returns 如果是 404 API 错误则返回 true
 */
export function isMissingChatSessionError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/**
 * 生成模型配置选择的 localStorage 存储键。
 *
 * @param tenantId - 租户 ID
 * @returns 格式为 skill_agent_selected_model_config:{tenantId} 的存储键
 */
export function modelStorageKey(tenantId: string): string {
  return `${MODEL_CONFIG_STORAGE_PREFIX}:${tenantId}`;
}

/**
 * 获取模型的显示名称，优先使用自定义名称，其次使用模型标识，兜底为"模型"。
 *
 * @param model - 包含 name 和 model 字段的对象
 * @returns 去除首尾空格后的显示名称
 */
export function modelDisplayName(model: { name?: string; model?: string }): string {
  return (model.name || model.model || '模型').trim();
}

/**
 * 生成模型的详情描述文本（模型标识或提供商 + 默认标记）。
 *
 * @param model - 包含 name、model、provider、is_default 的模型对象
 * @returns 详情描述字符串
 */
export function modelDetailText(model: { name?: string; model?: string; provider?: string; is_default?: boolean }): string {
  const detail = model.model && model.model !== model.name ? model.model : model.provider || '';
  return model.is_default ? `${detail} · 默认` : detail;
}

/**
 * 规范化消息文本：将所有空白字符合并为单个空格并去除首尾空格。
 * 用于判断消息是否有可渲染内容。
 *
 * @param value - 原始文本
 * @returns 规范化后的文本
 */
export function normalizeMessageText(value?: string): string {
  return typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : '';
}

/**
 * 判断流式文本是否有足够的可渲染内容（至少 2 个字符）。
 * 避免在流式传输初期就显示不完整的文字片段。
 *
 * @param value - 流式文本
 * @returns 如果规范化后字符数 >= 2 则返回 true
 */
export function hasRenderableStreamingText(value?: string): boolean {
  return Array.from(normalizeMessageText(value)).length >= 2;
}

/**
 * 判断一条消息是否为排队中的用户消息。
 * 排队消息在 AI 正在回复时被加入队列，等待当前回复完成后发送。
 *
 * @param messageItem - 聊天消息
 * @returns 如果是排队的用户消息则返回 true
 */
export function isQueuedChatMessage(messageItem: ChatMessage): boolean {
  return messageItem.role === 'user' && messageItem.metadata?.queued === true;
}

/**
 * 将排队消息排到消息列表末尾。
 * 时间线消息保持原序，排队消息去重后追加到列表尾部，
 * 确保用户看到的对话流自然（先显示已完成的对话，再显示等待发送的消息）。
 *
 * @param messages - 原始消息列表
 * @returns 排序后的消息列表（排队消息在最后）
 */
export function placeQueuedMessagesLast(messages: ChatMessage[]): ChatMessage[] {
  const timeline: ChatMessage[] = [];
  const queued: ChatMessage[] = [];
  const queuedTurnIds = new Set<string>();

  messages.forEach((messageItem) => {
    if (!isQueuedChatMessage(messageItem)) {
      timeline.push(messageItem);
      return;
    }
    const identity = messageItem.turnId || messageItem.id;
    if (queuedTurnIds.has(identity)) return;
    queuedTurnIds.add(identity);
    queued.push(messageItem);
  });

  return [...timeline, ...queued];
}

/**
 * 渲染行内 Markdown 标记（行内代码、粗体、链接、图片）。
 *
 * 使用正则表达式逐个匹配行内 Markdown 语法标记，将文本拆分为
 * 普通文本节点和格式化节点（`<code>`/`<strong>`/`<a>`/`<span>`）。
 * 支持递归处理粗体内部的行内标记。
 *
 * @param text - 包含行内 Markdown 语法的文本
 * @param keyPrefix - React key 前缀，用于生成唯一 key
 * @returns ReactNode 数组
 */
export function renderInlineMarkdown(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = /(`[^`]*`|\*\*[^*]+?\*\*|!?\[[^\]\n]*\]\([^\)\n]+\))/g;
  let cursor = 0;
  let index = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) {
      nodes.push(text.slice(cursor, match.index));
    }
    const token = match[0];
    const key = `${keyPrefix}-inline-${index}`;
    if (token.startsWith('`') && token.endsWith('`')) {
      nodes.push(<code key={key}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith('**') && token.endsWith('**')) {
      nodes.push(<strong key={key}>{renderInlineMarkdown(token.slice(2, -2), key)}</strong>);
    } else {
      const image = token.match(/^!\[([^\]]*)\]\(([^\)\n]+)\)$/);
      if (image) {
        nodes.push(<span key={key}>{image[1] || '图片'}</span>);
        cursor = match.index + token.length;
        index += 1;
        continue;
      }
      const link = token.match(/^\[([^\]]*)\]\(([^\)\n]+)\)$/);
      if (link) {
        const href = link[2].trim();
        const label = link[1] || href;
        if (/^https?:\/\//i.test(href)) {
          nodes.push(
            <a key={key} href={href} target="_blank" rel="noreferrer">
              {label}
            </a>,
          );
        } else {
          nodes.push(
            <span key={key} className="md-link-label" title={href}>
              {label}
            </span>,
          );
        }
      } else {
        nodes.push(token);
      }
    }
    cursor = match.index + token.length;
    index += 1;
  }

  if (cursor < text.length) {
    nodes.push(text.slice(cursor));
  }
  return nodes;
}

/**
 * 计算软换行的分隔符。
 * 当前后字符均为 CJK（中日韩）字符时不插入空格分隔；否则插入一个空格。
 * 这是因为 CJK 文本不需要词间空格，而拉丁语系文本需要。
 *
 * @param previousLine - 上一行文本
 * @param currentLine - 当前行文本
 * @returns 空字符串（CJK 之间）或空格（拉丁语系之间）
 */
function softLineBreakSeparator(previousLine: string, currentLine: string): string {
  const previous = previousLine.trimEnd();
  const current = currentLine.trimStart();
  if (!previous || !current) return '';

  const previousCharacter = previous.charAt(previous.length - 1);
  const currentCharacter = current.charAt(0);
  const cjkCharacter = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u;
  return cjkCharacter.test(previousCharacter) || cjkCharacter.test(currentCharacter) ? '' : ' ';
}

/**
 * 将多行文本逐行渲染为行内 Markdown 节点，并在行间插入分隔符。
 * 首行直接渲染；后续行根据 preserveLineBreaks 参数插入 `<br>` 或软换行分隔符。
 *
 * @param lines - 按换行拆分后的文本行数组
 * @param keyPrefix - React key 前缀
 * @param preserveLineBreaks - true 时用 `<br>` 分隔，false 时用软换行（CJK 之间无空格）
 * @returns 展平后的 ReactNode 数组
 */
function renderInlineLines(lines: string[], keyPrefix: string, preserveLineBreaks: boolean): ReactNode[] {
  return lines.flatMap((line, lineIndex) => {
    const renderedLine = preserveLineBreaks ? line : line.trim();
    const nodes = renderInlineMarkdown(renderedLine, `${keyPrefix}-line-${lineIndex}`);
    if (lineIndex === 0) return nodes;
    const separator = preserveLineBreaks
      ? <br key={`${keyPrefix}-br-${lineIndex}`} />
      : softLineBreakSeparator(lines[lineIndex - 1], line);
    return [separator, ...nodes];
  });
}

type MarkdownTableAlign = 'left' | 'center' | 'right';

/**
 * 解析 Markdown 表格行为单元格数组。
 * 去除首尾的管道符 `|`，并按 `|` 分割（支持转义的 `\|` 和行内代码中的 `|`）。
 *
 * @param row - 单行 Markdown 表格文本
 * @returns 去除空白后的单元格文本数组
 */
function splitMarkdownTableRow(row: string): string[] {
  let text = row.trim();
  if (text.startsWith('|')) text = text.slice(1);
  if (text.endsWith('|')) text = text.slice(0, -1);

  const cells: string[] = [];
  let current = '';
  let inCode = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (char === '`') {
      inCode = !inCode;
      current += char;
      continue;
    }
    if (char === '\\' && text[index + 1] === '|') {
      current += '|';
      index += 1;
      continue;
    }
    if (char === '|' && !inCode) {
      cells.push(current.trim());
      current = '';
      continue;
    }
    current += char;
  }
  cells.push(current.trim());
  return cells;
}

/**
 * 判断一行是否为 Markdown 表格分隔行（如 `|---|:---:|---:|`）。
 * 分隔行仅包含连字符和可选的冒号（用于对齐方式）。
 *
 * @param line - 待检查的文本行
 * @returns 如果是合法的分隔行则返回 true
 */
function isMarkdownTableSeparator(line: string): boolean {
  const cells = splitMarkdownTableRow(line);
  return cells.length >= 2 && cells.every((cell) => /^:?-{3,}:?$/.test(cell.replace(/\s+/g, '')));
}

/**
 * 从分隔行单元格中解析列的对齐方式。
 * `:---:` 居中，`---:` 右对齐，`---` 或 `:---` 左对齐。
 *
 * @param separatorCell - 分隔行的单个单元格文本
 * @returns 对齐方式（'left' / 'center' / 'right'）
 */
function markdownTableAlign(separatorCell: string): MarkdownTableAlign {
  const normalized = separatorCell.replace(/\s+/g, '');
  if (normalized.startsWith(':') && normalized.endsWith(':')) return 'center';
  if (normalized.endsWith(':')) return 'right';
  return 'left';
}

/**
 * 判断从指定位置开始的行是否为 Markdown 表格的起始行。
 * 检查当前行是否包含管道符且下一行是分隔行。
 *
 * @param lines - 全部文本行
 * @param index - 待检查的行索引
 * @returns 如果当前行和下一行构成表格起始则返回 true
 */
function isMarkdownTableStart(lines: string[], index: number): boolean {
  if (index + 1 >= lines.length) return false;
  const header = lines[index].trim();
  if (!header.includes('|')) return false;
  return splitMarkdownTableRow(header).length >= 2 && isMarkdownTableSeparator(lines[index + 1]);
}

/**
 * 渲染 Markdown 表格为带样式的 HTML 表格元素。
 * 解析表头行、分隔行（对齐方式）和数据行，生成 `<table>` 结构。
 *
 * @param lines - 全部文本行
 * @param startIndex - 表格起始行索引（表头行）
 * @param key - React key
 * @returns 包含渲染后的表格节点和下一行索引的对象
 */
function renderMarkdownTable(lines: string[], startIndex: number, key: string): { node: ReactNode; nextIndex: number } {
  const header = splitMarkdownTableRow(lines[startIndex]);
  const separator = splitMarkdownTableRow(lines[startIndex + 1]);
  const aligns = separator.map(markdownTableAlign);
  const rows: string[][] = [];
  let index = startIndex + 2;

  while (index < lines.length) {
    const row = lines[index].trim();
    if (!row || !row.includes('|') || isMarkdownTableSeparator(row)) break;
    const cells = splitMarkdownTableRow(row);
    if (cells.length < 2) break;
    rows.push(cells);
    index += 1;
  }

  const columnCount = Math.max(header.length, separator.length, ...rows.map((row) => row.length));
  const cellStyle = (cellIndex: number) => ({ textAlign: (aligns[cellIndex] || 'left') as MarkdownTableAlign });
  const renderCells = (cells: string[], rowKey: string) =>
    Array.from({ length: columnCount }, (_, cellIndex) => (
      <td key={`${rowKey}-${cellIndex}`} style={cellStyle(cellIndex)}>
        {renderInlineMarkdown(cells[cellIndex] || '', `${rowKey}-${cellIndex}`)}
      </td>
    ));

  return {
    nextIndex: index,
    node: (
      <div key={key} className={CHAT_MD_TABLE_SCROLL_CLASS}>
        <table className={CHAT_MD_TABLE_CLASS}>
          <thead>
            <tr>
              {Array.from({ length: columnCount }, (_, cellIndex) => (
                <th key={`${key}-head-${cellIndex}`} style={cellStyle(cellIndex)}>
                  {renderInlineMarkdown(header[cellIndex] || '', `${key}-head-${cellIndex}`)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={`${key}-row-${rowIndex}`}>{renderCells(row, `${key}-row-${rowIndex}`)}</tr>
            ))}
          </tbody>
        </table>
      </div>
    ),
  };
}

/**
 * 判断一行是否为 Markdown 块级元素的边界。
 * 用于在段落累积时检测是否遇到了新的块级结构（代码围栏、分割线、标题、引用、列表等）。
 *
 * @param line - 待检查的文本行
 * @returns 如果该行是块级边界则返回 true
 */
function isBlockBoundary(line: string): boolean {
  const trimmed = line.trim();
  return (
    trimmed.startsWith('```') ||
    /^(-{3,}|\*{3,}|_{3,})$/.test(trimmed) ||
    /^#{1,6}\s+/.test(trimmed) ||
    /^>\s?/.test(trimmed) ||
    /^[-*]\s+/.test(trimmed) ||
    /^\d+[.)]\s+/.test(trimmed)
  );
}

/**
 * 解析并渲染 Markdown 块级内容。
 *
 * 这是自研 Markdown 解析器的核心函数，逐行扫描文本，识别并渲染以下块级元素：
 * - 代码围栏（```language ... ```）
 * - 水平分割线（--- / *** / ___）
 * - 标题（# ~ ######，最多渲染到 h4）
 * - 引用块（> ...）
 * - 表格（| ... | 配合 |---| 分隔行）
 * - 无序列表（- / * 开头）
 * - 有序列表（1. / 1) 开头）
 * - 普通段落
 *
 * @param content - Markdown 原始文本
 * @param preserveLineBreaks - 是否保留换行（true 用 `<br>`，false 用软换行）
 * @returns ReactNode 块数组
 */
export function renderMarkdownBlocks(content: string, preserveLineBreaks = true): ReactNode[] {
  const lines = content.replace(/\r\n/g, '\n').split('\n');
  const blocks: ReactNode[] = [];
  let index = 0;
  let blockIndex = 0;

  while (index < lines.length) {
    const line = lines[index];
    const trimmed = line.trim();
    const key = `md-${blockIndex}`;
    if (!trimmed) {
      index += 1;
      continue;
    }

    if (trimmed.startsWith('```')) {
      const language = trimmed.slice(3).trim();
      const codeLines: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith('```')) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push(
        <CodeBlock key={key} className="md-code-block" code={codeLines.join('\n')} language={language || undefined} />,
      );
      blockIndex += 1;
      continue;
    }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
      blocks.push(<hr key={key} />);
      index += 1;
      blockIndex += 1;
      continue;
    }

    const heading = trimmed.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const level = Math.min(heading[1].length, 4) as 1 | 2 | 3 | 4;
      const Tag = `h${level}` as keyof JSX.IntrinsicElements;
      blocks.push(<Tag key={key}>{renderInlineMarkdown(heading[2], key)}</Tag>);
      index += 1;
      blockIndex += 1;
      continue;
    }

    if (/^>\s?/.test(trimmed)) {
      const quoteLines: string[] = [];
      while (index < lines.length && /^>\s?/.test(lines[index].trim())) {
        quoteLines.push(lines[index].trim().replace(/^>\s?/, ''));
        index += 1;
      }
      blocks.push(<blockquote key={key}>{renderMarkdownBlocks(quoteLines.join('\n'), preserveLineBreaks)}</blockquote>);
      blockIndex += 1;
      continue;
    }

    if (isMarkdownTableStart(lines, index)) {
      const table = renderMarkdownTable(lines, index, key);
      blocks.push(table.node);
      index = table.nextIndex;
      blockIndex += 1;
      continue;
    }

    if (/^[-*]\s+/.test(trimmed)) {
      const items: string[] = [];
      while (index < lines.length && /^[-*]\s+/.test(lines[index].trim())) {
        items.push(lines[index].trim().replace(/^[-*]\s+/, ''));
        index += 1;
      }
      blocks.push(
        <ul key={key}>
          {items.map((item, itemIndex) => (
            <li key={`${key}-${itemIndex}`}>{renderInlineMarkdown(item, `${key}-${itemIndex}`)}</li>
          ))}
        </ul>,
      );
      blockIndex += 1;
      continue;
    }

    if (/^\d+[.)]\s+/.test(trimmed)) {
      const items: string[] = [];
      while (index < lines.length && /^\d+[.)]\s+/.test(lines[index].trim())) {
        items.push(lines[index].trim().replace(/^\d+[.)]\s+/, ''));
        index += 1;
      }
      blocks.push(
        <ol key={key}>
          {items.map((item, itemIndex) => (
            <li key={`${key}-${itemIndex}`}>{renderInlineMarkdown(item, `${key}-${itemIndex}`)}</li>
          ))}
        </ol>,
      );
      blockIndex += 1;
      continue;
    }

    const paragraphLines: string[] = [];
    while (
      index < lines.length &&
      lines[index].trim() &&
      !isBlockBoundary(lines[index]) &&
      !isMarkdownTableStart(lines, index)
    ) {
      paragraphLines.push(lines[index]);
      index += 1;
    }
    blocks.push(<p key={key}>{renderInlineLines(paragraphLines, key, preserveLineBreaks)}</p>);
    blockIndex += 1;
  }

  return blocks;
}

/**
 * Markdown 消息渲染组件。
 * 将 Markdown 内容渲染为带样式的 HTML，供消息气泡使用。
 *
 * @param content - Markdown 文本内容
 * @param preserveLineBreaks - 是否保留原始换行
 */
export function MarkdownMessage({
  content,
  preserveLineBreaks = true,
}: {
  content: string;
  preserveLineBreaks?: boolean;
}) {
  return <div className={CHAT_MARKDOWN_CLASS}>{renderMarkdownBlocks(content, preserveLineBreaks)}</div>;
}

/**
 * 获取执行记录摘要行的图标名称。
 * 当前始终返回 'execute' 图标。
 *
 * @param _summary - 执行记录摘要（当前未使用）
 * @returns 图标名称
 */
export function traceSummaryIconName(_summary: { state: TraceLine['state'] }): CotTraceIconName {
  return 'execute';
}

/**
 * 根据追踪行的类型和属性确定其图标名称。
 * 如果行已指定 icon 则直接使用；否则根据 kind 字段推断：
 * - decision → judge（判断）
 * - tool → tool（工具）
 * - code → generated（代码生成）
 * - thinking → loading（思考中）
 * - 其他 → advance（推进）
 *
 * @param line - 追踪行
 * @returns 对应的图标名称
 */
export function traceLineIconName(line: TraceLine): CotTraceIconName {
  if (line.icon) return line.icon;
  if (line.kind === 'decision') return 'judge';
  if (line.kind === 'tool') return 'tool';
  if (line.kind === 'code') return 'generated';
  if (line.kind === 'thinking') return 'loading';
  return 'advance';
}

/**
 * 解析消息时间字符串为时间戳。
 * 如果输入缺少时区后缀，则自动追加 'Z'（UTC）。
 *
 * @param value - ISO 时间字符串（可能缺少时区）
 * @returns 时间戳（毫秒），无效输入返回 0
 */
export function parseMessageTime(value?: string): number {
  if (!value) return 0;
  const normalized = /(?:z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  const time = Date.parse(normalized);
  return Number.isFinite(time) ? time : 0;
}

/**
 * 向别名数组追加一个规范化后的轮次 ID（去重）。
 *
 * @param aliases - 已有别名数组（会被原地修改）
 * @param value - 待追加的值
 */
function appendTurnAlias(aliases: string[], value: unknown): void {
  if (typeof value !== 'string') return;
  const normalized = value.trim();
  if (normalized && !aliases.includes(normalized)) aliases.push(normalized);
}

/**
 * 安全提取消息元数据中的字符串字段。
 * 仅返回非空且去除空白后的字符串值。
 *
 * @param messageItem - 聊天消息
 * @param key - 元数据字段名
 * @returns 规范化后的字符串值，无效时返回 undefined
 */
function metadataString(messageItem: ChatMessage, key: string): string | undefined {
  const value = messageItem.metadata?.[key];
  return typeof value === 'string' && value.trim() ? value.trim() : undefined;
}

/**
 * 提取一条消息的所有轮次 ID 别名。
 *
 * 后端可能用不同字段名（turnId、turn_id、user_message_id、client_turn_id 等）
 * 引用同一个轮次，此函数收集所有可能的别名用于轮次关联。
 *
 * @param messageItem - 聊天消息
 * @returns 去重后的轮次 ID 别名数组
 */
export function messageTurnAliases(messageItem: ChatMessage): string[] {
  const aliases: string[] = [];
  appendTurnAlias(aliases, messageItem.turnId);
  appendTurnAlias(aliases, messageItem.turn_id);
  appendTurnAlias(aliases, metadataString(messageItem, 'turn_id'));
  appendTurnAlias(aliases, metadataString(messageItem, 'user_message_id'));
  appendTurnAlias(aliases, metadataString(messageItem, 'client_turn_id'));
  appendTurnAlias(aliases, messageItem.serverMessageId);
  if (messageItem.role === 'user') appendTurnAlias(aliases, messageItem.id);
  return aliases;
}

/**
 * 从消息的所有别名中选择规范轮次 ID。
 * 用户消息优先使用原始 ID（非 local_ 开头），其余取第一个别名。
 *
 * @param messageItem - 聊天消息
 * @param aliases - 已提取的别名数组
 * @returns 规范轮次 ID，无有效值时返回 undefined
 */
function preferredTurnAlias(messageItem: ChatMessage, aliases: string[]): string | undefined {
  if (messageItem.role === 'user' && !messageItem.id.startsWith('local_')) return messageItem.id;
  return aliases[0];
}

/**
 * 使用并查集（Union-Find）算法构建轮次 ID 别名映射表。
 *
 * 核心思路：后端可能用多种不同的 ID 字段引用同一个"对话轮次"，
 * 本函数将所有出现在同一条消息中的 ID 别名合并到同一个规范 ID（canonical）下。
 * 使用并查集实现高效的合并与查找操作。
 *
 * @param messages - 所有消息
 * @returns Map：每个别名 → 其所属的规范 ID
 */
export function buildTurnAliasMap(messages: ChatMessage[]): Map<string, string> {
  const parent = new Map<string, string>();

  const find = (value: string): string => {
    const current = parent.get(value);
    if (!current) {
      parent.set(value, value);
      return value;
    }
    if (current === value) return value;
    const root = find(current);
    parent.set(value, root);
    return root;
  };

  const union = (canonical: string, alias: string) => {
    const canonicalRoot = find(canonical);
    const aliasRoot = find(alias);
    if (canonicalRoot !== aliasRoot) {
      parent.set(aliasRoot, canonicalRoot);
    }
  };

  messages.forEach((messageItem) => {
    const aliases = messageTurnAliases(messageItem);
    const canonical = preferredTurnAlias(messageItem, aliases);
    if (!canonical) return;
    aliases.forEach((alias) => union(canonical, alias));
  });

  const result = new Map<string, string>();
  parent.forEach((_value, key) => {
    result.set(key, find(key));
  });
  return result;
}

/**
 * 在别名映射表中查找给定轮次 ID 的规范 ID。
 * 如果映射表中不存在则返回原始值（去除空白后）。
 *
 * @param turnId - 待查找的轮次 ID
 * @param aliasMap - 并查集别名映射表
 * @returns 规范轮次 ID，输入为空时返回 undefined
 */
export function canonicalTurnIdForValue(turnId: string | null | undefined, aliasMap: Map<string, string>): string | undefined {
  const normalized = typeof turnId === 'string' ? turnId.trim() : '';
  if (!normalized) return undefined;
  return aliasMap.get(normalized) || normalized;
}

/**
 * 获取消息在别名映射表中的规范轮次 ID。
 * 遍历消息的所有别名，返回第一个在映射表中找到的规范 ID。
 *
 * @param messageItem - 聊天消息
 * @param aliasMap - 并查集别名映射表
 * @returns 规范轮次 ID，无匹配时回退到 effectiveMessageTurnId
 */
export function canonicalMessageTurnId(messageItem: ChatMessage, aliasMap: Map<string, string>): string | undefined {
  const aliases = messageTurnAliases(messageItem);
  for (const alias of aliases) {
    const canonical = aliasMap.get(alias);
    if (canonical) return canonical;
  }
  return effectiveMessageTurnId(messageItem);
}

/**
 * 查找指定轮次中最新的用户消息。
 * 通过别名映射将用户消息按轮次分组，优先匹配指定轮次；
 * 如果指定轮次无匹配则回退到所有用户消息，返回时间最新的一条。
 *
 * @param slot - 会话消息槽位
 * @param turnId - 目标轮次 ID（可选）
 * @returns 最新的用户消息，无匹配时返回 undefined
 */
function latestUserMessageForTurn(slot: SessionSlot, turnId?: string | null): ChatMessage | undefined {
  const messages = [...slot.serverMessages, ...slot.realtimeMessages];
  const aliasMap = buildTurnAliasMap(messages);
  const canonicalTurnId = canonicalTurnIdForValue(turnId, aliasMap);
  const scoped = messages.filter((messageItem) => (
    messageItem.role === 'user'
    && (!canonicalTurnId || canonicalMessageTurnId(messageItem, aliasMap) === canonicalTurnId)
  ));
  const candidates = scoped.length
    ? scoped
    : messages.filter((messageItem) => messageItem.role === 'user');
  return candidates.sort((left, right) => parseMessageTime(right.created_at) - parseMessageTime(left.created_at))[0];
}

/**
 * 计算给定消息之后 1 毫秒的 ISO 时间戳。
 * 用于确保后续消息（如流式回复）的时间戳严格晚于用户消息。
 * 无有效时间时以当前时间为基准。
 *
 * @param messageItem - 参考消息（可选）
 * @returns 参考消息时间 +1ms 的 ISO 字符串
 */
function timestampAfterMessage(messageItem?: ChatMessage): string {
  const baseTime = messageItem ? parseMessageTime(messageItem.created_at) : 0;
  return new Date((baseTime > 0 ? baseTime : Date.now()) + 1).toISOString();
}

/**
 * 判断一条消息在指定轮次中是否已有对应的服务端消息。
 * 通过别名映射比较轮次 ID 和角色，用于实时消息去重。
 *
 * @param messageItem - 待检查的实时消息
 * @param serverMessages - 服务端消息列表
 * @returns 如果服务端已有同轮次同角色的消息则返回 true
 */
function hasServerMessageForTurn(messageItem: ChatMessage, serverMessages: ChatMessage[]): boolean {
  const messages = [...serverMessages, messageItem];
  const aliasMap = buildTurnAliasMap(messages);
  const messageTurnId = canonicalMessageTurnId(messageItem, aliasMap);
  if (!messageTurnId) return false;
  return serverMessages.some(
    (serverMessage) => (
      canonicalMessageTurnId(serverMessage, aliasMap) === messageTurnId
      && serverMessage.role === messageItem.role
    ),
  );
}

/**
 * 判断两条消息是否属于同一轮次且角色相同。
 * 通过别名映射比较轮次 ID，同时验证角色一致。
 *
 * @param left - 第一条消息
 * @param right - 第二条消息
 * @returns 如果同轮次同角色则返回 true
 */
export function sameRoleTurn(left: ChatMessage, right: ChatMessage): boolean {
  const aliasMap = buildTurnAliasMap([left, right]);
  const leftTurnId = canonicalMessageTurnId(left, aliasMap);
  const rightTurnId = canonicalMessageTurnId(right, aliasMap);
  return Boolean(leftTurnId && rightTurnId && leftTurnId === rightTurnId && left.role === right.role);
}

/**
 * 判断指定轮次是否已有完成的助手消息（非流式且有内容）。
 * 用于判断 AI 回复是否已完成。
 *
 * @param slot - 会话消息槽位
 * @param turnId - 目标轮次 ID
 * @returns 如果有完成的助手消息则返回 true
 */
export function hasAssistantMessageForTurn(slot: SessionSlot, turnId: string): boolean {
  if (!turnId) return false;
  const messages = [...slot.serverMessages, ...slot.realtimeMessages];
  const aliasMap = buildTurnAliasMap(messages);
  const canonicalTurnId = canonicalTurnIdForValue(turnId, aliasMap);
  return messages.some((messageItem) => (
    messageItem.role === 'assistant'
    && !messageItem.isStreaming
    && canonicalMessageTurnId(messageItem, aliasMap) === canonicalTurnId
    && Boolean(normalizeMessageText(messageItem.content))
  ));
}

/**
 * 判断指定轮次是否已有助手消息载体（包括有内容、错误消息或追踪占位符）。
 * 比 hasAssistantMessageForTurn 更宽松：错误消息和追踪占位符也算作有效载体。
 *
 * @param slot - 会话消息槽位
 * @param turnId - 目标轮次 ID
 * @returns 如果有任何助手消息载体则返回 true
 */
export function hasAssistantCarrierForTurn(slot: SessionSlot, turnId: string): boolean {
  if (!turnId) return false;
  const messages = [...slot.serverMessages, ...slot.realtimeMessages];
  const aliasMap = buildTurnAliasMap(messages);
  const canonicalTurnId = canonicalTurnIdForValue(turnId, aliasMap);
  return messages.some((messageItem) => (
    messageItem.role === 'assistant'
    && !messageItem.isStreaming
    && canonicalMessageTurnId(messageItem, aliasMap) === canonicalTurnId
    && (
      Boolean(normalizeMessageText(messageItem.content))
      || messageItem.isError
      || messageItem.id.startsWith('__trace_')
    )
  ));
}

/**
 * 生成流式消息的虚拟 ID。
 * 格式为 `__streaming_{sessionId}_{turnId}`，用于在实时消息中标识流式回复。
 *
 * @param sessionId - 会话 ID
 * @param turnId - 轮次 ID（可选）
 * @returns 流式消息虚拟 ID
 */
export function streamingMessageId(sessionId: string, turnId?: string | null): string {
  const normalizedTurnId = typeof turnId === 'string' ? turnId.trim() : '';
  return normalizedTurnId ? `__streaming_${sessionId}_${normalizedTurnId}` : `__streaming_${sessionId}`;
}

/**
 * 判断一个消息 ID 是否为流式消息虚拟 ID。
 * 匹配 `__streaming_{sessionId}` 或其带轮次 ID 的变体。
 *
 * @param messageId - 待检查的消息 ID
 * @param sessionId - 会话 ID
 * @returns 如果是流式消息 ID 则返回 true
 */
export function isStreamingMessageId(messageId: string, sessionId: string): boolean {
  const prefix = `__streaming_${sessionId}`;
  return messageId === prefix || messageId.startsWith(`${prefix}_`);
}

/**
 * 在实时消息中插入或更新流式追踪占位符消息。
 * 当 AI 正在思考但没有流式文本输出时，创建一个空的流式消息占位符，
 * 确保思维链追踪行有载体可以附着。
 *
 * @param slot - 会话消息槽位
 * @param sessionId - 会话 ID
 * @param turnId - 轮次 ID
 * @returns 如果消息列表发生了变化则返回 true
 */
export function upsertStreamingTracePlaceholder(slot: SessionSlot, sessionId: string, turnId: string): boolean {
  if (!turnId) return false;
  const streamId = streamingMessageId(sessionId, turnId);
  const streamingMessage: ChatMessage = {
    id: streamId,
    turnId,
    role: 'assistant',
    content: '',
    created_at: timestampAfterMessage(latestUserMessageForTurn(slot, turnId)),
    isStreaming: true,
  };
  const index = slot.realtimeMessages.findIndex((item) => item.id === streamId);
  if (index >= 0) {
    const current = slot.realtimeMessages[index];
    if (
      current.turnId === streamingMessage.turnId
      && current.isStreaming
      && current.content === streamingMessage.content
    ) {
      return false;
    }
    slot.realtimeMessages = [...slot.realtimeMessages];
    slot.realtimeMessages[index] = { ...current, ...streamingMessage, created_at: current.created_at || streamingMessage.created_at };
    return true;
  }
  slot.realtimeMessages = [...slot.realtimeMessages, streamingMessage];
  return true;
}

/**
 * 在实时消息中插入或更新追踪状态占位符消息。
 * 当轮次已完成但没有正式的助手消息时，创建一个追踪占位符消息（`__trace_` 前缀），
 * 用于承载已完成的思维链追踪记录。如果已有正式助手消息则不做替换。
 *
 * @param slot - 会话消息槽位
 * @param sessionId - 会话 ID
 * @param turnId - 轮次 ID
 * @returns 如果消息列表发生了变化则返回 true
 */
export function upsertTraceStatusPlaceholder(slot: SessionSlot, sessionId: string, turnId: string): boolean {
  if (!turnId) return false;
  const traceId = `__trace_${sessionId}_${turnId}`;
  const streamId = streamingMessageId(sessionId, turnId);
  const traceMessage: ChatMessage = {
    id: traceId,
    turnId,
    role: 'assistant',
    content: '',
    created_at: timestampAfterMessage(latestUserMessageForTurn(slot, turnId)),
    isStreaming: false,
  };
  const existingAliasMap = buildTurnAliasMap([...slot.serverMessages, ...slot.realtimeMessages, traceMessage]);
  const canonicalTraceTurnId = canonicalTurnIdForValue(turnId, existingAliasMap);
  const existingAssistantIndex = slot.realtimeMessages.findIndex((item) => (
    item.role === 'assistant'
    && item.id !== traceId
    && item.id !== streamId
    && canonicalMessageTurnId(item, existingAliasMap) === canonicalTraceTurnId
  ));
  if (existingAssistantIndex >= 0) return false;
  const index = slot.realtimeMessages.findIndex((item) => item.id === traceId);
  if (index >= 0) {
    const current = slot.realtimeMessages[index];
    if (current.turnId === traceMessage.turnId && current.content === traceMessage.content) return false;
    slot.realtimeMessages = [...slot.realtimeMessages];
    slot.realtimeMessages[index] = { ...current, ...traceMessage, created_at: current.created_at || traceMessage.created_at };
    return true;
  }
  const streamingIndex = slot.realtimeMessages.findIndex((item) => (
    item.id === streamId
    && canonicalMessageTurnId(item, existingAliasMap) === canonicalTraceTurnId
  ));
  if (streamingIndex >= 0) {
    const current = slot.realtimeMessages[streamingIndex];
    slot.realtimeMessages = slot.realtimeMessages.filter((item, itemIndex) => (
      itemIndex === streamingIndex
      || !(
        item.turnId === turnId
        && item.role === 'assistant'
        && (item.id === traceId || item.id === streamId)
      )
    ));
    const nextIndex = slot.realtimeMessages.findIndex((item) => item === current);
    slot.realtimeMessages[nextIndex] = {
      ...current,
      id: traceId,
      isStreaming: false,
      created_at: current.created_at || traceMessage.created_at,
    };
    return true;
  }
  slot.realtimeMessages = [
    ...slot.realtimeMessages.filter((item) => item.id !== streamId || item.turnId !== turnId),
    traceMessage,
  ];
  return true;
}

/**
 * 从消息对象中提取显式的轮次 ID（turnId 或 turn_id 字段）。
 * 不进行别名推断，仅检查原始字段。
 *
 * @param messageItem - 聊天消息
 * @returns 显式轮次 ID，无有效值时返回 undefined
 */
export function explicitMessageTurnId(messageItem: ChatMessage): string | undefined {
  const camelTurnId = typeof messageItem.turnId === 'string' ? messageItem.turnId.trim() : '';
  if (camelTurnId) return camelTurnId;
  const snakeTurnId = typeof messageItem.turn_id === 'string' ? messageItem.turn_id.trim() : '';
  return snakeTurnId || undefined;
}

/**
 * 获取消息的有效轮次 ID。
 * 优先使用显式字段，用户消息回退到消息 ID。
 *
 * @param messageItem - 聊天消息
 * @returns 有效轮次 ID，无值时返回 undefined
 */
export function effectiveMessageTurnId(messageItem: ChatMessage): string | undefined {
  return explicitMessageTurnId(messageItem) || (messageItem.role === 'user' ? messageItem.id : undefined);
}

/**
 * 从流式事件数据中提取轮次 ID。
 * 依次检查 turn_id、user_message_id 字段，都为空时使用回退值。
 *
 * @param data - 流式事件数据对象
 * @param fallbackTurnId - 回退轮次 ID
 * @returns 提取到的轮次 ID
 */
export function explicitStreamTurnId(data: Record<string, unknown>, fallbackTurnId: string): string {
  const turnId = typeof data.turn_id === 'string' ? data.turn_id.trim() : '';
  if (turnId) return turnId;
  const userMessageId = typeof data.user_message_id === 'string' ? data.user_message_id.trim() : '';
  if (userMessageId) return userMessageId;
  return fallbackTurnId;
}

/**
 * 从会话事件中提取轮次 ID。
 * 优先使用事件数据中的显式字段；对于 user_message_received 事件则使用 message_id。
 *
 * @param event - 会话事件
 * @returns 轮次 ID，无有效值时返回空字符串
 */
export function eventTraceTurnId(event: ChatSessionEventRead): string {
  const data = isPlainRecord(event.data) ? event.data : {};
  const explicit = explicitStreamTurnId(data, '');
  if (explicit) return explicit;
  if (event.event === 'user_message_received') {
    return typeof data.message_id === 'string' ? data.message_id.trim() : '';
  }
  return '';
}

/**
 * 将会话事件（ChatSessionEventRead）规范化为流式事件（StreamEvent）。
 * 后端会话事件和 SSE 流式事件使用不同的事件名称格式，此函数做统一转换：
 * - stream_status → status
 * - router_decision_created → router_decision
 * - assistant_message_created → stream_replace（携带回复内容）
 *
 * @param event - 原始会话事件
 * @returns 规范化后的流式事件
 */
export function normalizeSessionEventForStream(event: ChatSessionEventRead): StreamEvent {
  const data = isPlainRecord(event.data) ? event.data : {};
  if (event.event === 'stream_status') {
    return { event: 'status', data };
  }
  if (event.event === 'router_decision_created') {
    return { event: 'router_decision', data };
  }
  if (event.event === 'assistant_message_created') {
    const content = typeof data.reply === 'string' ? data.reply : '';
    return { event: 'stream_replace', data: { ...data, content } };
  }
  return { event: event.event, data };
}

/**
 * 判断一个会话事件是否为终止事件。
 * assistant_message_created 和其他终止流式事件都视为终止。
 *
 * @param event - 会话事件
 * @param isTerminalStreamEvent - 判断是否为终止流式事件的回调
 * @returns 如果是终止事件则返回 true
 */
export function isTerminalSessionEvent(
  event: ChatSessionEventRead,
  isTerminalStreamEvent: (event: ChatSessionEventRead) => boolean,
): boolean {
  if (event.event === 'assistant_message_created') return true;
  return isTerminalStreamEvent(event);
}

/**
 * 为服务端消息补充轮次 ID。
 * 优先使用消息自身的显式轮次 ID；其次从已绑定的实时消息中回查；
 * 用户消息回退到消息 ID。
 *
 * @param serverMessages - 服务端消息列表
 * @param realtimeMessages - 实时消息列表（用于回查绑定关系）
 * @returns 补充了 turnId 的消息列表
 */
export function attachTurnIdsToServerMessages(
  serverMessages: ChatMessage[],
  realtimeMessages: ChatMessage[],
): ChatMessage[] {
  const realtimeTurnIdsByServerId = new Map(
    realtimeMessages
      .filter((item) => item.turnId && item.serverMessageId)
      .map((item) => [item.serverMessageId as string, item.turnId as string]),
  );

  return serverMessages.map((messageItem) => {
    const turnId = explicitMessageTurnId(messageItem) || realtimeTurnIdsByServerId.get(messageItem.id);
    if (turnId) return { ...messageItem, turnId };
    if (messageItem.role === 'user') return { ...messageItem, turnId: messageItem.id };
    return messageItem;
  });
}

/**
 * 判断一条实时消息是否应该被保留。
 * 排队用户消息始终保留；流式消息仅当属于活跃轮次时保留；
 * 已有服务端消息的实时消息丢弃；有内容的助手消息和晚于服务端消息的保留。
 *
 * @param messageItem - 待检查的实时消息
 * @param serverMessages - 服务端消息列表
 * @param latestServerTime - 最新服务端消息时间戳
 * @param activeTurnId - 当前活跃轮次 ID
 * @returns 如果应该保留则返回 true
 */
function shouldKeepRealtimeMessage(
  messageItem: ChatMessage,
  serverMessages: ChatMessage[],
  latestServerTime: number,
  activeTurnId?: string | null,
): boolean {
  if (messageItem.role === 'user' && messageItem.metadata?.queued === true) return true;
  if (messageItem.isStreaming) {
    const aliasMap = buildTurnAliasMap([...serverMessages, messageItem]);
    const messageTurnId = canonicalMessageTurnId(messageItem, aliasMap);
    const activeCanonicalTurnId = canonicalTurnIdForValue(activeTurnId, aliasMap);
    return !messageTurnId || !activeCanonicalTurnId || messageTurnId === activeCanonicalTurnId;
  }
  if (hasServerMessageForTurn(messageItem, serverMessages)) return false;
  if (messageItem.serverMessageId && serverMessages.some((serverMessage) => serverMessage.id === messageItem.serverMessageId)) {
    return false;
  }
  if (
    messageItem.role === 'assistant'
    && (
      Boolean(normalizeMessageText(messageItem.content))
      || messageItem.isError
      || messageItem.id.startsWith('__trace_')
    )
  ) {
    return true;
  }
  if (activeTurnId) {
    const aliasMap = buildTurnAliasMap([...serverMessages, messageItem]);
    const messageTurnId = canonicalMessageTurnId(messageItem, aliasMap);
    const activeCanonicalTurnId = canonicalTurnIdForValue(activeTurnId, aliasMap);
    if (messageTurnId && activeCanonicalTurnId && messageTurnId === activeCanonicalTurnId) return true;
  }
  if (!latestServerTime) return true;
  return parseMessageTime(messageItem.created_at) > latestServerTime;
}

export { shouldKeepRealtimeMessage, hasServerMessageForTurn, latestUserMessageForTurn, timestampAfterMessage };

/**
 * 合并服务端消息和实时消息，生成最终的显示消息列表。
 *
 * 这是消息合并的核心算法，执行以下步骤：
 * 1. 过滤掉已在服务端消息中存在的实时消息（去重）
 * 2. 通过并查集构建轮次别名映射，将分散的消息按轮次分组
 * 3. 计算每个轮次的起始时间（取该轮次最早的用户消息时间）
 * 4. 按轮次起始时间和角色顺序排序（user → assistant → tool → system）
 * 5. 对于同一轮次的多条 assistant 消息，选择优先级最高的（服务端 > 有内容 > 流式中 > 非流式）
 *
 * @param slot - 会话消息槽位（含服务端消息和实时消息）
 * @param activeTurnId - 当前活跃的轮次 ID（影响流式消息的保留策略）
 * @returns 合并、去重、排序后的消息数组
 */
export function computeMergedMessages(slot: SessionSlot, activeTurnId?: string | null): ChatMessage[] {
  const serverIds = new Set(slot.serverMessages.map((item) => item.id));
  const latestServerTime = Math.max(0, ...slot.serverMessages.map((item) => parseMessageTime(item.created_at)));
  const extras = slot.realtimeMessages.filter((item) => {
    if (serverIds.has(item.id)) return false;
    return shouldKeepRealtimeMessage(item, slot.serverMessages, latestServerTime, activeTurnId);
  });
  const combined = [
    ...slot.serverMessages.map((messageItem, index) => ({ messageItem, index, source: 'server' as const })),
    ...extras.map((messageItem, index) => ({ messageItem, index: slot.serverMessages.length + index, source: 'realtime' as const })),
  ];
  const aliasMap = buildTurnAliasMap(combined.map((entry) => entry.messageItem));
  const turnStarts = new Map<string, number>();
  combined.forEach(({ messageItem }) => {
    if (messageItem.role !== 'user') return;
    const turnId = canonicalMessageTurnId(messageItem, aliasMap);
    if (!turnId) return;
    const createdAt = parseMessageTime(messageItem.created_at);
    const previous = turnStarts.get(turnId);
    if (previous === undefined || createdAt < previous) {
      turnStarts.set(turnId, createdAt);
    }
  });
  combined.forEach(({ messageItem }) => {
    const turnId = canonicalMessageTurnId(messageItem, aliasMap);
    if (!turnId || turnStarts.has(turnId)) return;
    turnStarts.set(turnId, parseMessageTime(messageItem.created_at));
  });
  const roleOrder: Record<ChatMessage['role'], number> = {
    user: 0,
    assistant: 1,
    tool: 2,
    system: 3,
  };

  const sorted = combined
    .sort((left, right) => {
      const leftQueued = left.messageItem.role === 'user' && left.messageItem.metadata?.queued === true;
      const rightQueued = right.messageItem.role === 'user' && right.messageItem.metadata?.queued === true;
      if (leftQueued !== rightQueued) return leftQueued ? 1 : -1;
      const leftTurnId = canonicalMessageTurnId(left.messageItem, aliasMap);
      const rightTurnId = canonicalMessageTurnId(right.messageItem, aliasMap);
      const leftTurnStart = leftTurnId ? turnStarts.get(leftTurnId) : undefined;
      const rightTurnStart = rightTurnId ? turnStarts.get(rightTurnId) : undefined;
      const leftSortTime = leftTurnStart ?? parseMessageTime(left.messageItem.created_at);
      const rightSortTime = rightTurnStart ?? parseMessageTime(right.messageItem.created_at);
      if (leftSortTime !== rightSortTime) return leftSortTime - rightSortTime;
      if (leftTurnId && leftTurnId === rightTurnId && left.messageItem.role !== right.messageItem.role) {
        return (roleOrder[left.messageItem.role] ?? 3) - (roleOrder[right.messageItem.role] ?? 3);
      }
      return (
        parseMessageTime(left.messageItem.created_at) - parseMessageTime(right.messageItem.created_at) ||
        left.index - right.index
      );
    });

  const selectedAssistantByTurn = new Map<string, { messageItem: ChatMessage; index: number; source: 'server' | 'realtime' }>();
  const assistantRank = (entry: { messageItem: ChatMessage; source: 'server' | 'realtime' }) => {
    const content = normalizeMessageText(entry.messageItem.content);
    let rank = 0;
    if (entry.source === 'server') rank += 100;
    if (content) rank += 60;
    if (entry.messageItem.isStreaming && (!activeTurnId || entry.messageItem.turnId === activeTurnId)) rank += 40;
    if (!entry.messageItem.isStreaming) rank += 10;
    return rank;
  };
  sorted.forEach((entry) => {
    if (entry.messageItem.role !== 'assistant') return;
    const turnId = canonicalMessageTurnId(entry.messageItem, aliasMap);
    if (!turnId) return;
    const previous = selectedAssistantByTurn.get(turnId);
    if (!previous || assistantRank(entry) >= assistantRank(previous)) {
      selectedAssistantByTurn.set(turnId, entry);
    }
  });

  return sorted
    .filter((entry) => {
      if (entry.messageItem.role !== 'assistant') return true;
      const turnId = canonicalMessageTurnId(entry.messageItem, aliasMap);
      if (!turnId) return true;
      return selectedAssistantByTurn.get(turnId)?.messageItem === entry.messageItem;
    })
    .map((item) => item.messageItem);
}

/**
 * 根据流式状态数据生成面向用户展示的阶段文本。
 * 将后端 phase（error/preparing/scheduled_task_draft/knowledge 等）映射为中文提示。
 *
 * @param data - 流式事件数据对象
 * @returns 面向用户展示的阶段文本
 */
function publicStreamPhase(data: Record<string, unknown>): string {
  const phase = typeof data.phase === 'string' ? data.phase : '';
  const text = typeof data.text === 'string' ? data.text : '';
  if (phase === 'error') return text || '请求失败';
  if (phase === 'preparing') return text || '正在整理上下文';
  if (phase === 'scheduled_task_draft') return text || '生成定时任务草案';
  if (isKnowledgeTracePhase(phase)) return text || knowledgeTraceText(data);
  return '正在思考';
}

export { publicStreamPhase };

type RecoverableTraceProgress = {
  id?: string;
  kind?: string;
  text?: string;
  detail?: string | null;
  code?: string | null;
  output?: string | null;
  state?: string;
};

/**
 * 判断追踪行列表是否有可恢复的执行进度。
 * 检查是否存在非运行中状态且有内容（detail/code/output/kind/text）的行。
 *
 * @param lines - 追踪行进度数组
 * @returns 如果有可恢复进度则返回 true
 */
function hasRecoverableTraceProgress(lines: RecoverableTraceProgress[]): boolean {
  return lines.some((line) => {
    if (!line) return false;
    if (line.state && line.state !== 'running') return false;
    if (line.detail || line.code || line.output) return true;
    if (line.kind && line.kind !== 'decision') return true;
    const text = String(line.text || '').trim();
    return Boolean(text);
  });
}

/**
 * 判断会话事件列表是否有可恢复的执行进度。
 * 排除 memory_recalled 事件；router_decision_created 事件需要有实际内容才算有进度。
 *
 * @param events - 会话事件列表
 * @returns 如果有可恢复进度则返回 true
 */
export function hasRecoverableEventProgress(events: ChatSessionEventRead[]): boolean {
  return events.some((event) => {
    if (event.event === 'memory_recalled') return false;
    if (event.event === 'router_decision_created') {
      const data = isPlainRecord(event.data) ? event.data : {};
      const intent = typeof data.user_intent === 'string' ? data.user_intent.trim() : '';
      const reason = typeof data.reason === 'string' ? data.reason.trim() : '';
      const decision = typeof data.decision === 'string' ? data.decision.trim() : '';
      return Boolean(intent || reason || decision);
    }
    return true;
  });
}

/**
 * 判断一个运行中的追踪记录是否可恢复。
 * 条件：未完成、有有效起始时间、在恢复窗口内、且有可恢复的追踪行进度。
 *
 * @param row - 追踪记录行（包含完成时间、起始时间和追踪行）
 * @returns 如果可恢复则返回 true
 */
export function isRecoverableRunningTrace(row: { completed_at?: string | null; lines: RecoverableTraceProgress[]; started_at: string }): boolean {
  if (row.completed_at) return false;
  const startedAt = parseMessageTime(row.started_at);
  if (startedAt <= 0) return false;
  if (Date.now() - startedAt > CHAT_TRACE_RECOVERY_WINDOW_MS) return false;
  const lines = row.lines || [];
  return hasRecoverableTraceProgress(lines);
}

const KNOWLEDGE_TRACE_PHASES = new Set([
  'knowledge',
  'okf_route',
  'okf_only',
  'document_route',
  'document_route_lexical',
  'bucket_route',
  'bucket_route_lexical',
  'section_expand',
  'read_chunks',
  'evidence_pack',
  'no_visible_knowledge',
  'no_documents',
  'no_buckets',
]);

/**
 * 判断一个阶段是否为知识检索追踪阶段。
 * 包括知识路由、文档检索、桶检索、分段展开、证据打包等多种知识库相关阶段。
 *
 * @param phase - 阶段标识
 * @returns 如果是知识检索阶段则返回 true
 */
export function isKnowledgeTracePhase(phase: string): boolean {
  return KNOWLEDGE_TRACE_PHASES.has(phase);
}

/**
 * 从事件数据中提取知识检索的显示文本。
 * 优先使用 message 字段，其次 text 字段，兜底为"检索知识库"。
 *
 * @param data - 事件数据对象
 * @returns 知识检索的显示文本
 */
export function knowledgeTraceText(data: Record<string, unknown>): string {
  const raw = typeof data.message === 'string'
    ? data.message
    : typeof data.text === 'string'
      ? data.text
      : '';
  if (!raw) return '检索知识库';
  return raw;
}

/**
 * 生成知识检索追踪行的唯一 ID。
 * 基于查询文本生成，确保同一查询的追踪行能被正确去重。
 *
 * @param data - 事件数据对象
 * @returns 格式为 `knowledge_lookup_{query}` 的行 ID
 */
export function knowledgeTraceLineId(data: Record<string, unknown>): string {
  const rawQuery = isPlainRecord(data.query) && typeof data.query.query === 'string'
    ? data.query.query
    : typeof data.query === 'string'
      ? data.query
      : '';
  const query = rawQuery.trim().replace(/\s+/g, ' ');
  return query ? `knowledge_lookup_${query}` : 'knowledge_lookup';
}

/**
 * 生成知识检索追踪行的详情文本。
 * 汇总查询条件、命中知识图谱数、候选数、片段数、证据数等信息。
 *
 * @param data - 事件数据对象
 * @returns 以 · 分隔的详情文本，无内容时返回 undefined
 */
export function knowledgeTraceDetail(data: Record<string, unknown>): string | undefined {
  const query = isPlainRecord(data.query) && typeof data.query.query === 'string' ? data.query.query : '';
  const parts = [
    query ? `查询：${query}` : '',
    typeof data.selected_count === 'number' ? `命中知识图谱 ${data.selected_count} 个` : '',
    typeof data.candidate_count === 'number' ? `候选 ${data.candidate_count} 个` : '',
    typeof data.chunk_count === 'number' ? `读取 ${data.chunk_count} 个片段` : '',
    typeof data.evidence_count === 'number' ? `整理 ${data.evidence_count} 条证据` : '',
  ].filter(Boolean);
  return parts.length ? parts.join(' · ') : undefined;
}

/**
 * 生成知识检索结果的详情文本。
 * 从知识检索结果中汇总命中知识图谱数、片段数和引用候选数。
 *
 * @param data - 知识结果事件数据
 * @returns 以 · 分隔的详情文本，无内容时返回 undefined
 */
export function knowledgeResultTraceDetail(data: Record<string, unknown>): string | undefined {
  const concepts = Array.isArray(data.selected_concepts) ? data.selected_concepts.length : 0;
  const chunks = Array.isArray(data.chunks) ? data.chunks.length : 0;
  const evidence = Array.isArray(data.evidence_pack) ? data.evidence_pack.length : 0;
  const parts = [
    concepts ? `命中知识图谱 ${concepts} 个` : '',
    chunks ? `读取 ${chunks} 个片段` : '',
    evidence ? `生成 ${evidence} 条引用候选` : '',
  ].filter(Boolean);
  return parts.length ? parts.join(' · ') : undefined;
}

/**
 * 将未知值规范化为 TraceSkill 对象。
 * 验证 skillId 必须存在，其余字段使用类型安全的默认值。
 *
 * @param value - 待规范化的原始值
 * @returns 规范化后的 TraceSkill 对象，无效输入返回 null
 */
export function normalizeTraceSkill(value: unknown): TraceSkill | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  const skillId = typeof item.skillId === 'string' ? item.skillId : '';
  if (!skillId) return null;
  return {
    skillId,
    name: typeof item.name === 'string' ? item.name : skillId,
    stepId: typeof item.stepId === 'string' ? item.stepId : undefined,
    state: typeof item.state === 'string' ? item.state : undefined,
  };
}

/**
 * 根据 SOP 状态和运行时决策生成技能追踪行的显示标签。
 * 映射各种 runtimeDecision（start_skill、suspend、exit 等）到中文标签。
 *
 * @param data - 事件数据对象
 * @param skill - 技能信息
 * @returns 技能追踪行的中文标签
 */
export function streamSkillLabel(data: Record<string, unknown>, skill: TraceSkill): string {
  if (skill.state === 'suspended') return '挂起SOP';
  if (skill.state === 'pending') return '等待SOP';
  const decision = typeof data.runtimeDecision === 'string' ? data.runtimeDecision : '';
  const fromSkillId = typeof data.fromSkillId === 'string' ? data.fromSkillId : '';
  const toSkillId = typeof data.toSkillId === 'string' ? data.toSkillId : '';
  if (decision === 'start_skill' || decision === 'start_new_task') return '选择SOP';
  if (decision === 'suspend_current_and_start_new_skill') return '切换SOP';
  if (
    (decision === 'answer_related_question_then_resume' || decision === 'answer_chitchat_then_resume')
    && fromSkillId
    && toSkillId
    && fromSkillId !== toSkillId
  ) return '切换SOP';
  if (decision === 'exit_current_skill') return '恢复SOP';
  return '推进SOP';
}

/**
 * 将未知值规范化为 TraceTool 对象。
 * 验证 toolId 必须存在，其余字段使用类型安全的默认值。
 *
 * @param value - 待规范化的原始值
 * @returns 规范化后的 TraceTool 对象，无效输入返回 null
 */
export function normalizeTraceTool(value: unknown): TraceTool | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  const toolId = typeof item.toolId === 'string' ? item.toolId : '';
  if (!toolId) return null;
  return {
    toolId,
    toolCallId: typeof item.toolCallId === 'string' ? item.toolCallId : undefined,
    toolName: typeof item.toolName === 'string' ? item.toolName : toolId,
    rawToolName: typeof item.rawToolName === 'string' ? item.rawToolName : toolId,
    success: typeof item.success === 'boolean' ? item.success : undefined,
    isError: typeof item.isError === 'boolean' ? item.isError : undefined,
    content: item.content,
  };
}

/**
 * 将任意值转换为短文本字符串。
 * string 直接返回；number/boolean 转为字符串；其他类型返回空字符串。
 *
 * @param value - 待转换的值
 * @returns 短文本字符串
 */
function shortTraceValue(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return '';
}

/**
 * 生成工具调用的详情文本。
 * 从工具返回数据中提取来源、命中状态、缺失原因、推荐信息等。
 *
 * @param tool - 工具追踪对象
 * @returns 以 · 分隔的详情文本，无内容时返回 undefined
 */
export function toolTraceDetail(tool: TraceTool): string | undefined {
  const content = tool.content && typeof tool.content === 'object' ? tool.content as Record<string, unknown> : null;
  const data = content?.data && typeof content.data === 'object' ? content.data as Record<string, unknown> : null;
  const parts = [
    tool.rawToolName && tool.rawToolName !== tool.toolName ? tool.rawToolName : '',
    shortTraceValue(data?.source),
    data?.found === false ? '未命中' : data?.found === true ? '已命中' : '',
    shortTraceValue(data?.miss_reason),
    shortTraceValue(data?.recommendation),
  ].filter(Boolean);
  return parts.length > 0 ? parts.join(' · ') : undefined;
}

/**
 * 生成反思决策的详情文本。
 * 从反思数据中提取原因、目标工具名、目标 SOP 和步骤等信息。
 *
 * @param data - 反思决策事件数据
 * @returns 以 · 分隔的详情文本，无内容时返回 undefined
 */
export function reflectionTraceDetail(data: Record<string, unknown>): string | undefined {
  const parts = [
    typeof data.reason === 'string' ? data.reason : '',
    typeof data.target_tool_name === 'string' ? `工具 ${data.target_tool_name}` : '',
    typeof data.target_skill_id === 'string' ? `SOP ${data.target_skill_id}` : '',
    typeof data.target_step_id === 'string' ? `步骤 ${data.target_step_id}` : '',
  ].filter(Boolean);
  return parts.length > 0 ? parts.join(' · ') : undefined;
}

/**
 * 生成错误事件的显示文本。
 * 对 LLM_ERROR 返回"模型调用失败"；对 stream_interrupted 返回"响应生成中断"；
 * 其他情况使用错误码或错误类型。
 *
 * @param data - 错误事件数据
 * @param eventName - 事件名称
 * @returns 错误显示文本
 */
function streamErrorText(data: Record<string, unknown>, eventName: string): string {
  const code = typeof data.code === 'string' ? data.code.trim() : '';
  if (code === 'LLM_ERROR') return '模型调用失败';
  if (eventName === 'stream_interrupted') return '响应生成中断';
  if (code) return `执行失败 ${code}`;
  const errorType = typeof data.error_type === 'string' ? data.error_type.trim() : '';
  return errorType ? `执行失败 ${errorType}` : '执行失败';
}

/**
 * 生成错误事件的详情文本。
 * 收集错误码、错误类型、消息、原因等字段，去重后用 · 连接，截断到 2000 字符。
 *
 * @param data - 错误事件数据
 * @returns 去重后的详情文本，无内容时返回 undefined
 */
function streamErrorDetail(data: Record<string, unknown>): string | undefined {
  const parts = [
    typeof data.code === 'string' ? data.code.trim() : '',
    typeof data.error_type === 'string' ? data.error_type.trim() : '',
    typeof data.message === 'string' ? data.message.trim() : '',
    typeof data.reason === 'string' ? data.reason.trim() : '',
    typeof data.text === 'string' ? data.text.trim() : '',
  ].filter(Boolean);
  const deduped = parts.filter((part, index) => parts.indexOf(part) === index);
  return deduped.length > 0 ? deduped.join(' · ').slice(0, 2000) : undefined;
}

/**
 * 构建错误事件的追踪行对象。
 * 根据 code/errorType/eventName 生成唯一行 ID，状态为 failed。
 *
 * @param data - 错误事件数据
 * @param eventName - 事件名称
 * @returns 失败状态的追踪行
 */
export function streamErrorTraceLine(data: Record<string, unknown>, eventName: string): TraceLine {
  const code = typeof data.code === 'string' ? data.code.trim() : '';
  const errorType = typeof data.error_type === 'string' ? data.error_type.trim() : '';
  const key = code || errorType || eventName || 'error';
  return {
    id: eventName === 'stream_interrupted' ? 'generation_interrupted' : `error_${key}`,
    kind: 'decision',
    text: streamErrorText(data, eventName),
    detail: streamErrorDetail(data),
    state: 'failed',
    icon: 'loading',
  };
}

/**
 * 构建路由决策的追踪行对象。
 * 从用户意图、决策、目标 SOP 和步骤信息生成已完成状态的追踪行。
 *
 * @param data - 路由决策事件数据
 * @returns 路由决策追踪行
 */
export function routerDecisionTraceLine(data: Record<string, unknown>): TraceLine {
  const intent = typeof data.user_intent === 'string' ? data.user_intent.trim() : '';
  const decision = typeof data.decision === 'string' ? data.decision.trim() : '';
  const skillId = typeof data.target_skill_id === 'string' ? data.target_skill_id.trim() : '';
  const stepId = typeof data.target_step_id === 'string' ? data.target_step_id.trim() : '';
  const reason = typeof data.reason === 'string' ? data.reason.trim() : '';
  const detail = [reason, skillId ? `目标SOP ${skillId}` : '', stepId ? `目标节点 ${stepId}` : '']
    .filter(Boolean)
    .join(' · ');
  return {
    id: 'decision_router',
    kind: 'decision',
    text: intent ? `判断意图 ${intent}` : decision ? `判断意图 ${decision}` : '判断意图',
    detail: detail || undefined,
    state: 'completed',
    icon: 'judge',
  };
}

/**
 * 构建步骤结果的追踪行对象。
 * 根据步骤结果中的工具调用、知识查询或下一步信息生成追踪行。
 * 包含工具调用时状态为 running，否则根据是否有 next_step_id 判断。
 *
 * @param data - 步骤结果事件数据
 * @returns 步骤结果追踪行
 */
export function stepResultTraceLine(data: Record<string, unknown>): TraceLine {
  const toolCall = isPlainRecord(data.tool_call) ? data.tool_call : undefined;
  const knowledgeQuery = isPlainRecord(data.knowledge_query) ? data.knowledge_query : undefined;
  const nextStepId = typeof data.next_step_id === 'string' ? data.next_step_id.trim() : '';
  const reply = typeof data.reply === 'string' ? data.reply.trim() : '';
  const toolName = typeof toolCall?.name === 'string' ? toolCall.name.trim() : '';
  const knowledgeQueryText = typeof knowledgeQuery?.query === 'string' ? knowledgeQuery.query.trim() : '';
  const detail = [
    nextStepId ? `下一节点 ${nextStepId}` : '',
    knowledgeQueryText ? `查询：${knowledgeQueryText}` : '',
    !toolName && !knowledgeQueryText && reply ? reply.slice(0, 80) : '',
  ].filter(Boolean).join(' · ');

  if (toolName) {
    return {
      id: `decision_step_tool_${toolName}`,
      kind: 'decision',
      text: `决定调用工具 ${toolName}`,
      detail: detail || undefined,
      state: 'running',
      icon: 'tool',
    };
  }
  if (knowledgeQueryText) {
    return {
      id: 'decision_step_knowledge',
      kind: 'decision',
      text: '决定查询知识库',
      detail: detail || undefined,
      state: 'running',
      icon: 'advance',
    };
  }
  return {
    id: 'decision_step_result',
    kind: 'decision',
    text: nextStepId ? '决定下一步' : '完成步骤判断',
    detail: detail || undefined,
    state: 'completed',
    icon: 'advance',
  };
}

/**
 * 合并两条追踪行为一条。
 *
 * 处理临时行（provisional）的优先级：如果新行是临时的而旧行不是，则保留旧行的文本和详情。
 * 状态合并：如果旧行已完成而新行是运行中，保持已完成状态不变。
 *
 * @param existing - 现有的追踪行
 * @param incoming - 新传入的追踪行
 * @returns 合并后的追踪行
 */
export function mergeTraceLine(existing: TraceLine, incoming: TraceLine): TraceLine {
  const keepExistingContent = incoming.provisional === true && existing.provisional !== true;
  const nextState =
    existing.state !== 'running' && incoming.state === 'running'
      ? existing.state
      : incoming.state;
  return {
    ...existing,
    ...incoming,
    text: keepExistingContent ? existing.text : incoming.text || existing.text,
    detail: keepExistingContent ? existing.detail : incoming.detail ?? existing.detail,
    code: incoming.code ?? existing.code,
    language: incoming.language ?? existing.language,
    output: incoming.output ?? existing.output,
    outputLanguage: incoming.outputLanguage ?? existing.outputLanguage,
    outputTitle: incoming.outputTitle ?? existing.outputTitle,
    state: nextState,
    provisional: incoming.provisional === true && existing.provisional === true,
  };
}

/**
 * 合并两个轮次追踪快照。
 *
 * 将新快照合并到现有快照上：按 ID 匹配并合并追踪行，
 * 如果新快照仍在运行中，则保留旧快照中不在新快照里的非占位行。
 * 限制最终行数最多 80 行，防止追踪数据无限增长。
 *
 * @param existing - 现有的轮次追踪（可能为 undefined）
 * @param incoming - 新传入的轮次追踪快照
 * @returns 合并后的轮次追踪
 */
export function mergeTurnTraceSnapshot(existing: TurnTrace | undefined, incoming: TurnTrace): TurnTrace {
  if (!existing) return incoming;

  const existingById = new Map(existing.lines.map((line) => [line.id, line]));
  const incomingIds = new Set(incoming.lines.map((line) => line.id));
  const mergedLines = incoming.lines.map((line) => {
    const previous = existingById.get(line.id);
    return previous ? mergeTraceLine(previous, line) : line;
  });

  const incomingStillRunning = !incoming.completedAt;
  if (incomingStillRunning) {
    existing.lines.forEach((line) => {
      if (!incomingIds.has(line.id) && !line.placeholder) {
        mergedLines.push(line);
      }
    });
  }

  const startedAt = existing.startedAt > 0 && incoming.startedAt > 0
    ? Math.min(existing.startedAt, incoming.startedAt)
    : existing.startedAt || incoming.startedAt;

  return {
    lines: mergedLines.slice(-80),
    startedAt,
    completedAt: incoming.completedAt || existing.completedAt,
  };
}

/**
 * 将任意值格式化为美化后的 JSON 字符串。
 * 字符串类型会尝试 JSON.parse 后再序列化；空值返回空字符串。
 *
 * @param value - 待格式化的值
 * @returns 缩进 2 空格的 JSON 字符串，或原始字符串
 */
function formatTracePayload(value: unknown): string {
  if (value === undefined || value === null || value === '') return '';
  if (typeof value === 'string') {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  return JSON.stringify(value, null, 2);
}

/**
 * 类型守卫：判断值是否为普通对象（非 null、非数组）。
 *
 * @param value - 待检查的值
 * @returns 如果是普通对象则返回 true
 */
export function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

/**
 * 检测文本内容的语言类型。
 * 尝试 JSON.parse 成功则返回 'json'，否则返回 'text'。
 *
 * @param value - 文本内容
 * @returns 'json' 或 'text'
 */
function tracePayloadLanguage(value: string): string {
  if (!value.trim()) return 'text';
  try {
    JSON.parse(value);
    return 'json';
  } catch {
    return 'text';
  }
}

/**
 * 生成通用技能追踪行的详情文本。
 * 反思阶段（reflection_）提取原因和修复提示；其他阶段使用 rationale 或 text 字段。
 *
 * @param data - 事件数据对象
 * @param phase - 技能阶段标识
 * @returns 详情文本，无内容时返回 undefined
 */
export function generalSkillTraceDetail(data: Record<string, unknown>, phase: string): string | undefined {
  const review = isPlainRecord(data.review) ? data.review : undefined;
  if (phase.startsWith('reflection_')) {
    return [
      typeof review?.reason === 'string' ? review.reason : '',
      typeof review?.repair_hint === 'string' ? review.repair_hint : '',
    ]
      .filter(Boolean)
      .join(' · ') || undefined;
  }
  const detail = typeof data.rationale === 'string'
    ? data.rationale
    : typeof data.text === 'string'
      ? data.text
      : undefined;
  return detail?.trim() || undefined;
}

/**
 * 从通用技能事件数据中提取输出信息（代码输出、错误输出、执行结果等）。
 * 根据阶段（stdout_chunk/stderr_chunk/code_finished/reflection_ 等）生成不同的输出内容和语言类型。
 *
 * @param data - 事件数据对象
 * @param phase - 技能阶段标识
 * @param accumulatedText - 累积的流式文本（用于 stdout/stderr 分块）
 * @returns 包含 output/language/title 的输出信息对象
 */
export function generalSkillTraceOutput(data: Record<string, unknown>, phase: string, accumulatedText?: string): {
  output?: string;
  language?: string;
  title?: string;
} {
  if (phase === 'stdout_chunk') {
    const output = formatTracePayload(accumulatedText || data.stdout_preview || data.text);
    return output ? { output, language: tracePayloadLanguage(output), title: '查看运行输出' } : {};
  }
  if (phase === 'stderr_chunk') {
    const output = formatTracePayload(accumulatedText || data.stderr_preview || data.text);
    return output ? { output, language: tracePayloadLanguage(output), title: '查看错误输出' } : {};
  }
  if (phase === 'code_finished' || phase === 'code_timeout') {
    const result: Record<string, unknown> = {};
    if ('return_code' in data) result.return_code = data.return_code;
    if ('structured_result' in data) result.structured_result = data.structured_result;
    if (typeof data.stdout_preview === 'string' && data.stdout_preview.trim()) result.stdout = data.stdout_preview;
    if (typeof data.stderr_preview === 'string' && data.stderr_preview.trim()) result.stderr = data.stderr_preview;
    const output = Object.keys(result).length > 0
      ? formatTracePayload(result)
      : formatTracePayload(data.stdout_preview || data.stderr_preview || data.text);
    return output ? { output, language: tracePayloadLanguage(output), title: phase === 'code_timeout' ? '查看超时结果' : '查看执行结果' } : {};
  }
  if (phase.startsWith('reflection_')) {
    const result: Record<string, unknown> = {};
    if ('structured_result' in data) result.structured_result = data.structured_result;
    if ('review' in data) result.review = data.review;
    if (typeof data.stdout_preview === 'string' && data.stdout_preview.trim()) result.stdout = data.stdout_preview;
    if (typeof data.stderr_preview === 'string' && data.stderr_preview.trim()) result.stderr = data.stderr_preview;
    const output = Object.keys(result).length > 0 ? formatTracePayload(result) : '';
    return output ? { output, language: tracePayloadLanguage(output), title: '查看校验详情' } : {};
  }
  return {};
}

/**
 * 根据追踪行类型和 UI 配置判断是否显示该行。
 * 失败的行始终显示；思考/决策/代码行受 show_thinking_trace 控制；
 * 技能行受 show_skill_trace 控制；工具行受 show_tool_trace 控制。
 *
 * @param line - 追踪行
 * @param config - UI 配置
 * @returns 如果应该显示则返回 true
 */
export function traceLineAllowed(line: TraceLine, config: UIConfigRead): boolean {
  if (line.state === 'failed') return true;
  if (line.kind === 'thinking' || line.kind === 'decision') return config.show_thinking_trace;
  if (line.kind === 'code') return config.show_thinking_trace;
  if (line.kind === 'skill') return config.show_skill_trace;
  if (line.kind === 'tool') return config.show_tool_trace;
  return true;
}

/**
 * 根据追踪状态生成执行记录的摘要文本和状态。
 * - 已完成且包含失败行 → "执行遇到问题"（failed）
 * - 已完成且无失败 → "执行记录"（completed）
 * - 包含运行中行 → "执行记录"（running）
 *
 * @param trace - 轮次追踪
 * @param lines - 可见的追踪行
 * @returns 包含 text 和 state 的摘要对象
 */
export function traceSummary(trace: TurnTrace, lines: TraceLine[]): { text: string; state: TraceLine['state'] } {
  if (trace.completedAt) {
    if (lines.some((line) => line.state === 'failed')) {
      return { text: '执行遇到问题', state: 'failed' };
    }
    return { text: '执行记录', state: 'completed' };
  }
  if (lines.some((line) => line.state === 'running')) {
    return { text: '执行记录', state: 'running' };
  }
  if (lines.some((line) => line.state === 'failed')) {
    return { text: '执行遇到问题', state: 'failed' };
  }
  return { text: '执行记录', state: 'completed' };
}

/**
 * 过滤出需要展示详情的追踪行。
 * 排除占位符行；非失败的 thinking 行也被排除（折叠在摘要中）。
 *
 * @param lines - 全部追踪行
 * @returns 需要展示详情的追踪行数组
 */
export function traceDetails(lines: TraceLine[]): TraceLine[] {
  const details = lines.filter((line) => {
    if (line.placeholder) return false;
    if (line.kind === 'thinking' && line.state !== 'failed') return false;
    return true;
  });
  return details.length > 0
    ? details
    : lines.filter((line) => !line.placeholder && (line.kind !== 'thinking' || line.state === 'failed'));
}

/**
 * 判断一条消息是否可以被评价（点赞/点踩）。
 * 仅已完成的非错误助手消息、且 ID 不以虚拟前缀（__/text_/error_）开头时可评价。
 *
 * @param item - 聊天消息
 * @returns 如果可评价则返回 true
 */
export function canRateMessage(item: ChatMessage): boolean {
  return (
    item.role === 'assistant'
    && !item.isStreaming
    && !item.isError
    && !item.id.startsWith('__')
    && !item.id.startsWith('text_')
    && !item.id.startsWith('error_')
  );
}

/**
 * 去除内容末尾的引用摘要（当前为空实现，保留接口以备扩展）。
 *
 * @param content - 原始内容
 * @returns 原始内容（未修改）
 */
export function stripTrailingCitationSummary(content: string): string {
  return content;
}

/**
 * 从正文中提取所有 [n] 格式的引用序号。
 * 使用正则匹配，返回大于等于 1 的整序号集合。
 *
 * @param content - 消息正文
 * @returns 引用序号集合
 */
function citationLabelsInContent(content: string): Set<number> {
  const labels = new Set<number>();
  content.replace(/\[(\d+)\]/g, (_match, value: string) => {
    const label = Number(value);
    if (Number.isInteger(label) && label >= 1) {
      labels.add(label);
    }
    return _match;
  });
  return labels;
}

/**
 * 从引用对象的 label 或 id 中解析引用序号。
 * 匹配 [n] 格式，解析失败时使用回退值。
 *
 * @param citation - 知识引用对象
 * @param fallback - 回退序号
 * @returns 解析出的序号
 */
function citationLabelNumber(citation: KnowledgeCitation, fallback: number): number {
  const labelText = citation.label || citation.id;
  const match = String(labelText || '').match(/\[(\d+)\]/);
  if (match) {
    const label = Number(match[1]);
    if (Number.isInteger(label) && label >= 1) {
      return label;
    }
  }
  return fallback;
}

/**
 * 从消息元数据中提取与正文匹配的知识引用。
 * 仅返回正文中通过 [n] 标记引用的知识引用，并按标记序号排序。
 *
 * @param item - 聊天消息
 * @param content - 消息正文（用于匹配 [n] 标记）
 * @returns 去重排序后的知识引用数组
 */
export function knowledgeCitations(item: ChatMessage, content: string): KnowledgeCitation[] {
  const citations = item.metadata?.knowledge_citations;
  if (!Array.isArray(citations)) return [];
  const usedLabels = citationLabelsInContent(content);
  if (usedLabels.size === 0) return [];
  const seen = new Set<string>();
  const result: KnowledgeCitation[] = [];
  citations.forEach((citation, index) => {
    if (!citation || !citation.id) return;
    const labelNumber = citationLabelNumber(citation, index + 1);
    if (!usedLabels.has(labelNumber)) return;
    const identity = (
      citation.title || citation.section_path || citation.summary || citation.excerpt || citation.source_path || citation.concept_id || citation.id
    );
    const key = normalizeMessageText(identity).toLowerCase();
    if (!key || seen.has(key)) return;
    seen.add(key);
    result.push({ ...citation, label: `[${labelNumber}]` });
  });
  return result.sort((a, b) => citationLabelNumber(a, 0) - citationLabelNumber(b, 0));
}

/**
 * 从消息元数据中提取定时任务草案。
 * 验证草案包含 should_create、title、prompt、agent_id 等必要字段。
 *
 * @param item - 聊天消息
 * @returns 定时任务草案对象，无效时返回 null
 */
export function scheduledDraftForMessage(item: ChatMessage): ScheduledTaskDraftRead | null {
  const draft = item.metadata?.scheduled_task_draft;
  if (!isPlainRecord(draft) || draft.should_create === false) return null;
  if (typeof draft.title !== 'string' || typeof draft.prompt !== 'string' || typeof draft.agent_id !== 'string') {
    return null;
  }
  return draft as unknown as ScheduledTaskDraftRead;
}

/**
 * 从消息元数据中提取已创建的定时任务信息。
 * 验证 id、title、prompt 等必要字段。
 *
 * @param item - 聊天消息
 * @returns 定时任务对象，无效时返回 undefined
 */
export function createdScheduledTaskForMessage(item: ChatMessage): ScheduledTaskRead | undefined {
  const task = item.metadata?.scheduled_task_created;
  if (!isPlainRecord(task)) return undefined;
  if (typeof task.id !== 'string' || typeof task.title !== 'string' || typeof task.prompt !== 'string') {
    return undefined;
  }
  return task as unknown as ScheduledTaskRead;
}

/**
 * 判断一条消息是否为定时任务模式的用户消息。
 *
 * @param item - 聊天消息
 * @returns 如果是 scheduled_task 模式的用户消息则返回 true
 */
export function isScheduledTaskPrompt(item: ChatMessage): boolean {
  return item.role === 'user' && item.metadata?.interaction_mode === 'scheduled_task';
}

/**
 * 获取知识引用的类型标签。
 * concept → 知识图谱；okf → 知识图谱引用；其他 → 引用来源。
 *
 * @param citation - 知识引用对象
 * @returns 类型标签文本
 */
export function citationKindLabel(citation: KnowledgeCitation): string {
  if (citation.kind === 'concept') return '知识图谱';
  if (citation.kind === 'okf') return '知识图谱引用';
  return '引用来源';
}

/**
 * 获取知识引用的显示标题。
 * 依次尝试 title、section_path、source_path、concept_id，兜底为"知识引用"。
 *
 * @param citation - 知识引用对象
 * @returns 去除空白后的显示标题
 */
export function citationDisplayTitle(citation: KnowledgeCitation): string {
  const raw = citation.title || citation.section_path || citation.source_path || citation.concept_id || '知识引用';
  return raw.trim() || '知识引用';
}

/**
 * 获取知识引用的来源路径标签。
 *
 * @param citation - 知识引用对象
 * @returns 来源路径文本，无值时返回空字符串
 */
export function citationSourceLabel(citation: KnowledgeCitation): string {
  const raw = citation.source_path || '';
  if (!raw) return '';
  return raw.trim();
}

/**
 * 获取知识引用的章节标签。
 * 优先使用 section_path，其次 title。
 *
 * @param citation - 知识引用对象
 * @returns 章节标签文本
 */
export function citationSectionLabel(citation: KnowledgeCitation): string {
  const raw = citation.section_path || citation.title || '';
  return raw.trim();
}

// ---------------------------------------------------------------------------
// 剪贴板 / 粘贴图片辅助函数
// 处理用户在输入框中粘贴图片的操作，支持多种图片来源：
// - 本地文件粘贴（clipboard.files / clipboard.items）
// - HTML 中的 data URL 图片（base64 内联图片）
// - HTML 中的远程图片 URL（自动下载转换为文件）
// - navigator.clipboard.read() 异步剪贴板 API
// ---------------------------------------------------------------------------
const MAX_PASTED_REMOTE_IMAGES = 6;

type ClipboardImageItem = {
  types: readonly string[];
  getType: (type: string) => Promise<Blob>;
};

/**
 * 判断剪贴板数据是否包含可处理的图片内容。
 * 检查 files、items 和 HTML/纯文本中的图片 URL。
 *
 * @param clipboardData - 粘贴事件的 DataTransfer 对象
 * @returns 如果包含图片则返回 true
 */
export function clipboardContainsComposerImage(clipboardData: DataTransfer): boolean {
  if (Array.from(clipboardData.files || []).some((file) => file.type.startsWith('image/'))) {
    return true;
  }
  if (Array.from(clipboardData.items || []).some((item) => item.kind === 'file' && item.type.startsWith('image/'))) {
    return true;
  }
  return extractImageSourceUrls(clipboardData.getData('text/html')).length > 0
    || extractImageSourceUrls(clipboardData.getData('text/plain')).length > 0;
}

/**
 * 从粘贴数据中提取所有图片文件。
 *
 * 异步处理流程：
 * 1. 先同步提取本地文件和 data URL 图片
 * 2. 从 HTML/纯文本中提取远程图片 URL 并下载（最多 MAX_PASTED_REMOTE_IMAGES 张）
 * 3. 如果以上都没有图片，尝试使用 navigator.clipboard.read() 异步 API
 * 4. 全程去重，避免重复添加相同文件
 *
 * @param clipboardData - 粘贴事件的 DataTransfer 对象
 * @returns 提取到的 File 数组
 */
export async function extractPastedComposerFiles(clipboardData: DataTransfer): Promise<File[]> {
  const files = extractPastedComposerFilesSync(clipboardData);
  const seen = new Set(files.map(pastedFileKey));

  const pushFile = (file: File | null | undefined) => {
    if (!file || file.size <= 0) return;
    const key = pastedFileKey(file);
    if (seen.has(key)) return;
    seen.add(key);
    files.push(file);
  };

  const imageSources = [
    ...extractImageSourceUrls(clipboardData.getData('text/html')),
    ...extractImageSourceUrls(clipboardData.getData('text/plain')),
  ].filter((source) => !isImageDataUrl(source));

  for (const [index, source] of imageSources.slice(0, MAX_PASTED_REMOTE_IMAGES).entries()) {
    pushFile(await imageSourceToFile(source, files.length + index));
  }

  if (files.length === 0) {
    const clipboardImages = await readClipboardImageItems();
    clipboardImages.forEach((file) => pushFile(file));
  }

  return files;
}

/**
 * 同步提取粘贴数据中的本地图片文件（剪贴板文件、data URL 图片）。
 * 不处理远程图片下载，仅处理可直接获取的文件。
 *
 * @param clipboardData - 粘贴事件的 DataTransfer 对象
 * @returns 本地图片文件数组（已去重）
 */
function extractPastedComposerFilesSync(clipboardData: DataTransfer): File[] {
  const files: File[] = [];
  const seen = new Set<string>();

  const pushFile = (file: File | null | undefined, index: number) => {
    if (!file || file.size <= 0) return;
    const normalized = normalizePastedFile(file, index);
    const key = pastedFileKey(normalized);
    if (seen.has(key)) return;
    seen.add(key);
    files.push(normalized);
  };

  Array.from(clipboardData.files || []).forEach((file, index) => pushFile(file, index));

  Array.from(clipboardData.items || []).forEach((item, index) => {
    if (item.kind !== 'file' || !item.type.startsWith('image/')) return;
    pushFile(item.getAsFile(), files.length + index);
  });

  const dataUrls = [
    ...extractImageDataUrls(clipboardData.getData('text/html')),
    ...extractImageDataUrls(clipboardData.getData('text/plain')),
  ];
  dataUrls.forEach((dataUrl, index) => pushFile(dataUrlToImageFile(dataUrl, index), files.length + index));

  return files;
}

/**
 * 规范化粘贴文件的文件名和类型。
 * 为无意义文件名的图片生成带时间戳和扩展名的文件名。
 *
 * @param file - 原始文件
 * @param index - 文件索引（用于生成唯一文件名）
 * @returns 规范化后的文件对象
 */
function normalizePastedFile(file: File, index: number): File {
  const type = file.type || 'application/octet-stream';
  const hasUsefulName = Boolean(file.name && !/^image\.(png|jpe?g|gif|webp)$/i.test(file.name));
  if (hasUsefulName) return file;

  const filename = type.startsWith('image/')
    ? `pasted-image-${Date.now()}-${index + 1}.${imageExtension(type)}`
    : (file.name || `pasted-file-${Date.now()}-${index + 1}`);
  return new File([file], filename, { type, lastModified: file.lastModified || Date.now() });
}

/**
 * 生成粘贴文件的唯一去重键（类型:大小）。
 *
 * @param file - 文件对象
 * @returns 去重键字符串
 */
function pastedFileKey(file: File): string {
  return `${file.type || 'application/octet-stream'}:${file.size}`;
}

/**
 * 从原始文本中提取所有 data URL 格式的图片。
 * 仅保留 base64 编码的内联图片。
 *
 * @param raw - 原始文本（HTML 或纯文本）
 * @returns data URL 图片地址数组
 */
function extractImageDataUrls(raw: string): string[] {
  return extractImageSourceUrls(raw).filter(isImageDataUrl);
}

/**
 * 从原始文本中提取所有图片来源 URL（data URL + 远程 URL）。
 * 使用 DOMParser 解析 HTML 中的 `<img>` 标签，同时用正则匹配裸 URL 和 data URL。
 *
 * @param raw - 原始文本（HTML 或纯文本）
 * @returns 去重后的图片来源 URL 数组
 */
function extractImageSourceUrls(raw: string): string[] {
  if (!raw) return [];
  const urls = new Set<string>();
  const text = raw.trim();
  if (isImageDataUrl(text) || isLikelyImageUrl(text)) {
    urls.add(text);
  }

  try {
    const document = new DOMParser().parseFromString(raw, 'text/html');
    Array.from(document.images).forEach((image) => {
      const src = image.getAttribute('src') || '';
      if (isSupportedPastedImageSource(src, true)) urls.add(src.trim());
    });
  } catch {
    // DOMParser is best-effort here; the regex below still catches inline image data.
  }

  const matches = raw.match(/data:image\/[a-z0-9.+-]+(?:;[a-z0-9.+-]+=[^,;]*)*;base64,[a-z0-9+/=\r\n]+/gi) || [];
  matches.forEach((url) => {
    if (isImageDataUrl(url)) urls.add(url);
  });
  const urlMatches = raw.match(/https?:\/\/[^\s"'<>]+/gi) || [];
  urlMatches.forEach((url) => {
    if (isLikelyImageUrl(url)) urls.add(url);
  });
  return Array.from(urls);
}

/**
 * 判断字符串是否为 data URL 格式的图片。
 * 匹配 `data:image/...;base64,...` 或其他编码格式。
 *
 * @param value - 待检查的字符串
 * @returns 如果是 data URL 图片则返回 true
 */
function isImageDataUrl(value: string): boolean {
  return /^data:image\/[a-z0-9.+-]+(?:;[^,]*)?,/i.test(value.trim());
}

/**
 * 判断一个图片来源 URL 是否为受支持的格式。
 * 支持 data URL、blob URL 和 http(s) URL；`<img>` 元素来源放宽 URL 验证。
 *
 * @param value - 图片来源 URL
 * @param fromImageElement - 是否来自 `<img>` 元素（放宽 URL 验证）
 * @returns 如果支持则返回 true
 */
function isSupportedPastedImageSource(value: string, fromImageElement = false): boolean {
  const trimmed = value.trim();
  if (!trimmed) return false;
  if (isImageDataUrl(trimmed)) return true;
  if (trimmed.startsWith('blob:')) return true;
  if (/^https?:\/\//i.test(trimmed)) return fromImageElement || isLikelyImageUrl(trimmed);
  if (trimmed.startsWith('//')) return fromImageElement || isLikelyImageUrl(`https:${trimmed}`);
  return false;
}

/**
 * 判断一个 URL 是否为图片 URL（通过文件扩展名判断）。
 * 匹配 png/jpg/jpeg/gif/webp/bmp/svg/heic/tiff 等常见图片扩展名。
 *
 * @param value - 待检查的 URL
 * @returns 如果疑似图片 URL 则返回 true
 */
function isLikelyImageUrl(value: string): boolean {
  return /^https?:\/\/[^\s"'<>]+\.(?:png|jpe?g|gif|webp|bmp|svg|heic|tiff?)(?:[?#][^\s"'<>]*)?$/i.test(value.trim());
}

/**
 * 将图片来源 URL 转换为 File 对象。
 * data URL 直接解码；远程 URL 通过 fetch 下载并验证 Content-Type。
 *
 * @param source - 图片来源 URL
 * @param index - 文件索引（用于生成文件名）
 * @returns 转换后的 File 对象，失败时返回 null
 */
async function imageSourceToFile(source: string, index: number): Promise<File | null> {
  const normalized = normalizePastedImageSource(source);
  if (!normalized) return null;
  if (isImageDataUrl(normalized)) {
    return dataUrlToImageFile(normalized, index);
  }
  try {
    const response = await fetch(normalized);
    if (!response.ok) return null;
    const blob = await response.blob();
    if (!blob.type.startsWith('image/')) return null;
    return blobToPastedImageFile(blob, index, pastedImageNameFromUrl(normalized));
  } catch {
    return null;
  }
}

/**
 * 规范化粘贴图片来源 URL。
 * 补全协议（`//` → `https://`），过滤不支持的格式。
 *
 * @param source - 原始来源字符串
 * @returns 规范化后的 URL，不支持时返回 null
 */
function normalizePastedImageSource(source: string): string | null {
  const trimmed = source.trim();
  if (!trimmed) return null;
  if (isImageDataUrl(trimmed) || trimmed.startsWith('blob:') || /^https?:\/\//i.test(trimmed)) return trimmed;
  if (trimmed.startsWith('//')) return `${window.location.protocol}${trimmed}`;
  return null;
}

/**
 * 从图片 URL 中提取文件名。
 * 解析 URL 路径的最后一段，仅当是合法图片文件名时返回。
 *
 * @param source - 图片 URL
 * @returns 图片文件名，无法解析或非图片文件名时返回 undefined
 */
function pastedImageNameFromUrl(source: string): string | undefined {
  try {
    const pathname = new URL(source, window.location.href).pathname;
    const name = decodeURIComponent(pathname.split('/').filter(Boolean).pop() || '');
    return isLikelyImageFilename(name) ? name : undefined;
  } catch {
    return undefined;
  }
}

/**
 * 判断文件名是否为合法的图片文件名（通过扩展名判断）。
 *
 * @param value - 文件名
 * @returns 如果是图片文件名则返回 true
 */
function isLikelyImageFilename(value: string): boolean {
  return /\.(?:png|jpe?g|gif|webp|bmp|svg|heic|tiff?)$/i.test(value);
}

/**
 * 使用异步剪贴板 API（navigator.clipboard.read）读取图片文件。
 * 遍历剪贴板项，提取所有 image/* 类型的 Blob 并转换为 File。
 *
 * @returns 图片文件数组，不支持或失败时返回空数组
 */
async function readClipboardImageItems(): Promise<File[]> {
  const clipboard = navigator.clipboard as (Clipboard & { read?: () => Promise<ClipboardImageItem[]> }) | undefined;
  if (!clipboard?.read) return [];
  try {
    const items = await clipboard.read();
    const files: File[] = [];
    for (const [index, item] of items.entries()) {
      const imageType = item.types.find((type) => type.startsWith('image/'));
      if (!imageType) continue;
      const blob = await item.getType(imageType);
      files.push(blobToPastedImageFile(blob, index));
    }
    return files;
  } catch {
    return [];
  }
}

/**
 * 将 data URL 格式的图片转换为 File 对象。
 * 支持 base64 和 URL 编码两种格式。
 *
 * @param dataUrl - data URL 字符串
 * @param index - 文件索引（用于生成文件名）
 * @returns 转换后的 File 对象，解析失败时返回 null
 */
function dataUrlToImageFile(dataUrl: string, index: number): File | null {
  const match = dataUrl.trim().match(/^data:(image\/[a-z0-9.+-]+)((?:;[^,]*)?),(.*)$/i);
  if (!match) return null;
  const type = match[1] || 'image/png';
  const meta = match[2] || '';
  const payload = match[3] || '';

  try {
    const bytes = meta.toLowerCase().includes(';base64')
      ? bytesFromBase64(payload)
      : new TextEncoder().encode(decodeURIComponent(payload));
    const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
    return new File([buffer], `pasted-image-${Date.now()}-${index + 1}.${imageExtension(type)}`, { type });
  } catch {
    return null;
  }
}

/**
 * 将 Blob 转换为带文件名的图片 File 对象。
 *
 * @param blob - Blob 数据
 * @param index - 文件索引（用于生成默认文件名）
 * @param filename - 指定文件名（可选）
 * @returns File 对象
 */
function blobToPastedImageFile(blob: Blob, index: number, filename?: string): File {
  const type = blob.type || 'image/png';
  return new File([blob], filename || `pasted-image-${Date.now()}-${index + 1}.${imageExtension(type)}`, {
    type,
    lastModified: Date.now(),
  });
}

/**
 * 将 base64 编码的字符串解码为 Uint8Array。
 *
 * @param payload - base64 编码的字符串
 * @returns 解码后的字节数组
 */
function bytesFromBase64(payload: string): Uint8Array {
  const binary = window.atob(payload.replace(/\s/g, ''));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

/**
 * 根据 Content-Type 获取图片的文件扩展名。
 * 支持 jpeg/png/gif/webp/bmp/svg/heic/tiff，兜底返回 png。
 *
 * @param contentType - MIME 类型字符串
 * @returns 文件扩展名（不含点）
 */
function imageExtension(contentType: string): string {
  const normalized = contentType.toLowerCase().split(';')[0];
  if (normalized === 'image/jpeg' || normalized === 'image/jpg') return 'jpg';
  if (normalized === 'image/png') return 'png';
  if (normalized === 'image/gif') return 'gif';
  if (normalized === 'image/webp') return 'webp';
  if (normalized === 'image/bmp') return 'bmp';
  if (normalized === 'image/svg+xml') return 'svg';
  if (normalized === 'image/heic') return 'heic';
  if (normalized === 'image/tiff') return 'tiff';
  return 'png';
}

// ---------------------------------------------------------------------------
// 附件辅助函数
// ---------------------------------------------------------------------------
/**
 * 将编辑器附件转换为请求附件（去除 UI 专有字段）。
 *
 * @param attachment - 编辑器附件对象
 * @returns 去除了 uploadStatus 和 uploadKey 的纯数据附件
 */
export function toRequestAttachment(attachment: ComposerAttachment): ChatAttachmentRead {
  const { uploadStatus: _uploadStatus, uploadKey: _uploadKey, ...rest } = attachment;
  return rest;
}

/**
 * 从消息元数据中提取附件列表。
 *
 * @param messageItem - 聊天消息
 * @returns 通过验证的附件数组
 */
export function messageAttachments(messageItem: ChatMessage): ChatAttachmentRead[] {
  const attachments = messageItem.metadata?.attachments;
  if (!Array.isArray(attachments)) return [];
  return attachments.filter(isChatAttachment);
}

/**
 * 类型守卫：判断值是否为有效的聊天附件对象。
 * 要求 id 和 filename 字段为字符串。
 *
 * @param value - 待检查的值
 * @returns 如果是有效附件则返回 true
 */
function isChatAttachment(value: unknown): value is ChatAttachmentRead {
  if (!value || typeof value !== 'object') return false;
  const item = value as Partial<ChatAttachmentRead>;
  return typeof item.id === 'string' && typeof item.filename === 'string';
}

/**
 * 生成附件的类型标签文本（如"PDF · 2.3 MB"）。
 *
 * @param attachment - 附件对象
 * @returns 类型和大小组合的标签字符串
 */
export function attachmentTypeLabel(attachment: ChatAttachmentRead): string {
  const size = formatAttachmentSize(attachment.size);
  const type = attachment.kind === 'pdf'
    ? 'PDF'
    : attachment.kind === 'image'
      ? '图片'
      : attachment.kind === 'text'
        ? '文本'
        : '文件';
  return `${type}${size ? ` · ${size}` : ''}`;
}

/**
 * 格式化附件大小为人类可读的文本。
 * 自动选择 B/KB/MB 单位，KB 以下保留 1 位小数。
 *
 * @param size - 文件大小（字节）
 * @returns 格式化后的大小文本，无效时返回空字符串
 */
function formatAttachmentSize(size: number): string {
  if (!Number.isFinite(size) || size <= 0) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(size < 10 * 1024 ? 1 : 0)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

// ---------------------------------------------------------------------------
// 定时任务草案调度辅助函数
// ---------------------------------------------------------------------------
/**
 * 将定时任务草案的调度信息格式化为人类可读的中文文本。
 * 根据调度类型（一次性/每天/每周/每月）生成不同的格式。
 *
 * @param draft - 定时任务草案
 * @returns 格式化后的调度描述（如"每周 周一、周三 09:00"）
 */
export function formatDraftSchedule(draft: ScheduledTaskDraftRead): string {
  const schedule = draft.schedule || {};
  const scheduleType = normalizeDraftScheduleType(draft.schedule_type);
  if (scheduleType === 'weekly') {
    const weekdays = Array.isArray(schedule.weekdays)
      ? schedule.weekdays.map((item) => DRAFT_WEEKDAY_LABELS[Number(item)]).filter(Boolean).join('、')
      : '周一';
    return `每周 ${weekdays} ${schedule.time || '09:00'}`;
  }
  if (scheduleType === 'monthly') {
    return `每月 ${schedule.day_of_month || 1} 号 ${schedule.time || '09:00'}`;
  }
  if (scheduleType === 'once') {
    const value = String(schedule.run_at || '');
    const formatted = formatClientDateTime(value, '');
    return formatted
      ? `一次性 ${formatted}`
      : '一次性';
  }
  return `每天 ${schedule.time || '09:00'}`;
}

/**
 * 获取定时任务调度类型的中文标签。
 *
 * @param type - 调度类型（once/daily/weekly/monthly）
 * @returns 对应的中文标签
 */
export function scheduleTypeLabel(type: ScheduledTaskDraftRead['schedule_type']): string {
  return DRAFT_SCHEDULE_TYPE_LABELS[normalizeDraftScheduleType(type)];
}

/**
 * 从定时任务草案中提取编辑器初始值。
 * 一次性任务返回 run_at；其他类型返回 time。
 *
 * @param draft - 定时任务草案
 * @returns 编辑器初始值字符串
 */
export function scheduleEditValue(draft: ScheduledTaskDraftRead): string {
  const schedule = draft.schedule || {};
  if (normalizeDraftScheduleType(draft.schedule_type) === 'once') return String(schedule.run_at || '');
  return String(schedule.time || '09:00');
}

/**
 * 根据编辑器输入值更新定时任务草案的调度配置。
 * 一次性任务写入 run_at；其他类型写入 time。
 *
 * @param draft - 定时任务草案
 * @param value - 编辑器输入值
 * @returns 更新后的调度配置对象
 */
export function scheduleFromEditValue(draft: ScheduledTaskDraftRead, value: string): Record<string, unknown> {
  if (normalizeDraftScheduleType(draft.schedule_type) === 'once') {
    return { ...(draft.schedule || {}), run_at: value };
  }
  return { ...(draft.schedule || {}), time: value };
}

/**
 * 根据调度类型生成新的调度配置对象。
 * 从现有配置中提取所需字段，按类型生成 once/weekly/monthly/daily 的调度结构。
 *
 * @param schedule - 现有调度配置
 * @param type - 目标调度类型
 * @returns 新的调度配置对象
 */
export function draftScheduleForType(schedule: Record<string, unknown>, type: DraftScheduleType): Record<string, unknown> {
  const time = String(schedule.time || '09:00');
  if (type === 'once') {
    return { run_at: String(schedule.run_at || '') };
  }
  if (type === 'weekly') {
    return {
      time,
      weekdays: Array.isArray(schedule.weekdays) ? schedule.weekdays : [0],
    };
  }
  if (type === 'monthly') {
    return {
      time,
      day_of_month: schedule.day_of_month || 1,
    };
  }
  return { time };
}

/**
 * 规范化调度类型字符串，非法值回退为 'daily'。
 *
 * @param value - 原始调度类型字符串
 * @returns 合法的调度类型
 */
export function normalizeDraftScheduleType(value: string): DraftScheduleType {
  const scheduleType = value as DraftScheduleType;
  return DRAFT_SCHEDULE_TYPES.has(scheduleType) ? scheduleType : 'daily';
}

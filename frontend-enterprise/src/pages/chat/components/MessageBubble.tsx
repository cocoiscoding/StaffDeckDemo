/**
 * @file MessageBubble.tsx
 * @module pages/chat/components/MessageBubble
 * @description
 * 单条消息气泡组件。
 *
 * 负责渲染单条聊天消息的完整内容，包括：
 * - 消息气泡容器（根据角色和错误状态应用不同样式）
 * - 执行记录面板（思维链追踪，通过 ExecutionRecord 组件渲染）
 * - 消息正文（AI 消息用 Markdown 渲染，用户消息用纯文本渲染）
 * - 附件列表（图片预览或文件图标）
 * - 知识引用标签（点击查看引用详情）
 * - 定时任务草案卡片（内嵌在 AI 回复中）
 * - 消息反馈按钮（点赞/点踩）
 * - 排队状态指示器（"排队中"标签）
 *
 * 渲染内容由 MessageList 组件预计算的 MessageRender 对象驱动，
 * 避免在此组件中重复计算复杂的追踪和引用逻辑。
 */

import StaffdeckIcon from '@/components/StaffdeckIcon';
import IconThumbUp from '@/assets/icons/thumb-up.svg?react';
import IconThumbDown from '@/assets/icons/thumb-down.svg?react';
import { cn } from '@/lib/utils';
import type {
  ChatAttachmentRead,
  ChatMessage,
  KnowledgeCitation,
  ScheduledTaskDraftRead,
  ScheduledTaskRead,
} from '@/types';

import {
  CHAT_ATTACHMENT_CARD_CLASS,
  CHAT_ATTACHMENT_COPY_CLASS,
  CHAT_ATTACHMENT_FILE_ICON_CLASS,
  CHAT_ATTACHMENT_IMG_CLASS,
  CHAT_ATTACHMENT_LIST_CLASS,
  CHAT_ATTACHMENT_META_CLASS,
  CHAT_ATTACHMENT_NAME_CLASS,
  CHAT_CITATION_CHIP_CLASS,
  CHAT_CITATION_HEADING_CLASS,
  CHAT_CITATION_INDEX_CLASS,
  CHAT_CITATION_LIST_CLASS,
  CHAT_CITATION_TITLE_CLASS,
  CHAT_CITATIONS_CLASS,
  CHAT_FEEDBACK_BTN_ACTIVE_CLASS,
  CHAT_FEEDBACK_BTN_CLASS,
  CHAT_FEEDBACK_BTN_DISLIKE_ACTIVE_CLASS,
  CHAT_FEEDBACK_CLASS,
  CHAT_MESSAGE_ITEM_CLASS,
  CHAT_MESSAGE_MODE_CHIP_CLASS,
  CHAT_PLAIN_ANSWER_CLASS,
  CHAT_QUEUED_MESSAGE_ITEM_CLASS,
  CHAT_QUEUED_STATUS_CLASS,
  CHAT_QUEUED_STATUS_ROW_CLASS,
  chatBubbleClass,
  chatRowClass,
} from '../chatPageStyles';
import {
  MarkdownMessage,
  attachmentTypeLabel,
  canRateMessage,
  citationDisplayTitle,
} from '../chatHelpers';
import type { TraceLine } from '../chatTypes';
import type { UseChatSession } from '../useChatSession';
import ExecutionRecord from './ExecutionRecord';
import ScheduledDraftCard from './ScheduledDraftCard';

/**
 * 消息渲染数据类型。
 * 由 MessageList 预计算，包含渲染单条消息所需的全部信息。
 */
export type MessageRender = {
  /** 追踪轮次 ID */
  traceTurnId: string;
  /** 执行记录摘要（文本和状态） */
  summary: { text: string; state: TraceLine['state'] } | null;
  /** 执行记录详细行 */
  details: TraceLine[];
  /** 是否展开执行记录 */
  expanded: boolean;
  /** 是否显示内联执行记录 */
  showInlineTrace: boolean;
  /** 可见消息内容 */
  visibleContent: string;
  /** 知识引用列表 */
  citations: KnowledgeCitation[];
  /** 定时任务草案（如有） */
  scheduledDraft: ScheduledTaskDraftRead | null;
  /** 已创建的定时任务（如有） */
  createdTask?: ScheduledTaskRead;
  /** 是否为定时任务提示消息 */
  scheduledTaskPrompt: boolean;
  /** 附件列表 */
  attachments: ChatAttachmentRead[];
  /** 是否仅显示状态（如"已停止生成"） */
  statusOnly: boolean;
};

/**
 * 消息气泡组件的属性类型。
 */
type MessageBubbleProps = {
  /** useChatSession Hook 的返回值 */
  chat: UseChatSession;
  /** 聊天消息数据 */
  item: ChatMessage;
  /** 预计算的渲染数据 */
  render: MessageRender;
};

/**
 * 单条消息气泡组件。
 * 根据 MessageRender 数据渲染消息的全部可视内容。
 *
 * @param chat - useChatSession Hook 的返回值
 * @param item - 聊天消息数据
 * @param render - 预计算的渲染数据
 * @returns 消息气泡 JSX
 */
export default function MessageBubble({ chat, item, render }: MessageBubbleProps) {
  const { toggleTrace, rateMessage, setActiveCitation, confirmScheduledTask, dismissScheduledTaskDraft } = chat;
  const {
    traceTurnId,
    summary,
    details,
    expanded,
    showInlineTrace,
    visibleContent,
    citations,
    scheduledDraft,
    createdTask,
    scheduledTaskPrompt,
    attachments,
    statusOnly,
  } = render;
  const queuedMessage = item.role === 'user' && item.metadata?.queued === true;

  return (
    <div className={cn(CHAT_MESSAGE_ITEM_CLASS, queuedMessage && CHAT_QUEUED_MESSAGE_ITEM_CLASS)}>
      <div className={chatRowClass(item.role)}>
        <div
          className={chatBubbleClass(item.role, item.isError)}
        >
          {statusOnly ? (
            <div className="text-[13px] text-[#858b9c]">{visibleContent}</div>
          ) : showInlineTrace && summary ? (
            <ExecutionRecord
              traceTurnId={traceTurnId}
              summary={summary}
              details={details}
              expanded={expanded}
              onToggle={toggleTrace}
            />
          ) : null}

          {!statusOnly && visibleContent ? (
            item.role === 'assistant' ? (
              <div data-i18n-ignore>
                <MarkdownMessage content={visibleContent} />
              </div>
            ) : (
              <div className={CHAT_PLAIN_ANSWER_CLASS}>
                {scheduledTaskPrompt && (
                  <span className={CHAT_MESSAGE_MODE_CHIP_CLASS}>
                    <StaffdeckIcon name="clock" size={13} />
                    定时任务
                  </span>
                )}
                <span data-i18n-ignore>{visibleContent}</span>
              </div>
            )
          ) : null}

          {!statusOnly && attachments.length > 0 && (
            <div className={CHAT_ATTACHMENT_LIST_CLASS}>
              {attachments.map((attachment) => (
                <div className={CHAT_ATTACHMENT_CARD_CLASS} key={attachment.id}>
                  {attachment.kind === 'image' && attachment.data_url ? (
                    <img className={CHAT_ATTACHMENT_IMG_CLASS} src={attachment.data_url} alt={attachment.filename} />
                  ) : (
                    <span className={CHAT_ATTACHMENT_FILE_ICON_CLASS}>
                      <StaffdeckIcon name={attachment.kind === 'pdf' ? 'file' : 'folder'} size={18} />
                    </span>
                  )}
                  <span className={CHAT_ATTACHMENT_COPY_CLASS}>
                    <span className={CHAT_ATTACHMENT_NAME_CLASS} data-i18n-ignore>{attachment.filename}</span>
                    <span className={CHAT_ATTACHMENT_META_CLASS} data-i18n-ignore>
                      {attachmentTypeLabel(attachment)}
                      {attachment.error ? ` · ${attachment.error}` : ''}
                    </span>
                  </span>
                </div>
              ))}
            </div>
          )}

          {item.role === 'assistant' && citations.length > 0 && (
            <div className={CHAT_CITATIONS_CLASS} aria-label="知识引用">
              <div className={CHAT_CITATION_HEADING_CLASS}>
                <StaffdeckIcon name="file" size={14} />
                <span>知识来源</span>
              </div>
              <div className={CHAT_CITATION_LIST_CLASS}>
                {citations.map((citation) => (
                  <button
                    key={citation.id}
                    type="button"
                    className={CHAT_CITATION_CHIP_CLASS}
                    onClick={() => setActiveCitation(citation)}
                  >
                    <span className={CHAT_CITATION_INDEX_CLASS} data-i18n-ignore>{citation.label || citation.id}</span>
                    <span className={CHAT_CITATION_TITLE_CLASS} data-i18n-ignore>{citationDisplayTitle(citation)}</span>
                  </button>
                ))}
              </div>
            </div>
          )}

          {scheduledDraft && (
            <ScheduledDraftCard
              draft={scheduledDraft}
              createdTask={createdTask}
              onConfirm={(nextDraft) => void confirmScheduledTask(nextDraft, item.id)}
              onDismiss={() => dismissScheduledTaskDraft(item.id)}
            />
          )}

          {canRateMessage(item) && (
            <div className={CHAT_FEEDBACK_CLASS}>
              <button
                type="button"
                className={cn(CHAT_FEEDBACK_BTN_CLASS, item.feedback_rating === 'up' && CHAT_FEEDBACK_BTN_ACTIVE_CLASS)}
                aria-label="点赞"
                onClick={() => rateMessage(item, 'up')}
              >
                <IconThumbUp width={15} height={15} />
              </button>
              <button
                type="button"
                className={cn(
                  CHAT_FEEDBACK_BTN_CLASS,
                  item.feedback_rating === 'down' && CHAT_FEEDBACK_BTN_DISLIKE_ACTIVE_CLASS,
                )}
                aria-label="点踩"
                onClick={() => rateMessage(item, 'down')}
              >
                <IconThumbDown width={15} height={15} />
              </button>
            </div>
          )}
        </div>
      </div>
      {queuedMessage && (
        <div className={CHAT_QUEUED_STATUS_ROW_CLASS}>
          <span className={CHAT_QUEUED_STATUS_CLASS} role="status">
            <StaffdeckIcon name="clock" size={12} />
            排队中
          </span>
        </div>
      )}
    </div>
  );
}

/**
 * @file MessageList.tsx
 * @module pages/chat/components/MessageList
 * @description
 * 聊天消息列表组件。
 *
 * 负责将 useChatSession 提供的消息数据渲染为可视化的消息气泡列表。
 * 核心职责包括：
 * - 将排队消息排到列表末尾（placeQueuedMessagesLast）
 * - 为每条消息计算思维链追踪信息（trace 匹配、展开/折叠状态）
 * - 处理流式消息的追踪恢复逻辑
 * - 提取知识引用、定时任务草案、附件等元数据
 * - 过滤掉无内容的空消息
 * - 渲染空状态组件（无消息时）
 * - 渲染独立的定时任务草案卡片（不在消息流中的草案）
 * - 调试面板（SHOW_DEBUG 模式下显示 session_state JSON）
 */

import {
  CHAT_DEBUG_PANEL_CLASS,
  CHAT_MESSAGES_CLASS,
  CHAT_MESSAGE_STACK_CLASS,
} from '../chatPageStyles';
import {
  CHAT_TRACE_RECOVERY_WINDOW_MS,
  createdScheduledTaskForMessage,
  isScheduledTaskPrompt,
  knowledgeCitations,
  messageAttachments,
  normalizeMessageText,
  placeQueuedMessagesLast,
  scheduledDraftForMessage,
  stripTrailingCitationSummary,
  traceDetails,
  traceLineAllowed,
  traceSummary,
} from '../chatHelpers';
import { staffdeckDisplayText } from '@/employee';
import type { UseChatSession } from '../useChatSession';
import ChatEmptyState from './ChatEmptyState';
import MessageBubble, { type MessageRender } from './MessageBubble';
import ScheduledDraftCard from './ScheduledDraftCard';

/**
 * 聊天消息列表组件。
 * 遍历消息数据，为每条消息计算渲染所需的全部信息并委托 MessageBubble 渲染。
 *
 * @param chat - useChatSession Hook 的返回值
 * @returns 消息列表 JSX
 */
export default function MessageList({ chat }: { chat: UseChatSession }) {
  const {
    displayedMessages,
    turnTraceRef,
    uiConfig,
    currentStream,
    runningTurn,
    expandedTraceIds,
    collapsedTraceIds,
    isCurrentStreamingTrace,
    dismissedDraftMessageIds,
    createdScheduledTasks,
    activeConversationId,
    currentScheduledDraft,
    hasVisibleMessageScheduledDraft,
    confirmScheduledTask,
    dismissScheduledTaskDraft,
    chatMessagesRef,
    handleChatMessagesScroll,
    SHOW_DEBUG,
    lastTurn,
  } = chat;
  const renderMessages = placeQueuedMessagesLast(displayedMessages);

  return (
    <div className={CHAT_MESSAGES_CLASS} ref={chatMessagesRef} onScroll={handleChatMessagesScroll}>
      {renderMessages.length === 0 && <ChatEmptyState chat={chat} />}

      <div className={CHAT_MESSAGE_STACK_CLASS}>
        {renderMessages.map((item, itemIndex) => {
          const turnId = item.turnId || item.id;
          const fallbackTraceId = item.role === 'assistant' && item.isStreaming
            ? (currentStream.turnId || runningTurn?.turnId || '')
            : '';
          const primaryTrace = item.role === 'assistant' ? turnTraceRef.current.get(turnId) : undefined;
          const fallbackTrace = fallbackTraceId ? turnTraceRef.current.get(fallbackTraceId) : undefined;
          const trace = primaryTrace || fallbackTrace;
          const traceTurnId = primaryTrace ? turnId : (fallbackTrace ? fallbackTraceId : turnId);
          const traceLines = trace?.lines || [];
          const allowedTrace = traceLines.filter((line) => traceLineAllowed(line, uiConfig));
          const forceRunningTrace = Boolean(
            item.role === 'assistant'
            && item.isStreaming
            && allowedTrace.length === 0
            && traceLines.some((line) => line.state === 'running'),
          );
          const visibleTrace = forceRunningTrace ? traceLines : allowedTrace;
          const summary = trace && visibleTrace.length > 0 ? traceSummary(trace, visibleTrace) : null;
          const details = traceDetails(visibleTrace);
          const traceActive = isCurrentStreamingTrace(traceTurnId, item);
          const summaryForRender = summary && traceActive && !trace?.completedAt
            ? { ...summary, state: 'running' as const }
            : summary;
          const traceOnlyMessage = Boolean(
            item.role === 'assistant' && !normalizeMessageText(item.content) && details.length > 0,
          );
          const latestAssistantTrace = Boolean(
            item.role === 'assistant'
            && details.length > 0
            && !renderMessages.slice(itemIndex + 1).some((later) => (
              later.role === 'assistant'
              && Boolean(turnTraceRef.current.get(later.turnId || later.id)?.lines.length)
            )),
          );
          const recentlyStartedTrace = Boolean(
            trace?.startedAt && Date.now() - trace.startedAt <= CHAT_TRACE_RECOVERY_WINDOW_MS,
          );
          const defaultExpanded = Boolean(
            traceActive
            || summaryForRender?.state === 'running'
            || traceOnlyMessage
            || (latestAssistantTrace && recentlyStartedTrace),
          );
          const manuallyCollapsed = collapsedTraceIds.includes(traceTurnId);
          const expanded = Boolean(
            !manuallyCollapsed
            && (expandedTraceIds.includes(traceTurnId) || defaultExpanded),
          );
          const rawVisibleContent = staffdeckDisplayText(
            item.role === 'assistant' ? stripTrailingCitationSummary(item.content) : item.content,
          );
          const visibleContent = normalizeMessageText(rawVisibleContent) ? rawVisibleContent : '';
          const citations = item.role === 'assistant' ? knowledgeCitations(item, visibleContent) : [];
          const scheduledTaskPrompt = isScheduledTaskPrompt(item);
          const scheduledDraft = item.role === 'assistant' && !dismissedDraftMessageIds.includes(item.id)
            ? scheduledDraftForMessage(item)
            : null;
          const persistedCreatedTask = item.role === 'assistant'
            ? createdScheduledTaskForMessage(item)
            : undefined;
          const stoppedStatusOnly = Boolean(
            item.role === 'system'
            && item.id.startsWith('local_interrupt_')
            && visibleContent === '已停止生成',
          );
          const attachments = messageAttachments(item);
          const statusOnly = stoppedStatusOnly;
          const showInlineTrace = Boolean(summaryForRender && !stoppedStatusOnly);

          if (
            item.role === 'assistant'
            && !visibleContent
            && !showInlineTrace
            && !statusOnly
            && !scheduledDraft
            && !persistedCreatedTask
            && citations.length === 0
            && attachments.length === 0
          ) {
            return null;
          }

          const render: MessageRender = {
            traceTurnId,
            summary: summaryForRender,
            details,
            expanded,
            showInlineTrace,
            visibleContent,
            citations,
            scheduledDraft,
            createdTask: createdScheduledTasks[item.id] || persistedCreatedTask,
            scheduledTaskPrompt,
            attachments,
            statusOnly,
          };

          return <MessageBubble key={`${item.id}:message`} chat={chat} item={item} render={render} />;
        })}
      </div>

      {currentScheduledDraft && !hasVisibleMessageScheduledDraft && (
        <div className={CHAT_MESSAGE_STACK_CLASS}>
          <ScheduledDraftCard
            draft={currentScheduledDraft}
            createdTask={activeConversationId ? createdScheduledTasks[`session:${activeConversationId}`] : undefined}
            onConfirm={(nextDraft) => void confirmScheduledTask(nextDraft)}
            onDismiss={() => dismissScheduledTaskDraft()}
          />
        </div>
      )}

      {SHOW_DEBUG && lastTurn && (
        <pre className={CHAT_DEBUG_PANEL_CLASS}>{JSON.stringify(lastTurn.session_state, null, 2)}</pre>
      )}
    </div>
  );
}

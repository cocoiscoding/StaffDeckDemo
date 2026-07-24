/**
 * @file ChatPage.tsx
 * @module pages/chat/ChatPage
 * @description
 * 聊天主页面组件。
 *
 * 这是聊天功能的入口页面，组合了以下子组件：
 * - AppSidebar：会话列表侧边栏（可折叠）
 * - ChatHeader：顶部标题栏（含会话名称、重命名、用户菜单）
 * - MessageList：消息列表区域（含空状态展示）
 * - Composer：底部输入框（含附件上传、模型选择）
 * - ChatDialogs：各种弹窗（重命名、删除、引用详情、人工接续等）
 *
 * 所有子组件共享同一个 useChatSession Hook 实例作为数据源。
 */

import { type CSSProperties } from 'react';

import AppSidebar from '@/components/AppSidebar';
import { SidebarProvider } from '@/components/ui/sidebar';
import { cn } from '@/lib/utils';

import { CHAT_MAIN_CLASS } from './chatPageStyles';
import { sessionHasUnreadReply } from './chatHelpers';
import { useChatSession } from './useChatSession';
import ChatHeader from './components/ChatHeader';
import MessageList from './components/MessageList';
import Composer from './components/Composer';
import ChatDialogs from './components/ChatDialogs';

/**
 * 聊天主页面组件。
 * 初始化 useChatSession Hook 并将状态分发给各子组件。
 *
 * @returns 聊天页面 JSX
 */
export default function ChatPage() {
  const chat = useChatSession();

  return (
    <SidebarProvider
      open={!chat.sidebarCollapsed}
      onOpenChange={(open) => {
        if (open === chat.sidebarCollapsed) chat.toggleSidebar();
      }}
      style={
        {
          '--sidebar-width': '220px',
          '--sidebar-width-icon': '72px',
        } as CSSProperties
      }
      className="h-screen min-h-0 bg-[#fcfcfc] text-[#18181a]"
    >
      <AppSidebar
        variant="chat"
        sessions={chat.visibleSidebarSessions}
        sessionsLoading={chat.sessionsLoading}
        agents={chat.agents}
        activeSessionId={chat.sessionId}
        sessionFilter={chat.sessionAgentFilter}
        onSessionFilterChange={chat.setSessionAgentFilter}
        sessionFilterOptions={chat.sessionFilterOptions}
        isSessionUnread={(session) => sessionHasUnreadReply(session, chat.sessionReadTimes, chat.sessionId)}
        onOpenSession={chat.openSession}
        onOpenGallery={chat.openGallery}
        handoffCount={chat.handoffs.length}
        onOpenHandoffs={chat.openHandoffInbox}
        onRenameSession={chat.openRename}
        onDeleteSession={chat.requestDelete}
        onOpenAdmin={chat.openAdmin}
      />
      <main className={cn(CHAT_MAIN_CLASS, 'flex-1')}>
        <ChatHeader chat={chat} />
        <MessageList chat={chat} />
        <Composer chat={chat} />
      </main>
      <ChatDialogs chat={chat} />
    </SidebarProvider>
  );
}

/**
 * @file ChatGalleryPage.tsx
 * @module pages/chat/ChatGalleryPage
 * @description
 * 数字员工展示页面（聊天入口画廊）。
 *
 * 此页面展示所有可用的数字员工（Agent），用户可以浏览员工信息并选择
 * 与某个员工发起聊天。选择员工后会调用后端 "use" 接口激活该员工，
 * 然后导航到草稿会话页面。
 *
 * 页面布局与 ChatPage 类似（共享侧边栏和弹窗），但主区域展示的是
 * EmployeeGalleryPage 组件而非消息列表。
 */

import { type CSSProperties } from 'react';

import { api, TENANT_ID } from '@/api/client';
import AppSidebar from '@/components/AppSidebar';
import { notify } from '@/components/ui/app-toast';
import { SidebarProvider } from '@/components/ui/sidebar';
import { getEnterpriseAuthSession, isEnterpriseAdmin } from '@/auth';
import type { AgentProfileRead } from '@/types';

import EmployeeGalleryPage from '../EmployeeGalleryPage';
import { sessionHasUnreadReply } from './chatHelpers';
import { useChatSession } from './useChatSession';
import ChatDialogs from './components/ChatDialogs';

/**
 * 数字员工画廊聊天页面组件。
 *
 * @returns 包含侧边栏和员工画廊的页面 JSX
 */
export default function ChatGalleryPage() {
  const chat = useChatSession();
  const auth = getEnterpriseAuthSession();
  const isAdmin = isEnterpriseAdmin(auth?.user);

  /**
   * 启动与指定数字员工的聊天会话。
   * 1. 调用后端 "use" 接口激活员工
   * 2. 刷新员工列表
   * 3. 设置会话过滤器为该员工
   * 4. 导航到草稿会话页面
   *
   * @param agent - 选中的数字员工
   */
  async function startGalleryChat(agent: AgentProfileRead) {
    try {
      await api.post<AgentProfileRead>(`/api/chat/agents/${agent.id}/use?tenant_id=${TENANT_ID}`, {});
      await chat.refreshAgents(agent.id);
      chat.setSessionAgentFilter(agent.id);
      chat.openDraftForAgent(agent.id);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '无法打开数字员工');
    }
  }

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
        galleryActive
        handoffCount={chat.handoffs.length}
        onOpenHandoffs={chat.openHandoffInbox}
        onRenameSession={chat.openRename}
        onDeleteSession={chat.requestDelete}
        onOpenAdmin={chat.openAdmin}
      />
      <main className="min-h-0 flex-1 overflow-y-auto">
        <EmployeeGalleryPage
          currentUser={auth?.user}
          isAdmin={isAdmin}
          onStartChat={startGalleryChat}
          onLogout={chat.logout}
        />
      </main>
      <ChatDialogs chat={chat} />
    </SidebarProvider>
  );
}

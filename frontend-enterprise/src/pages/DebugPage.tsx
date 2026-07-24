/**
 * @file pages/DebugPage.tsx
 * @description Agent 对话调试页面。
 *
 * 提供一个轻量的对话界面，用于直接与企业端 Agent 进行交互调试。
 * 左侧为聊天面板（消息列表 + 输入框），右侧为 Trace 快照面板，
 * 以折叠面板形式展示路由决策、Step Agent、工具调用结果和会话状态。
 *
 * 所有调试请求通过 /api/chat/turn 接口发送（debug=true），
 * 返回的 ChatTurnResponse 包含完整的中间状态供开发者分析。
 */

import { SendOutlined } from '../icons';
import { useState } from 'react';
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  notify,
} from '@/components/ui';
import { api, TENANT_ID } from '../api/client';
import type { ChatTurnResponse } from '../types';

/** 聊天消息结构，role 区分用户消息与助手回复 */
type ChatMessage = { role: 'user' | 'assistant'; content: string };

/**
 * Agent 调试页面主组件。
 * 管理会话 ID、消息列表、输入内容和最近一次的 Trace 快照。
 * @returns 渲染好的调试页面
 */
export default function DebugPage() {
  // 当前会话 ID，首次发送后由后端返回
  const [sessionId, setSessionId] = useState('');
  // 输入框当前内容
  const [input, setInput] = useState('');
  // 聊天消息列表
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  // 最近一次对话轮次的完整响应（含 Trace）
  const [lastTurn, setLastTurn] = useState<ChatTurnResponse | null>(null);
  // 是否正在等待回复
  const [loading, setLoading] = useState(false);

  /**
   * 发送调试消息：将用户输入提交到后端 chat 接口，
   * 收到回复后追加到消息列表并保存 Trace 快照。
   */
  async function send() {
    if (!input.trim()) return;
    const userText = input;
    setInput('');
    // 先将用户消息追加到列表，提供即时反馈
    setMessages((items) => [...items, { role: 'user', content: userText }]);
    setLoading(true);
    try {
      const result = await api.post<ChatTurnResponse>('/api/chat/turn', {
        tenant_id: TENANT_ID,
        // 首次发送不传 session_id，由后端创建新会话
        session_id: sessionId || undefined,
        user_id: 'enterprise_debugger',
        message: userText,
        channel: 'enterprise_debug',
        debug: true,
      });
      // 保存后端返回的会话 ID，后续消息复用同一会话
      setSessionId(result.session_id);
      setLastTurn(result);
      setMessages((items) => [...items, { role: 'assistant', content: result.reply }]);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '发送失败');
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      {/* 页面标题栏：标题 + 会话 ID 输入框 */}
      <div className="page-title">
        <h3>Agent 调试</h3>
        <Input
          className="page-field w-[240px]"
          value={sessionId}
          onChange={(event) => setSessionId(event.target.value)}
          placeholder="Session ID"
        />
      </div>
      <div className="grid-2">
        {/* 左侧：聊天面板 */}
        <Card>
          <CardContent>
            <div className="chat-panel">
              {/* 消息列表 */}
              <div className="messages">
                {messages.map((item, index) => (
                  <div key={`${item.role}-${index}`} className={`message-row ${item.role}`}>
                    <div className="bubble">{item.content}</div>
                  </div>
                ))}
              </div>
              {/* 输入区域：回车发送（排除输入法组合状态） */}
              <div className="flex gap-[8px]">
                <Input
                  value={input}
                  onChange={(event) => setInput(event.target.value)}
                  onKeyDown={(event) => {
                    // isComposing 防止输入法选词时误触发发送
                    if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
                      event.preventDefault();
                      void send();
                    }
                  }}
                  placeholder="输入调试消息"
                />
                <Button disabled={loading} onClick={() => void send()}>
                  <SendOutlined />
                  发送
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
        {/* 右侧：Trace 快照面板，以折叠面板展示各阶段中间状态 */}
        <Card>
          <CardHeader>
            <CardTitle>Trace Snapshot</CardTitle>
          </CardHeader>
          <CardContent>
            <Accordion type="multiple" defaultValue={['router', 'session']}>
              {/* 路由决策：Router 如何分发当前消息 */}
              <AccordionItem value="router">
                <AccordionTrigger>Router Decision</AccordionTrigger>
                <AccordionContent><pre>{JSON.stringify(lastTurn?.router_decision, null, 2)}</pre></AccordionContent>
              </AccordionItem>
              {/* Step Agent：Agent Loop 的执行结果 */}
              <AccordionItem value="step">
                <AccordionTrigger>Step Agent</AccordionTrigger>
                <AccordionContent><pre>{JSON.stringify(lastTurn?.step_result, null, 2)}</pre></AccordionContent>
              </AccordionItem>
              {/* 工具调用结果 */}
              <AccordionItem value="tool">
                <AccordionTrigger>Tool Result</AccordionTrigger>
                <AccordionContent><pre>{JSON.stringify(lastTurn?.tool_result, null, 2)}</pre></AccordionContent>
              </AccordionItem>
              {/* 会话状态：当前对话的上下文快照 */}
              <AccordionItem value="session">
                <AccordionTrigger>Session State</AccordionTrigger>
                <AccordionContent><pre>{JSON.stringify(lastTurn?.session_state, null, 2)}</pre></AccordionContent>
              </AccordionItem>
            </Accordion>
          </CardContent>
        </Card>
      </div>
    </>
  );
}

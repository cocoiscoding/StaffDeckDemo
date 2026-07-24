/**
 * @file scheduled-tasks/StatusBadge.tsx
 * @description 定时任务模块的状态徽章组件集合。
 *
 * 提供三种徽章：
 * - StatusBadge：通用基础徽章，接收色调与子节点
 * - TaskStatusBadge：根据任务状态（active/paused/completed/archived）自动渲染对应徽章
 * - TaskRunResultBadge：根据执行记录状态（succeeded/failed/running/skipped）自动渲染对应徽章
 *
 * 配色与文案映射定义在 shared.ts 的 TASK_STATUS_BADGE / RUN_STATUS_BADGE 中。
 */

import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import { BADGE_TONE_CLASS, RUN_STATUS_BADGE, TASK_STATUS_BADGE, type BadgeTone } from './shared';

interface StatusBadgeProps {
  /** 徽章色调，决定背景色与文字色 */
  tone: BadgeTone;
  /** 徽章文本内容 */
  children: ReactNode;
}

/**
 * 通用状态徽章基础组件。
 * 渲染为圆角胶囊形状的小标签，颜色由 tone 决定。
 * @param props.tone 色调（blue/orange/green/red/gray）
 * @param props.children 徽章内容
 * @returns 渲染好的 <span> 元素
 */
export function StatusBadge({ tone, children }: StatusBadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-[12px] py-[4px] text-[10px] leading-none whitespace-nowrap capitalize',
        BADGE_TONE_CLASS[tone],
      )}
    >
      {children}
    </span>
  );
}

interface TaskStatusBadgeProps {
  /** 任务状态原始值，如 "active"、"paused" */
  status: string;
}

/**
 * 任务状态徽章：根据任务状态值自动匹配色调与文案。
 * 未知状态回退为"已删除"（archived）样式。
 * @param props.status 任务状态字符串
 * @returns 对应的 StatusBadge
 */
export function TaskStatusBadge({ status }: TaskStatusBadgeProps) {
  const { tone, text } = TASK_STATUS_BADGE[status] || TASK_STATUS_BADGE.archived;
  return <StatusBadge tone={tone}>{text}</StatusBadge>;
}

interface TaskRunResultBadgeProps {
  /** 执行记录状态原始值，如 "succeeded"、"failed" */
  status: string;
}

/**
 * 执行记录结果徽章：根据执行状态值自动匹配色调与文案。
 * 未知状态回退为灰色并直接显示原始状态值。
 * @param props.status 执行记录状态字符串
 * @returns 对应的 StatusBadge
 */
export function TaskRunResultBadge({ status }: TaskRunResultBadgeProps) {
  const preset = RUN_STATUS_BADGE[status] || { tone: 'gray' as BadgeTone, text: status || '暂无' };
  return <StatusBadge tone={preset.tone}>{preset.text}</StatusBadge>;
}

/**
 * @file DetailField.tsx
 * @module components/DetailField
 * @description 详情字段组件。在详情弹窗（员工记忆/对话日志/SOP版本详情等）中
 *              展示带标签的只读字段，呈现为带边框的浅色卡片。
 */

import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

/**
 * DetailField 组件的属性定义。
 */
export type DetailFieldProps = {
  /** 字段标签文字。 */
  label: string;
  /** 字段值内容。 */
  children: ReactNode;
  /** 附加 CSS 类名。 */
  className?: string;
};

/**
 * 详情字段组件。在详情弹窗中展示带标签的只读字段。
 * 呈现为带边框的浅色卡片，包含 11px 标签和 12px 值内容。
 *
 * @param props - 组件属性
 * @param props.label - 字段标签
 * @param props.children - 字段值
 * @param props.className - 附加类名
 * @returns 渲染好的详情字段元素
 */
export function DetailField({ label, children, className }: DetailFieldProps) {
  return (
    <div
      className={cn(
        'flex min-w-0 flex-col gap-[6px] rounded-[10px] border border-[#eef0f4] bg-[#fafbfc] px-[12px] py-[10px]',
        className,
      )}
    >
      <span className="text-[11px] font-semibold text-[#858b9c]">{label}</span>
      <div className="min-w-0 wrap-break-word text-[12px] text-[#18181a]">{children}</div>
    </div>
  );
}

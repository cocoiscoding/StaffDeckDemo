/**
 * @file StatCard.tsx
 * @module components/StatCard
 * @description 统计指标卡片组件。在企业端各页面（定时任务/对话日志/技能等）展示
 *              关键指标数据，支持默认灰色、绿色、红色三种色调主题。
 */

import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

/** 卡片色调类型 */
export type StatCardTone = 'default' | 'green' | 'red';

/** 卡片表面背景色映射 */
const SURFACE_CLASS: Record<StatCardTone, string> = {
  default: 'bg-[#f6f6f6]',
  green: 'bg-[#e9f7ef]',
  red: 'bg-[#fce7e7]',
};
/** 数值文字颜色映射 */
const VALUE_CLASS: Record<StatCardTone, string> = {
  default: 'text-[#18181a]',
  green: 'text-[#2cb360]',
  red: 'text-[#d20b0b]',
};
/** 标签文字颜色映射 */
const LABEL_CLASS: Record<StatCardTone, string> = {
  default: 'text-[#464c5e]',
  green: 'text-[#2cb360]',
  red: 'text-[#d20b0b]',
};

/**
 * StatCard 组件的属性定义。
 */
export type StatCardProps = {
  /** 大号数值内容。 */
  value: ReactNode;
  /** 数值后的标签文字。 */
  label: ReactNode;
  /** 色调主题。default 为中性灰色卡片，green/red 为着色卡片。 */
  tone?: StatCardTone;
  /** 数值的附加 CSS 类名（如自定义颜色）。 */
  valueClassName?: string;
  /** 外层卡片的附加 CSS 类名（如覆盖 flex 基准）。 */
  className?: string;
};

/**
 * 统计指标卡片组件。在企业端各页面展示关键指标数据。
 * 呈现为带色调背景的圆角卡片，内含大号数值和尾部标签。
 *
 * @param props - 组件属性
 * @param props.value - 数值内容
 * @param props.label - 标签文字
 * @param props.tone - 色调主题，默认 default
 * @param props.valueClassName - 数值附加类名
 * @param props.className - 外层卡片附加类名
 * @returns 渲染好的统计卡片元素
 */
export function StatCard({ value, label, tone = 'default', valueClassName, className }: StatCardProps) {
  return (
    <div
      className={cn(
        'flex h-[70px] flex-1 basis-[180px] items-center rounded-[14px] px-[24px] py-[8px]',
        SURFACE_CLASS[tone],
        className,
      )}
    >
      <div className="flex min-w-0 items-end gap-[6px]">
        <span className={cn('shrink-0 text-[26px] font-semibold leading-none', VALUE_CLASS[tone], valueClassName)}>
          {value}
        </span>
        <span className={cn('truncate text-[14px] leading-none', LABEL_CLASS[tone])}>{label}</span>
      </div>
    </div>
  );
}

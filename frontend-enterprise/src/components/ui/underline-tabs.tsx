/**
 * @file underline-tabs.tsx
 * @description 下划线选项卡组件，项目自定义的标签页实现（非 Radix UI 封装）。
 *              提供两种视觉变体：
 *              - dot：激活标签下方显示居中的短圆角指示条（对齐 SD1 设计规范节点 38:6404）。
 *              - line：全宽底部分隔线 + 激活标签下方的全宽 2px 激活条（对齐 SD1 设计规范节点 281:1935）。
 *              本项目中用于页面顶部导航、内容区域切换等场景。
 */

import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

/** 下划线选项卡项配置 */
export type UnderlineTabItem<T extends string = string> = {
  /** 选项卡唯一标识值 */
  value: T;
  /** 选项卡显示文本（支持 ReactNode） */
  label: ReactNode;
  /** 是否禁用该选项卡 */
  disabled?: boolean;
};

/**
 * 选项卡视觉变体类型：
 * - `dot`：激活标签下方居中的短圆角指示条（SD1 节点 38:6404）。
 * - `line`：全宽底部分隔线 + 全宽 2px 激活条（SD1 节点 281:1935）。
 */
export type UnderlineTabsVariant = 'dot' | 'line';

/** 下划线选项卡组件属性 */
export type UnderlineTabsProps<T extends string = string> = {
  /** 选项卡项列表 */
  items: UnderlineTabItem<T>[];
  /** 当前激活的选项卡值 */
  value: T;
  /** 选项卡切换回调 */
  onChange: (value: T) => void;
  /** 视觉变体，默认为 "dot" */
  variant?: UnderlineTabsVariant;
  /** 容器额外类名 */
  className?: string;
  /** 每个选项卡按钮的额外类名（如覆盖默认固定宽度） */
  tabClassName?: string;
  /** 无障碍标签 */
  'aria-label'?: string;
};

/**
 * 全局下划线选项卡组件。
 * 使用 `variant="dot"`（默认）显示短圆角指示条，或使用 `variant="line"` 显示全宽分隔线和激活条。
 * @param items - 选项卡项配置列表。
 * @param value - 当前激活值。
 * @param onChange - 切换回调函数。
 * @param variant - 视觉变体，"dot"（默认）或 "line"。
 * @param className - 容器额外类名。
 * @param tabClassName - 每个选项卡按钮的额外类名。
 * @param ariaLabel - 无障碍 aria-label 属性。
 * @returns 渲染一个带下划线指示器的选项卡导航栏。
 */
export function UnderlineTabs<T extends string = string>({
  items,
  value,
  onChange,
  variant = 'dot',
  className,
  tabClassName,
  'aria-label': ariaLabel,
}: UnderlineTabsProps<T>) {
  const isLine = variant === 'line';
  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      className={cn(
        'flex items-start',
        // line 变体添加底部分隔线
        isLine && 'border-b-[0.5px] border-[#e3e7f1]',
        className,
      )}
    >
      {items.map((item) => {
        const active = item.value === value;
        return (
          <button
            key={item.value}
            type="button"
            role="tab"
            aria-selected={active}
            disabled={item.disabled}
            onClick={() => onChange(item.value)}
            className={cn(
              'relative flex w-[120px] items-start justify-center px-[16px] text-[14px] capitalize transition-colors outline-none',
              // line 变体和 dot 变体的内边距差异
              isLine ? 'pt-[6px] pb-[8px] mb-[-0.5px] border-b-2' : 'py-[6px]',
              // 根据变体和激活状态设置不同的文字颜色和边框
              isLine
                ? active
                  ? 'border-[#18181a] font-medium text-[#18181a]'
                  : 'border-transparent font-normal text-[#4f5669] hover:text-[#18181a]'
                : active
                  ? 'font-medium text-[#18181A]'
                  : 'font-normal text-[#858B9C] hover:text-[#18181A]',
              'disabled:cursor-not-allowed disabled:opacity-50',
              tabClassName,
            )}
          >
            {item.label}
            {/* dot 变体激活时显示居中的短圆角指示条 */}
            {!isLine && active && (
              <span
                aria-hidden="true"
                className="absolute top-[33px] left-1/2 h-[3px] w-[10px] -translate-x-1/2 rounded-[4px] bg-[#18181A] max-[560px]:top-auto max-[560px]:bottom-0"
              />
            )}
          </button>
        );
      })}
    </div>
  );
}

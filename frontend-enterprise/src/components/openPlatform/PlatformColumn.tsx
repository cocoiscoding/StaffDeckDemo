/**
 * @file 开放广场列布局组件
 * @description
 * 开放广场（数字员工 / 知识库 / 技能 / SOP / 工具）五个模块共享的列布局外壳。
 * 它封装了所有模块都需要重复的视觉结构：图标+标题的头部（含计数）、筛选标签行、
 * 分割线、卡片列表（或空状态占位）、以及底部的"查看全部"按钮。
 * 各模块只需通过 `children` 注入自己的卡片内容即可，无需重复编写布局骨架。
 */

import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import IconChevronDown from '../../assets/icons/chevron-down.svg?react';

export type PlatformColumnProps = {
  /** 标题前方显示的 14px 小图标。 */
  icon: ReactNode;
  /** 列标题，例如"数字员工广场"。 */
  title: ReactNode;
  /** 头部右侧展示的数量统计值。 */
  count: number;
  /** 计数后方展示的单位标签，例如"员工 / 内容"。 */
  countLabel: string;
  /** 标题下方渲染的筛选标签数组。 */
  filters?: string[];
  /** 数据加载中时为 true，渲染骨架占位而非真实内容。 */
  loading?: boolean;
  /** 列为空时为 true，展示空状态占位。 */
  isEmpty?: boolean;
  /** 空状态占位的提示文案。 */
  emptyText?: string;
  /** 点击"查看全部"按钮时的回调。 */
  onViewAll?: () => void;
  /** 列内的卡片内容，由各模块自行提供。 */
  children?: ReactNode;
  className?: string;
};

/**
 * 开放广场列布局外壳组件。
 *
 * 该组件抽取了五个模块（数字员工 / 知识库 / 技能 / SOP / 工具）共有的列结构：
 * 顶部图标+标题行（带计数）、筛选标签行、分割线、可滚动的卡片列表（或空占位），
 * 以及底部的"查看全部"按钮。各模块仅通过 `children` 提供自身卡片即可复用整套骨架。
 *
 * @param props - 组件属性，参见 {@link PlatformColumnProps}
 * @returns 列布局的 JSX 元素
 */
export default function PlatformColumn({
  icon,
  title,
  count,
  countLabel,
  filters,
  loading = false,
  isEmpty = false,
  emptyText = '暂无开放内容',
  onViewAll,
  children,
  className,
}: PlatformColumnProps) {
  return (
    <section
      className={cn(
        'flex h-full min-h-0 w-full min-w-[180px] flex-col items-center gap-[10px] rounded-[14px] border-[0.5px] border-[#e3e7f1] px-[12px] py-[14px]',
        '',
        className,
      )}
    >
      <div className="flex w-full min-h-0 flex-1 flex-col gap-[16px]">
        {/* —— 头部区域：图标 + 标题 + 计数 —— */}
        <div className="flex w-full shrink-0 flex-col gap-[10px]">
          <div className="flex w-full items-center justify-between">
            <div className="flex items-center gap-[4px]">
              <span className="flex size-[14px] shrink-0 items-center justify-center text-[#464c5e]">
                {icon}
              </span>
              <p className="truncate text-[12px] font-medium text-[#464c5e]">{title}</p>
            </div>
            <div className="flex shrink-0 items-center gap-[2px] text-[12px] text-[#464c5e]">
              <span>{count}</span>
              {/* <span>{countLabel}</span> */}
              {/* <IconChevronDown className="size-[14px] text-[#757f9c]" /> */}
            </div>
          </div>

          {/* —— 筛选标签行（仅当 filters 非空时渲染） —— */}
          {filters && filters.length > 0 && (
            <div className="flex flex-wrap items-center gap-[6px]">
              {filters.map((filter) => (
                <span
                  key={filter}
                  className="rounded-[20px] border-[0.5px] border-[#e3e7f1] px-[8px] py-[2px] text-[10px] leading-[normal] text-[#757f9c]"
                >
                  {filter}
                </span>
              ))}
            </div>
          )}

          <div className="h-px w-full bg-[#e3e7f1]" />
        </div>

        {/* —— 卡片列表区域：加载中显示骨架，空时显示占位，否则渲染 children —— */}
        <div className="mr-[-12px] flex min-h-0 w-[calc(100%+12px)] flex-1 flex-col gap-[16px] overflow-y-auto pr-[12px]">
          {loading ? (
            <PlatformColumnSkeleton />
          ) : isEmpty ? (
            // 空状态占位：图标 + 主提示 + 副提示
            <div className="flex min-h-[180px] w-full flex-1 items-center justify-center rounded-[18px] border border-dashed border-[#e4e9f2] bg-[#fbfcfe] px-[18px] py-[28px] text-center">
              <div className="flex max-w-[180px] flex-col items-center">
                <span className="grid size-[34px] place-items-center rounded-[12px] bg-white text-[#98a2b3] shadow-[0_1px_8px_rgba(70,76,94,0.06)] ring-1 ring-[#edf1f6]">
                  <IconChevronDown className="size-[16px] rotate-90" />
                </span>
                <p className="mt-[12px] text-[13px] font-medium leading-[19px] text-[#7f879a]">
                  {emptyText}
                </p>
                <p className="mt-[4px] text-[10px] leading-[16px] text-[#a7adbb]">
                  发布内容后会在这里展示
                </p>
              </div>
            </div>
          ) : (
            children
          )}
        </div>
      </div>

      {/* —— 底部：分割线 + 查看全部按钮（空状态时不显示） —— */}
      {!isEmpty && (
        <>
          <div className="h-px w-full shrink-0 bg-[#e3e7f1]" />

          <button
            type="button"
            onClick={onViewAll}
            className="flex w-[120px] shrink-0 items-center justify-center gap-[2px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[20px] py-[8px] text-[12px] text-[#757f9c] transition-colors hover:text-[#18181a]"
          >
            查看全部
            <IconChevronDown className="size-[14px] shrink-0 -rotate-90" />
          </button>
        </>
      )}
    </section>
  );
}

/**
 * 列骨架占位组件。
 * 在数据加载期间渲染三个带脉冲动画的灰色方块，模拟卡片加载效果。
 *
 * @returns 三个骨架方块的 JSX 元素
 */
function PlatformColumnSkeleton() {
  return (
    <div className="flex w-full flex-col gap-[16px]">
      {[0, 1, 2].map((index) => (
        <div
          key={index}
          className="h-[112px] w-full shrink-0 animate-pulse rounded-[20px] border-[0.5px] border-[#f0f1f5] bg-[#f6f6f6]"
        />
      ))}
    </div>
  );
}

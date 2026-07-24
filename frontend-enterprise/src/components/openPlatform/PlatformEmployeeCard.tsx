/**
 * @file 开放广场数字员工卡片组件
 * @description
 * 用于数字员工广场列表中的紧凑型员工卡片。严格还原 Figma 设计稿的布局：
 * 灰色横幅区域承载头像（头像顶部会超出横幅上沿），其右侧依次展示名称、角色、
 * 在线状态胶囊和右箭头交互指示；横幅下方为两行描述文本和拼接的统计指标行。
 *
 * 该组件作为可点击按钮整体触发 onOpen 回调，打开员工详情抽屉。
 */

import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import IconArrowRight from '../../assets/icons/arrow-right.svg?react';

/**
 * 单条统计指标的形状。
 */
export type PlatformStat = {
  /** 指标数值，通常为数字或短文本。 */
  value: ReactNode;
  /** 指标标签，例如"资料 / 技能 / SOP"。 */
  label: string;
};

export type PlatformEmployeeCardProps = {
  /** 头像插画，通常为 <EmployeeAvatar /> 组件。 */
  avatar: ReactNode;
  /** 员工名称。 */
  name: ReactNode;
  /** 员工角色。 */
  role: ReactNode;
  /** 是否在线，控制状态圆点颜色和文案。 */
  online?: boolean;
  /** 员工描述，最多显示两行。 */
  description: ReactNode;
  /** 底部统计指标段数组（资料 / 技能 / SOP …）。 */
  stats: PlatformStat[];
  /** 点击卡片时的回调，用于打开详情抽屉。 */
  onOpen?: () => void;
  className?: string;
};

/**
 * 数字员工广场卡片组件。
 *
 * 布局结构（自上而下）：
 * 1. 灰色横幅：左侧头像（超出横幅上沿）+ 右侧名称/角色/在线状态；右侧箭头图标
 * 2. 两行描述文本
 * 3. 拼接的统计指标行（各段共享边框，首尾圆角）
 *
 * 整张卡片为按钮元素，点击触发 `onOpen` 回调。
 *
 * @param props - 组件属性，参见 {@link PlatformEmployeeCardProps}
 * @returns 员工卡片的 JSX 元素
 */
export default function PlatformEmployeeCard({
  avatar,
  name,
  role,
  online = true,
  description,
  stats,
  onOpen,
  className,
}: PlatformEmployeeCardProps) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className={cn(
        'group relative flex h-[140px] w-full shrink-0 flex-col justify-end gap-[6px] rounded-[20px] border-[0.5px] border-[#f6f6f6] bg-white p-[4px] text-left transition-shadow hover:shadow-[0_10px_24px_rgba(0,0,0,0.06)]',
        '',
        className,
      )}
    >
      {/* —— 灰色横幅区域 —— */}
      <div className="flex w-full flex-col px-[6px] pb-[2px]">
        <div className="flex h-[54px] w-full items-end justify-between rounded-[14px] bg-[#f6f6f6] px-[8px] pb-[4px] pt-[8px]">
          <div className="flex min-w-0 items-end gap-[10px]">
            {/* 头像容器：高度超出横幅，使头像顶部露出于横幅之上 */}
            <div className="flex h-[59px] w-[50px] shrink-0 items-end justify-center">
              {avatar}
            </div>
            {/* 名称 + 角色 + 在线状态 */}
            <div className="flex min-w-0 flex-col items-start justify-center gap-[2px]">
              <p className="truncate text-[10px] font-medium text-[#18181a] leading-[1.35]">{name}</p>
              <p className="truncate text-[8px] text-[#757f9c] leading-[1.6]">{role}</p>
              <span className="inline-flex w-[34px] items-center justify-center rounded-[90px] bg-white px-[4px] py-[2px]">
                <span className="flex items-center gap-[2px]">
                  {/* 在线/下线状态圆点 */}
                  <i
                    className={cn('size-[4px] shrink-0 rounded-full', online ? 'bg-[#22c55e]' : 'bg-[#9ca3af]')}
                    aria-hidden="true"
                  />
                  <span className="text-[8px] text-[#757f9c]">{online ? '在线' : '下线'}</span>
                </span>
              </span>
            </div>
          </div>
          {/* 右箭头：悬停时加深，暗示可点击 */}
          <span className="grid size-[24px] shrink-0 self-center place-items-center rounded-[10px] bg-white text-[#757f9c] transition-colors group-hover:text-[#18181a]">
            <IconArrowRight className="size-[14px]" />
          </span>
        </div>
      </div>

      {/* —— 两行描述文本 —— */}
      <p className="line-clamp-2 h-[26px] w-full px-[8px] text-[10px] leading-[13px] text-[#757f9c]">
        {description}
      </p>

      {/* —— 统计指标行：各段共享边框，首尾段有圆角 —— */}
      <div className="flex w-full items-stretch px-[8px] pb-[4px]">
        {stats.map((stat, index) => (
          <div
            key={stat.label}
            className={cn(
              'flex h-[28px] flex-1 items-center justify-center border-[0.5px] border-[#e3e7f1] px-[10px]',
              // 第一个指标段左侧圆角
              index === 0 && 'rounded-l-[10px]',
              // 最后一个指标段右侧圆角
              index === stats.length - 1 && 'rounded-r-[10px]',
              // 非第一个段去掉左边框，使相邻段共享一条边框线
              index > 0 && 'border-l-0',
            )}
          >
            <span className="flex items-baseline gap-[2px] leading-none">
              <span className="text-[10px] font-medium text-[#18181a]">{stat.value}</span>
              <span className="text-[8px] text-[#464c5e]">{stat.label}</span>
            </span>
          </div>
        ))}
      </div>
    </button>
  );
}

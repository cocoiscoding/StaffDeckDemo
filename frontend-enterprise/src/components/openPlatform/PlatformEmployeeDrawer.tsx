/**
 * @file 开放广场数字员工详情抽屉组件
 * @description
 * SD1 数字员工广场详情侧拉面板（Figma 298:1416）。
 * 从屏幕右侧滑入，展示某位数字员工的完整信息：大尺寸头像、名称、描述、
 * 在线状态、统计指标行、分类信息、工作风格标签、详细说明文本，
 * 以及底部操作区（条件性删除按钮 + "使用员工"按钮）。
 *
 * 支持通过上一/下一导航箭头在员工列表间切换。
 */

import type { ReactNode } from 'react';

import { Sheet, SheetContent } from '@/components/ui';
import { cn } from '@/lib/utils';
import { XIcon } from 'lucide-react';

import IconChevronDown from '../../assets/icons/chevron-down.svg?react';
import IconTrash from '../../assets/icons/trash.svg?react';
import EmployeeAvatar from '../EmployeeAvatar';
import type { AgentProfileRead } from '../../types';

import type { PlatformStat } from './PlatformEmployeeCard';

export type PlatformEmployeeDrawerProps = {
  /** 抽屉是否打开。 */
  open: boolean;
  /** 当前展示的数字员工数据。 */
  agent: AgentProfileRead;
  /** 所属广场分类标题（如"客服员工"）。 */
  platformTitle: string;
  /** 员工名称。 */
  name: ReactNode;
  /** 员工角色。 */
  role: ReactNode;
  /** 员工简短描述。 */
  description: ReactNode;
  /** 员工详细说明文本。 */
  detailText: ReactNode;
  /** 工作风格标签数组（最多展示前 3 个）。 */
  workStyles: string[];
  /** 统计指标行数据。 */
  stats: PlatformStat[];
  /** 是否在线。 */
  online?: boolean;
  /** 当前用户是否有管理权限（控制删除按钮是否显示）。 */
  canManage?: boolean;
  /** 删除操作进行中（禁用按钮）。 */
  deleting?: boolean;
  /** 是否存在前一位员工（控制上一箭头可用状态）。 */
  hasPrev?: boolean;
  /** 是否存在后一位员工（控制下一箭头可用状态）。 */
  hasNext?: boolean;
  /** 关闭抽屉回调。 */
  onClose: () => void;
  /** 切换到前一位员工。 */
  onPrev?: () => void;
  /** 切换到后一位员工。 */
  onNext?: () => void;
  /** 删除当前员工回调。 */
  onDelete?: () => void;
  /** 使用该员工回调（跳转到对话等）。 */
  onUse: () => void;
};

/**
 * 抽屉内使用的水平分割线。
 * @returns 1px 高的灰色分割线元素
 */
function DrawerDivider() {
  return <div className="h-px w-full shrink-0 bg-[#e3e7f1]" />;
}

/**
 * 抽屉头部的上一/下一导航箭头按钮。
 *
 * @param direction - 箭头方向：'prev' 指向左（上一位），'next' 指向右（下一位）
 * @param disabled - 是否禁用（列表边界时禁用对应方向）
 * @param onClick - 点击回调
 * @param label - 无障碍标签
 * @returns 导航箭头按钮元素
 */
function NavChevron({
  direction,
  disabled,
  onClick,
  label,
}: {
  direction: 'prev' | 'next';
  disabled?: boolean;
  onClick?: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className="grid size-[14px] place-items-center text-[#757f9c] transition-colors enabled:hover:text-[#18181a] disabled:cursor-not-allowed disabled:opacity-35"
    >
      <IconChevronDown
        className={cn('size-[14px]', direction === 'prev' ? 'rotate-90' : '-rotate-90')}
      />
    </button>
  );
}

/**
 * SD1 数字员工广场详情侧拉面板（Figma 298:1416）。
 *
 * 数据流转：父组件传入完整的 agent 数据和展示所需的 ReactNode 字段，
 * 组件内部仅负责布局渲染和事件回调转发。导航、删除、使用等操作均通过
 * 回调委托给父组件处理。
 *
 * @param props - 组件属性，参见 {@link PlatformEmployeeDrawerProps}
 * @returns 详情抽屉的 JSX 元素
 */
export default function PlatformEmployeeDrawer({
  open,
  agent,
  platformTitle,
  name,
  role,
  description,
  detailText,
  workStyles,
  stats,
  online = true,
  canManage = false,
  deleting = false,
  hasPrev = false,
  hasNext = false,
  onClose,
  onPrev,
  onNext,
  onDelete,
  onUse,
}: PlatformEmployeeDrawerProps) {
  return (
    <Sheet open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <SheetContent
        side="right"
        showCloseButton={false}
        className={cn(
          'platform-employee-drawer flex w-[400px] flex-col gap-[10px] border-[0.5px] border-[#e3e7f1] bg-white p-[16px_20px] shadow-[0_4px_15px_rgba(0,0,0,0.25)] sm:max-w-[400px]',
          'top-[24px]! right-[24px]! bottom-[24px]! left-auto! h-auto! max-h-[calc(100vh-48px)] rounded-[20px]',
          '',
        )}
      >
        {/* —— 头部栏：分类标题 + 导航箭头 + 关闭按钮 —— */}
        <div className="flex w-full shrink-0 flex-col gap-[10px]">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-[4px]">
              <span className="text-[12px] font-medium capitalize text-[#464c5e]">
                {platformTitle}
              </span>
              <NavChevron direction="prev" disabled={!hasPrev} onClick={onPrev} label="上一位员工" />
              <NavChevron direction="next" disabled={!hasNext} onClick={onNext} label="下一位员工" />
            </div>
            <button
              type="button"
              aria-label="关闭"
              onClick={onClose}
              className="grid size-[14px] place-items-center text-[#757f9c] transition-colors hover:text-[#18181a]"
            >
              <XIcon className="size-[14px]" strokeWidth={1.75} />
            </button>
          </div>
          <DrawerDivider />
        </div>

        {/* —— 可滚动主体区域 —— */}
        <div className="flex min-h-0 flex-1 flex-col gap-[10px] overflow-auto px-[4px] pt-[48px]">
          {/* 头像 + 名称 + 描述 + 在线状态 */}
          <div className="flex w-full items-end gap-[10px] pb-[4px]">
            <div className="flex h-[117.5px] w-[100px] shrink-0 items-end justify-center overflow-hidden">
              <EmployeeAvatar
                agent={agent}
                width={100}
                height={118}
                fit="contain"
                objectPosition="center bottom"
                className="overflow-visible! rounded-none! border-0! bg-transparent! bg-none! shadow-none! after:hidden!"
              />
            </div>
            <div className="flex min-w-0 flex-1 flex-col justify-center gap-[8px] pb-[2px]">
              <div className="flex flex-col gap-[4px]">
                <p className="truncate text-[16px] font-medium capitalize text-[#464c5e]">
                  {name}
                </p>
                <p className="line-clamp-2 text-[12px] leading-[18px] text-[#757f9c]">
                  {description}
                </p>
              </div>
              {/* 在线状态胶囊：在线为绿色，下线为灰色 */}
              <span
                className={cn(
                  'inline-flex w-fit items-center gap-[4px] rounded-[90px] border-[0.5px] px-[10px] py-[4px]',
                  online
                    ? 'border-[#96d9b0] bg-[#e9f7ef] text-[#2cb360]'
                    : 'border-[#d1d5db] bg-[#f3f4f6] text-[#757f9c]',
                )}
              >
                <i
                  className={cn('size-[4px] shrink-0 rounded-full shadow-[inset_1px_1px_2px_0.5px_rgba(0,0,0,0.05)]', online ? 'bg-[#22c55e]' : 'bg-[#9ca3af]')}
                  aria-hidden="true"
                />
                <span className="text-[10px] capitalize">{online ? '在线' : '下线'}</span>
              </span>
            </div>
          </div>

          {/* 统计指标行 */}
          <div className="flex w-full items-stretch">
            {stats.map((stat, index) => (
              <div
                key={stat.label}
                className={cn(
                  'flex h-[60px] flex-1 flex-col justify-center gap-[4px] border-[0.5px] border-[#e3e7f1] px-[20px] py-[8px]',
                  index === 0 && 'rounded-l-[14px]',
                  index === stats.length - 1 && 'rounded-r-[14px]',
                  index > 0 && 'border-l-0',
                )}
              >
                <strong className="text-[18px] font-medium text-[#18181a]">{stat.value}</strong>
                <span className="text-[10px] text-[#464c5e]">{stat.label}</span>
              </div>
            ))}
          </div>

          {/* 分类信息双栏 */}
          <div className="grid grid-cols-2 gap-[10px]">
            <div className="flex min-h-[60px] flex-col justify-center gap-[4px] rounded-[14px] border-[0.5px] border-[#e3e7f1] px-[16px] py-[8px]">
              <span className="text-[10px] leading-[13px] text-[#464c5e]">分类</span>
              <strong className="truncate text-[12px] leading-[16px] font-medium text-[#18181a]">{platformTitle}</strong>
            </div>
            <div className="flex min-h-[60px] flex-col justify-center gap-[4px] rounded-[14px] border-[0.5px] border-[#e3e7f1] px-[16px] py-[8px]">
              <span className="text-[10px] leading-[13px] text-[#464c5e]">分类</span>
              <strong className="truncate text-[12px] leading-[16px] font-medium text-[#18181a]">{role}</strong>
            </div>
          </div>

          {/* 说明区域：工作风格标签 + 详细文本 */}
          <div className="flex min-h-0 flex-1 flex-col gap-[8px]">
            <span className="text-[12px] capitalize text-[#464c5e]">说明</span>
            <div className="flex min-h-0 flex-1 flex-col gap-[10px]">
              {workStyles.length > 0 && (
                <div className="flex flex-wrap gap-[10px]">
                  {workStyles.slice(0, 3).map((tag) => (
                    <span
                      key={tag}
                      className="rounded-[10px] bg-[#f6f6f6] px-[12px] py-[4px] text-[12px] text-[#757f9c]"
                    >
                      {tag}
                    </span>
                  ))}
                </div>
              )}
              <p className="text-[12px] leading-[20px] text-[#757f9c]">
                {detailText}
              </p>
            </div>
          </div>
        </div>

        <DrawerDivider />

        {/* —— 底部操作栏 —— */}
        <div className="flex shrink-0 justify-end gap-[10px]">
          {/* 删除按钮：仅当有管理权限且传入 onDelete 时显示 */}
          {canManage && onDelete && (
            <button
              type="button"
              disabled={deleting}
              onClick={onDelete}
              className="inline-flex h-[34px] w-[80px] items-center justify-center gap-[4px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white text-[12px] text-[#757f9c] transition-colors hover:border-[#d20b0b] hover:text-[#d20b0b] disabled:cursor-not-allowed disabled:opacity-50"
            >
              <IconTrash className="size-[14px]" />
              删除
            </button>
          )}
          <button
            type="button"
            onClick={onUse}
            className="inline-flex h-[34px] w-[80px] items-center justify-center rounded-[10px] bg-[#18181a] text-[12px] text-white transition-colors hover:bg-[#2a2a2e]"
          >
            使用员工
          </button>
        </div>
      </SheetContent>
    </Sheet>
  );
}

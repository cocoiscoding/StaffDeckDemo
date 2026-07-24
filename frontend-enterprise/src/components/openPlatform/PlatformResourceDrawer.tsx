/**
 * @file 开放广场资源详情抽屉组件
 * @description
 * SD1 广场资源详情侧拉面板（知识库 298:4801 / SOP·技能·工具 298:4869 系列）。
 * 从屏幕右侧滑入，展示知识库、技能、SOP 或工具的完整详情：模块图标、标题、
 * 描述、强调色徽章、分类信息双栏、详细说明文本，以及底部操作区
 * （条件性删除按钮 + 自定义使用按钮）。
 *
 * 支持通过上一/下一导航箭头在同类资源间切换。
 */

import type { ReactNode } from 'react';

import { Sheet, SheetContent } from '@/components/ui';
import { cn } from '@/lib/utils';
import { XIcon } from 'lucide-react';

import IconChevronDown from '../../assets/icons/chevron-down.svg?react';
import IconTrash from '../../assets/icons/trash.svg?react';

import { platformResourceAccentStyles, type PlatformResourceAccent } from './PlatformResourceCard';

export type PlatformResourceDrawerProps = {
  /** 抽屉是否打开。 */
  open: boolean;
  /** 所属广场分类标题（如"知识库"）。 */
  platformTitle: string;
  /** 资源图标。 */
  icon: ReactNode;
  /** 模块强调色。 */
  accent?: PlatformResourceAccent;
  /** 资源标题。 */
  title: ReactNode;
  /** 资源描述。 */
  description: ReactNode;
  /** 强调色徽章内容。 */
  badge: ReactNode;
  /** 分类元信息（右侧栏显示）。 */
  categoryMeta: ReactNode;
  /** 详细说明文本。 */
  detailText: ReactNode;
  /** 使用按钮的文案（如"使用知识库"）。 */
  useLabel: string;
  /** 当前用户是否有管理权限（控制删除按钮是否显示）。 */
  canManage?: boolean;
  /** 删除操作进行中（禁用按钮）。 */
  deleting?: boolean;
  /** 是否存在上一项（控制箭头可用状态）。 */
  hasPrev?: boolean;
  /** 是否存在下一项（控制箭头可用状态）。 */
  hasNext?: boolean;
  /** 关闭抽屉回调。 */
  onClose: () => void;
  /** 切换到上一项。 */
  onPrev?: () => void;
  /** 切换到下一项。 */
  onNext?: () => void;
  /** 删除当前资源回调。 */
  onDelete?: () => void;
  /** 使用该资源回调。 */
  onUse: () => void;
};

/** 抽屉面板的 Tailwind 类名常量。 */
const DRAWER_SHEET_CLASS = cn(
  'platform-resource-drawer flex w-[400px] flex-col gap-[10px] border-[0.5px] border-[#e3e7f1] bg-white p-[16px_20px] shadow-[0_4px_15px_rgba(0,0,0,0.25)] sm:max-w-[400px]',
  'top-[24px]! right-[24px]! bottom-[24px]! left-auto! h-auto! max-h-[calc(100vh-48px)] rounded-[20px]',
  '',
);

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
 * @param direction - 箭头方向：'prev' 指向左（上一项），'next' 指向右（下一项）
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
 * SD1 广场资源详情侧拉面板（知识库 298:4801 / SOP·技能·工具 298:4869 系列）。
 *
 * 数据流转：父组件传入完整的资源数据和展示字段，组件内部仅负责布局渲染和
 * 事件回调转发。导航、删除、使用等操作均通过回调委托给父组件处理。
 *
 * @param props - 组件属性，参见 {@link PlatformResourceDrawerProps}
 * @returns 资源详情抽屉的 JSX 元素
 */
export default function PlatformResourceDrawer({
  open,
  platformTitle,
  icon,
  accent = 'green',
  title,
  description,
  badge,
  categoryMeta,
  detailText,
  useLabel,
  canManage = false,
  deleting = false,
  hasPrev = false,
  hasNext = false,
  onClose,
  onPrev,
  onNext,
  onDelete,
  onUse,
}: PlatformResourceDrawerProps) {
  const accentStyles = platformResourceAccentStyles[accent];

  return (
    <Sheet open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <SheetContent side="right" showCloseButton={false} className={DRAWER_SHEET_CLASS}>
        {/* —— 头部栏：分类标题 + 导航箭头 + 关闭按钮 —— */}
        <div className="flex w-full shrink-0 flex-col gap-[10px]">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-[4px]">
              <span className="text-[12px] font-medium capitalize text-[#464c5e]">
                {platformTitle}
              </span>
              <NavChevron direction="prev" disabled={!hasPrev} onClick={onPrev} label="上一项" />
              <NavChevron direction="next" disabled={!hasNext} onClick={onNext} label="下一项" />
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
        <div className="flex min-h-0 flex-1 flex-col gap-[10px] overflow-auto px-[4px]">
          {/* 模块图标 */}
          <div className="size-[36px] shrink-0">{icon}</div>

          {/* 标题 + 描述 + 徽章 */}
          <div className="flex min-h-[75px] w-full flex-col justify-center gap-[8px] pb-[2px]">
            <div className="flex flex-col gap-[4px]">
              <p className="text-[16px] font-medium capitalize text-[#464c5e]">
                {title}
              </p>
              <p className="text-[12px] leading-[18px] text-[#757f9c]">
                {description}
              </p>
            </div>
            {/* 强调色徽章：使用对应模块主题色 */}
            <span
              className={cn(
                'inline-flex w-fit items-center rounded-[90px] px-[10px] py-[4px] text-[10px] capitalize',
                accentStyles.tag,
              )}
            >
              {badge}
            </span>
          </div>

          {/* 分类信息双栏 */}
          <div className="grid grid-cols-2 gap-[10px]">
            <div className="flex min-h-[60px] flex-col justify-center gap-[4px] rounded-[14px] border-[0.5px] border-[#e3e7f1] px-[16px] py-[8px]">
              <span className="text-[10px] leading-[13px] text-[#464c5e]">分类</span>
              <strong className="truncate text-[12px] leading-[16px] font-medium text-[#18181a]">
                {platformTitle}
              </strong>
            </div>
            <div className="flex min-h-[60px] flex-col justify-center gap-[4px] rounded-[14px] border-[0.5px] border-[#e3e7f1] px-[16px] py-[8px]">
              <span className="text-[10px] leading-[13px] text-[#464c5e]">分类</span>
              <strong className={cn('truncate text-[12px] leading-[16px] font-medium', accentStyles.meta)}>
                {categoryMeta}
              </strong>
            </div>
          </div>

          {/* 详细说明区域 */}
          <div className="flex min-h-0 flex-1 flex-col gap-[8px]">
            <span className="text-[12px] capitalize text-[#464c5e]">说明</span>
            <p className="text-[12px] leading-[20px] text-[#757f9c]">
              {detailText}
            </p>
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
              className="inline-flex h-[34px] w-[80px] items-center justify-center gap-[4px] rounded-[10px] border-[0.5px] border-[#d20b0b] bg-white text-[12px] text-[#d20b0b] transition-colors hover:bg-[#fce7e7] disabled:cursor-not-allowed disabled:opacity-50"
            >
              <IconTrash className="size-[14px]" />
              删除
            </button>
          )}
          <button
            type="button"
            onClick={onUse}
            className="inline-flex h-[34px] items-center justify-center rounded-[10px] bg-[#18181a] px-[20px] text-[12px] text-white transition-colors hover:bg-[#2a2a2e]"
          >
            {useLabel}
          </button>
        </div>
      </SheetContent>
    </Sheet>
  );
}

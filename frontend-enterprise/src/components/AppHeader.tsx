/**
 * @file AppHeader.tsx
 * @module components/AppHeader
 * @description 全局页面头部组件。左侧展示页面标题/描述（或自定义内容），
 *              右侧提供语言切换和用户头像下拉菜单（含退出登录）。
 *              支持通过 right 属性完全替换右侧默认内容。
 */

import type { ReactNode } from 'react';

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui';
import { cn } from '@/lib/utils';

import IconChevronDown from '../assets/icons/chevron-down.svg?react';
import IconLogout from '../assets/icons/logout.svg?react';
import LanguageSwitcher from './LanguageSwitcher';

/**
 * AppHeader 组件的属性定义。
 */
export type AppHeaderProps = {
  /**
   * 页面特定的左侧内容。提供时优先于 title/description 字段。
   */
  left?: ReactNode;
  /** 左侧插槽的标题行便捷字段。设置 left 时被忽略。 */
  title?: ReactNode;
  /** 左侧插槽的描述行便捷字段。设置 left 时被忽略。 */
  description?: ReactNode;
  /**
   * 右侧自定义内容。提供时完全替换默认的用户头像/退出下拉菜单
   *（例如未登录的登录页使用主题切换 + 登录按钮）。
   */
  right?: ReactNode;
  /** 点击退出菜单项时的回调。 */
  onLogout?: () => void;
  /** 当前用户的显示名称，用于生成头像首字母。 */
  userName?: string;
  /** 附加 CSS 类名。 */
  className?: string;
};

/**
 * 全局页面头部组件。右侧默认显示用户头像按钮，其下拉菜单包含退出登录操作；
 * 左侧通过 left 插槽或 title/description 便捷字段按页面提供。
 * 当传入 left 时直接渲染，便捷字段被忽略。传入 right 可覆盖默认头像区域。
 *
 * @param props - 组件属性
 * @param props.left - 左侧自定义内容
 * @param props.title - 左侧标题
 * @param props.description - 左侧描述
 * @param props.right - 右侧自定义内容（替换默认头像下拉）
 * @param props.onLogout - 退出登录回调
 * @param props.userName - 当前用户名
 * @param props.className - 附加类名
 * @returns 渲染好的页面头部 header 元素
 */
export default function AppHeader({
  left,
  title,
  description,
  right,
  onLogout,
  userName,
  className,
}: AppHeaderProps) {
  // 提取用户名首字母（大写）作为头像占位符
  const initial = userName?.trim()?.[0]?.toUpperCase();

  // 左侧内容：优先使用 left 插槽，否则用 title/description 组合，都没有则为 null
  const leftContent = left ?? (
    (title !== undefined || description !== undefined) ? (
      <div className="flex min-h-[40px] flex-col justify-center gap-[4px]">
        {title !== undefined && (
          <p className="text-[16px] font-medium leading-[normal] text-[#464c5e]">{title}</p>
        )}
        {description !== undefined && (
          <p className="text-[14px] leading-[normal] text-[#757f9c]">{description}</p>
        )}
      </div>
    ) : null
  );

  return (
    <header className={cn('flex w-full items-start gap-[16px]', className)}>
      <div className="min-w-0 flex-1">{leftContent}</div>
      <div className="flex h-[32px] shrink-0 items-center gap-[8px]">
        {/* 语言切换器始终显示 */}
        <LanguageSwitcher />
        {/* 右侧内容：自定义 right 优先，否则渲染默认的用户头像下拉菜单 */}
        {right !== undefined ? right : (
          <DropdownMenu>
            <DropdownMenuTrigger
              aria-label="账户菜单"
              className="flex h-[32px] shrink-0 items-center gap-[8px] rounded-[10px] pl-[4px] pr-[8px] outline-none"
            >
              <span className="grid size-[32px] shrink-0 place-items-center overflow-hidden rounded-full bg-[#eef1fb] text-[14px] font-medium leading-none text-[#7e96dc]">
                {initial ?? '--'}
              </span>
              <IconChevronDown className="size-[14px] shrink-0 text-[#757F9C]" />
            </DropdownMenuTrigger>
            <DropdownMenuContent
              align="end"
              className="w-fit min-w-0 rounded-[14px] border-0 bg-white p-[6px] shadow-[0px_16px_15px_rgba(0,0,0,0.1)] ring-0 [--accent:#F6F6F6] [--accent-foreground:#18181A]"
            >
              <DropdownMenuItem
                onSelect={() => onLogout?.()}
                className="h-[36px] cursor-pointer gap-2 rounded-[10px] px-[12px] text-[14px] text-[#464C5E]"
              >
                <IconLogout className="size-[16px]" />
                退出登录
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
    </header>
  );
}

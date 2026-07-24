/**
 * @file 企业版 UI 共享样式常量与辅助函数。
 *
 * 将各列表页（SOP、技能、定时任务、员工记忆、对话日志等）中重复使用的
 * Tailwind 类名抽取为常量，避免在每个页面文件中复制粘贴相同的样式字符串，
 * 保证视觉一致性并便于全局调整。
 *
 * 核心概念：
 * - **样式令牌（Style Token）**：每个常量对应一种 UI 元素的标准样式，如菜单项、对话框按钮、搜索框等。
 * - **日期格式化**：`formatDateTime` 封装了时区感知的日期格式化，供所有列表页统一使用。
 */

import { formatClientDateTime } from './timezone';

/**
 * 下拉菜单项样式（图标 + 文本，12px 灰色文字）。
 * 用于 DropdownMenu 中的普通菜单项。
 */
export const MENU_ITEM_CLASS =
  'cursor-pointer gap-[6px] rounded-[10px] px-[12px] py-[6px] text-[12px] text-[#858b9c] focus:text-[#18181a] [&_svg]:size-[14px]';

/**
 * 危险操作菜单项样式（红色文字，用于删除等不可逆操作）。
 */
export const MENU_ITEM_DANGER_CLASS =
  'cursor-pointer gap-[6px] rounded-[10px] px-[12px] py-[6px] text-[12px] text-[#d20b0b] focus:bg-[#fce7e7] focus:text-[#d20b0b] focus:[&_svg]:text-[#d20b0b]! [&_svg]:size-[14px]';

/**
 * 下拉菜单浮层容器样式（圆角白色卡片 + 柔和阴影）。
 */
export const MENU_CONTENT_CLASS =
  'flex w-auto min-w-[140px] flex-col gap-[4px] rounded-[14px] border-0 bg-white p-[4px] shadow-[0px_0px_8px_rgba(0,0,0,0.1)] ring-0 [--accent:#F6F6F6] [--accent-foreground:#18181A]';

/**
 * shadcn Select 触发器样式（34px 高度，与筛选控件统一）。
 */
export const SELECT_TRIGGER_CLASS =
  'h-[34px] data-[size=default]:h-[34px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white text-[12px] text-[#464c5e] shadow-none data-placeholder:text-[#858b9c] hover:border-[#cbd3e6] focus-visible:border-[#18181a] focus-visible:ring-0';

/**
 * 移动端（<768px）列表卡片外层包装样式。
 */
export const MOBILE_CARD_CLASS =
  'min-w-0 rounded-[8px] border border-[#eceef1] bg-white p-[14px]';

/**
 * 对话框底部操作栏样式（白色背景、上边框、操作按钮右对齐）。
 */
export const DIALOG_FOOTER_CLASS =
  'flex items-center justify-end gap-[8px] bg-white px-[24px] py-[12px]';

/**
 * 对话框标准取消按钮样式。
 */
export const DIALOG_CANCEL_BUTTON_CLASS =
  'h-[32px] min-w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]';

/**
 * 对话框标准主操作（确认）按钮样式。
 */
export const DIALOG_PRIMARY_BUTTON_CLASS =
  'h-[32px] min-w-[80px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030]';

/**
 * 标准描边操作按钮样式（工具栏刷新、卡片操作等）。
 */
export const OUTLINE_ACTION_BUTTON_CLASS =
  'h-[34px] gap-[4px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[20px] text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]';

/**
 * 紧凑型描边操作按钮样式（用于卡片标题行内联按钮）。
 */
export const OUTLINE_ACTION_BUTTON_SM_CLASS =
  'h-[32px] gap-[4px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#464c5e] hover:border-[#cbd3e6] hover:bg-[#f6f6f6] hover:text-[#18181a] [&_svg:not([class*="size-"])]:size-[14px]';

/**
 * 搜索组合框外层容器样式（输入框 + 提交按钮一体化）。
 */
export const SEARCH_COMBO_CLASS =
  'flex h-[32px] min-w-0 items-stretch overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white transition-colors focus-within:border-[#18181a]';

/**
 * 搜索组合框中的输入框样式。
 */
export const SEARCH_COMBO_INPUT_CLASS =
  'min-w-0 flex-1 bg-transparent px-[14px] text-[14px] text-[#17191f] outline-none placeholder:text-[#c0c6d4]';

/**
 * 搜索组合框中的提交按钮样式。
 */
export const SEARCH_COMBO_BUTTON_CLASS =
  'shrink-0 bg-[#18181a] px-[20px] text-[14px] font-normal text-white transition-colors hover:bg-[#303030] disabled:pointer-events-none disabled:opacity-50';

/**
 * 将后端时间戳格式化为当前 UI 语言的本地日期时间，空值或无效时返回 `-`。
 *
 * @param value 后端时间字符串
 * @returns 格式化后的日期时间字符串，或 `-`
 */
export function formatDateTime(value?: string): string {
  return formatClientDateTime(value, '-');
}

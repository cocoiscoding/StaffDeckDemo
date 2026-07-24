/**
 * @file app-toast.tsx
 * @description 项目自定义的 Toast 轻提示组件，基于 sonner 库封装。
 *              提供统一的 success / error / warning / info / loading 消息提示。
 *              success 和 error 使用项目自定义的「药丸式」样式（对齐 SD1 设计规范），
 *              warning / info / loading 则委托给 sonner 原生样式。
 *              本项目中作为全局消息提示工具，通过 notify 对象调用。
 */

import type { ReactNode } from 'react';
import { toast, type ExternalToast } from 'sonner';

import { cn } from '@/lib/utils';

import IconError from '@/assets/icons/error-fill.svg?react';
import IconSuccess from '@/assets/icons/success-fill.svg?react';

/** Toast 变体类型，仅 success 和 error 使用自定义样式 */
type ToastVariant = 'success' | 'error';

// 颜色、圆角和间距对齐 SD1 「基础组件/Dialog/Message」设计规范
// （success 节点 281:3334，error 节点 281:3342）
const VARIANTS: Record<
  ToastVariant,
  { container: string; icon: string; Icon: typeof IconSuccess }
> = {
  // 成功提示：绿色主题
  success: {
    container: 'border-[#96d9b0] bg-[#e9f7ef] text-[#018434]',
    icon: 'text-[#2cb360]',
    Icon: IconSuccess,
  },
  // 错误提示：红色主题
  error: {
    container: 'border-[#f38989] bg-[#fce7e7] text-[#d20b0b]',
    icon: 'text-[#d20b0b]',
    Icon: IconError,
  },
};

/**
 * 药丸式 Toast 内容组件，渲染带图标的圆角提示条。
 * @param variant - 提示类型，"success" 或 "error"。
 * @param message - 提示文本内容。
 * @returns 渲染一个居中的圆角提示条，包含状态图标和文本。
 */
function ToastPill({ variant, message }: { variant: ToastVariant; message: ReactNode }) {
  const { container, icon, Icon } = VARIANTS[variant];
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        'pointer-events-auto flex max-w-full items-center gap-[12px] rounded-[14px] border border-solid px-[24px] py-[10px] shadow-[0px_12px_32px_rgba(0,0,0,0.12)]',
        container,
      )}
    >
      <Icon className={cn('size-[16px] shrink-0', icon)} aria-hidden="true" />
      <span className="text-[14px] leading-[normal] wrap-anywhere">{message}</span>
    </div>
  );
}

/**
 * 品牌化 Toast 接受的配置项。
 * 外观（图标、样式和居中位置）由本组件统一管理，因此排除了这些 sonner 原生字段。
 */
export type AppToastOptions = Omit<
  ExternalToast,
  'icon' | 'className' | 'style' | 'unstyled' | 'descriptionClassName'
>;

/**
 * 显示指定变体的自定义 Toast。
 * @param variant - 提示类型，"success" 或 "error"。
 * @param message - 提示文本内容。
 * @param options - 额外的 sonner 配置项。
 * @returns sonner 返回的 toast ID。
 */
function showVariant(variant: ToastVariant, message: ReactNode, options?: AppToastOptions) {
  return toast.custom(() => <ToastPill variant={variant} message={message} />, {
    // 成功提示 3.2 秒后消失，错误提示 4.8 秒后消失
    duration: variant === 'success' ? 3200 : 4800,
    unstyled: true,
    className: 'flex w-full justify-center',
    ...options,
  });
}

/**
 * 全局 Toast 辅助对象。
 * `success` / `error` 渲染 SD1 风格的消息药丸；
 * `warning` / `info` / `loading` 委托给 sonner，共享 <Toaster /> 上配置的居中定位。
 */
export const notify = {
  /** 显示成功提示（绿色药丸，3.2 秒后消失） */
  success: (message: ReactNode, options?: AppToastOptions) =>
    showVariant('success', message, options),
  /** 显示错误提示（红色药丸，4.8 秒后消失） */
  error: (message: ReactNode, options?: AppToastOptions) => showVariant('error', message, options),
  /** 显示警告提示（委托 sonner 原生样式） */
  warning: (message: ReactNode, options?: AppToastOptions) => toast.warning(message, options),
  /** 显示信息提示（委托 sonner 原生样式） */
  info: (message: ReactNode, options?: AppToastOptions) => toast.info(message, options),
  /** 显示加载中提示（委托 sonner 原生样式） */
  loading: (message: ReactNode, options?: AppToastOptions) => toast.loading(message, options),
  /** 关闭指定 ID 的 Toast，不传 ID 则关闭所有 */
  dismiss: (id?: string | number) => toast.dismiss(id),
};

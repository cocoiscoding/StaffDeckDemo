/**
 * @file ConfirmDialog.tsx
 * @module components/ConfirmDialog
 * @description 确认弹窗组件。基于 Radix AlertDialog 原语构建，提供标题、描述、
 *              取消/确认按钮的标准确认交互，支持危险操作（红色按钮）和加载锁定。
 */

import type { ReactNode } from 'react';
import { AlertDialog as AlertDialogPrimitive } from 'radix-ui';

import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
} from '@/components/ui';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

import IconWarningFill from '../assets/icons/warning-fill.svg?react';

/**
 * 确认弹窗组件的属性定义。
 */
export type ConfirmDialogProps = {
  /** 弹窗是否打开。 */
  open: boolean;
  /** 控制弹窗开关的回调。 */
  onOpenChange: (open: boolean) => void;
  /** 标题内容，支持富文本（如在 <strong> 中嵌入目标名称）。 */
  title: ReactNode;
  /** 标题下方的补充说明文本（可选）。 */
  description?: ReactNode;
  /** 确认按钮文案，默认"删除"。 */
  confirmText?: string;
  /** 取消按钮文案，默认"取消"。 */
  cancelText?: string;
  /** 确认按钮点击回调。 */
  onConfirm: () => void;
  /** 为 true 时禁用按钮并阻止通过遮罩/ESC 关闭弹窗。 */
  loading?: boolean;
  /** 是否为危险操作（红色确认按钮）。默认 true，匹配删除流程。 */
  destructive?: boolean;
  /** 覆盖标题前导图标。传入 null 可隐藏图标。 */
  icon?: ReactNode;
};

/**
 * 确认弹窗组件。呈现警告图标 + 标题头部、灰色描述文字，以及右对齐的取消/确认按钮。
 * 基于 Radix AlertDialog 原语，自动处理焦点陷阱和无障碍访问。
 *
 * @param props - 组件属性
 * @param props.open - 是否打开
 * @param props.onOpenChange - 开关回调
 * @param props.title - 标题
 * @param props.description - 描述文本
 * @param props.confirmText - 确认按钮文案，默认"删除"
 * @param props.cancelText - 取消按钮文案，默认"取消"
 * @param props.onConfirm - 确认回调
 * @param props.loading - 是否处于加载锁定状态，默认 false
 * @param props.destructive - 是否危险操作样式，默认 true
 * @param props.icon - 自定义前导图标
 * @returns 渲染好的确认弹窗
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmText = '删除',
  cancelText = '取消',
  onConfirm,
  loading = false,
  destructive = true,
  icon,
}: ConfirmDialogProps) {
  // 前导图标：未自定义时使用默认的橙色警告图标
  const leadingIcon =
    icon === undefined ? (
      <IconWarningFill className="mt-px size-[16px] shrink-0 text-[#ff7f00]" />
    ) : (
      icon
    );

  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        // 加载中时阻止通过遮罩点击或 ESC 关闭弹窗
        if (loading && !next) return;
        onOpenChange(next);
      }}
    >
      <AlertDialogContent className="gap-0 overflow-hidden rounded-[16px] p-0">
        {/* 标题区：前导图标 + 标题文字 */}
        <div className="flex items-start gap-[8px] px-[16px] pt-[16px] pb-[12px]">
          {leadingIcon}
          <AlertDialogTitle className="min-w-0 flex-1 text-[14px] leading-[normal] font-medium text-[#18181a] [word-break:break-word]">
            {title}
          </AlertDialogTitle>
        </div>
        {/* 描述区（可选） */}
        {description != null && (
          <div className="px-[24px] pb-[12px]">
            <AlertDialogDescription className="text-[14px] leading-[20px] text-[#4f5669] [word-break:break-word]">
              {description}
            </AlertDialogDescription>
          </div>
        )}
        {/* 按钮区：取消（左）+ 确认（右） */}
        <div className="flex items-center justify-end gap-[8px] pt-[12px] pr-[16px] pb-[16px] pl-[12px]">
          <AlertDialogPrimitive.Cancel asChild>
            <Button
              variant="outline"
              disabled={loading}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] py-[8px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
            >
              {cancelText}
            </Button>
          </AlertDialogPrimitive.Cancel>
          <AlertDialogPrimitive.Action asChild>
            <Button
              disabled={loading}
              className={cn(
                'h-[32px] w-[80px] rounded-[10px] px-[12px] py-[8px] text-[14px] font-normal',
                // 危险操作使用红色按钮，普通操作使用深色按钮
                destructive
                  ? 'bg-[#d20b0b] text-white hover:bg-[#b80909]'
                  : 'bg-[#18181a] text-white hover:bg-[#303030]',
              )}
              onClick={(event) => {
                event.preventDefault();
                onConfirm();
              }}
            >
              {confirmText}
            </Button>
          </AlertDialogPrimitive.Action>
        </div>
      </AlertDialogContent>
    </AlertDialog>
  );
}

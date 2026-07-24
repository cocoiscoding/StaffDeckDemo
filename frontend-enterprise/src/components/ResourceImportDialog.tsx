/**
 * @file ResourceImportDialog.tsx
 * @module components/ResourceImportDialog
 * @description 资源复制导入弹窗组件。通用型弹窗，用于从其他作用域复制资源（SOP/技能等）。
 *              提供复制目标选择、复制来源选择、可导入资源勾选列表，以及确认提交。
 */

import { useEffect } from 'react';
import type { ReactNode } from 'react';
import { ChevronDown } from 'lucide-react';

import {
  Checkbox,
  Dialog,
  DialogContent,
  DialogTitle,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { SELECT_TRIGGER_CLASS } from '@/lib/enterprise-ui';

/** 导入来源选项类型 */
export type ImportSourceOption = { value: string; label: string };
/** 可导入资源项类型 */
export type ImportChoiceItem = { id: string; label: ReactNode };

/**
 * 资源导入弹窗的属性定义。
 */
export type ResourceImportDialogProps = {
  /** 弹窗是否打开。 */
  open: boolean;
  /** 是否处于加载状态。 */
  loading: boolean;
  /** 头部图标（14px）。 */
  icon: ReactNode;
  /** 弹窗标题。 */
  title: string;
  /** 复制目标选择器的占位文本（当目标不由页面作用域决定时使用）。 */
  targetPlaceholder?: string;
  /** 复制目标选择器的标签。 */
  targetLabel?: string;
  /** 复制目标选项列表。 */
  targets?: ImportSourceOption[];
  /** 当前选中的复制目标 ID。 */
  targetId?: string;
  /** "复制来源"选择器的占位文本。 */
  sourcePlaceholder: string;
  /** 复制来源选项列表。 */
  sources: ImportSourceOption[];
  /** 当前选中的复制来源 ID。 */
  sourceId: string;
  /** 勾选列表上方的标题，如"选择 SOP"/"选择技能"。 */
  itemsLabel: string;
  /** 可导入的资源项列表。 */
  items: ImportChoiceItem[];
  /** 已选中的资源 ID 数组。 */
  selectedIds: string[];
  /** 选了来源但无可导入资源时的空状态文本。 */
  emptyText: string;
  /** 未选来源时的提示文本，默认"请先选择复制来源"。 */
  emptySourceText?: string;
  /** 说明性页脚注释。 */
  note: ReactNode;
  /** 提交按钮文案，默认"复制"。 */
  submitText?: string;
  /** 复制目标变更回调。 */
  onTargetChange?: (value: string) => void;
  /** 复制来源变更回调。 */
  onSourceChange: (value: string) => void;
  /** 已选资源变更回调。 */
  onSelectedChange: (ids: string[]) => void;
  /** 关闭弹窗回调。 */
  onClose: () => void;
  /** 提交回调。 */
  onSubmit: () => void;
};

/**
 * 资源复制导入弹窗组件。由 SOP 和技能页面共用。
 * 包含复制来源选择器和可导入资源的勾选列表。
 *
 * @param props - 组件属性
 * @returns 渲染好的资源导入弹窗
 */
export function ResourceImportDialog({
  open,
  loading,
  icon,
  title,
  targetPlaceholder,
  targetLabel = '复制到',
  targets,
  targetId,
  sourcePlaceholder,
  sources,
  sourceId,
  itemsLabel,
  items,
  selectedIds,
  emptyText,
  emptySourceText = '请先选择复制来源',
  note,
  submitText = '复制',
  onTargetChange,
  onSourceChange,
  onSelectedChange,
  onClose,
  onSubmit,
}: ResourceImportDialogProps) {
  // 是否显示复制目标选择器
  const showTargetSelect = Boolean(targets && onTargetChange);
  // 有效来源 ID：当只有一个来源时自动选中
  const effectiveSourceId = sourceId || (sources.length === 1 ? sources[0].value : '');

  // 弹窗打开且只有一个来源时自动选中
  useEffect(() => {
    if (!open || sourceId || sources.length !== 1) return;
    onSourceChange(sources[0].value);
  }, [onSourceChange, open, sourceId, sources]);

  /** 切换资源项的选中状态 */
  const toggle = (id: string, checked: boolean) => {
    onSelectedChange(checked ? [...selectedIds, id] : selectedIds.filter((value) => value !== id));
  };
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        className="flex max-h-[calc(100dvh-4rem)] w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[640px]"
      >
        {/* 标题区：图标 + 标题 */}
        <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          {icon}
          <DialogTitle className="text-[14px] font-normal leading-none text-[#757f9c]">
            {title}
          </DialogTitle>
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-[14px] overflow-y-auto px-[12px]">
          {/* 复制目标选择器（可选） */}
          {showTargetSelect && (
            <div className="flex flex-col gap-[6px]">
              <span className="text-[11px] font-semibold text-[#858b9c]">{targetLabel}</span>
              <Select value={targetId || undefined} onValueChange={onTargetChange}>
                <SelectTrigger className={cn(SELECT_TRIGGER_CLASS, 'w-full')}>
                  <SelectValue placeholder={targetPlaceholder || targetLabel} />
                </SelectTrigger>
                <SelectContent>
                  {(targets || []).map((item) => (
                    <SelectItem key={item.value} value={item.value}>
                      {item.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}

          {/* 复制来源选择器 */}
          <div className="flex flex-col gap-[6px]">
            <span className="text-[11px] font-semibold text-[#858b9c]">复制来源</span>
            <div className="relative">
              <select
                value={effectiveSourceId}
                onChange={(event) => onSourceChange(event.target.value)}
                className={cn(
                  SELECT_TRIGGER_CLASS,
                  'w-full appearance-none px-3 pr-9 outline-none disabled:cursor-not-allowed disabled:opacity-60'
                )}
              >
                <option value="" disabled>
                  {sourcePlaceholder}
                </option>
                {sources.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </select>
              <ChevronDown className="pointer-events-none absolute right-3 top-1/2 size-4 -translate-y-1/2 text-[#858b9c]" />
            </div>
          </div>

          {/* 可导入资源勾选列表 */}
          <div className="flex flex-col gap-[6px]">
            <span className="text-[11px] font-semibold text-[#858b9c]">{itemsLabel}</span>
            <div className="max-h-[300px] overflow-y-auto rounded-[10px] border border-[#eef0f4] p-[6px]">
              {items.length === 0 ? (
                // 空状态：根据是否选了来源显示不同提示
                <div className="py-[28px] text-center text-[12px] text-[#858b9c]">
                  {sourceId ? emptyText : emptySourceText}
                </div>
              ) : (
                items.map((item) => (
                  <label
                    key={item.id}
                    className="flex cursor-pointer items-center gap-[10px] rounded-[8px] px-[8px] py-[7px] hover:bg-[#f6f6f6]"
                  >
                    <Checkbox
                      checked={selectedIds.includes(item.id)}
                      onCheckedChange={(checked) => toggle(item.id, checked === true)}
                    />
                    <span className="min-w-0 flex-1 truncate text-[12px] text-[#18181a]">
                      {item.label}
                    </span>
                  </label>
                ))
              )}
            </div>
          </div>

          {/* 说明性注释 */}
          <p className="text-[12px] leading-[1.6] text-[#858b9c]">{note}</p>
        </div>

        {/* 底部操作按钮 */}
        <div className="flex items-center justify-end gap-[8px] px-[12px]">
          <Button
            variant="outline"
            disabled={loading}
            onClick={onClose}
            className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
          >
            取消
          </Button>
          <Button
            disabled={loading}
            onClick={onSubmit}
            className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030]"
          >
            {submitText}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

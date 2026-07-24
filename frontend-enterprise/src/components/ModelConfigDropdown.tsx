/**
 * @file ModelConfigDropdown.tsx
 * @module components/ModelConfigDropdown
 * @description 模型配置下拉选择器组件。以按钮触发的下拉菜单形式展示可用模型列表，
 *              支持自定义按钮样式、对齐方式、占位文本等。
 */

import { CheckOutlined } from '../icons';
import IconChevronDown from '../assets/icons/chevron-down.svg?react';
import type { ModelConfigRead } from '../types';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { MENU_CONTENT_CLASS, MENU_ITEM_CLASS } from '@/lib/enterprise-ui';
import { cn } from '@/lib/utils';

/** 默认模型选择按钮样式 */
const DEFAULT_MODEL_BUTTON_CLASS =
  'h-8 max-w-[220px] gap-1 rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-4 text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6]! hover:bg-white! hover:text-[#18181a]! aria-expanded:border-[#cbd3e6]! aria-expanded:bg-white! aria-expanded:text-[#18181a]!';

/**
 * 模型配置下拉选择器的属性定义。
 */
type ModelConfigDropdownProps = {
  /** 可选模型列表。 */
  models: ModelConfigRead[];
  /** 当前选中的模型 ID。 */
  value: string;
  /** 模型变更回调。 */
  onChange: (modelId: string) => void;
  /** 是否禁用。 */
  disabled?: boolean;
  /** 按钮的自定义 CSS 类名。 */
  buttonClassName?: string;
  /** 下拉菜单对齐方式。 */
  align?: 'start' | 'center' | 'end';
  /** 占位文本。 */
  placeholder?: string;
};

/**
 * 模型配置下拉选择器组件。
 * 显示当前选中的模型名称，点击展开下拉菜单选择其他模型。
 * 当前选中的模型会显示勾选标记。
 *
 * @param props - 组件属性
 * @param props.models - 可选模型列表
 * @param props.value - 当前选中模型 ID
 * @param props.onChange - 模型变更回调
 * @param props.disabled - 是否禁用，默认 false
 * @param props.buttonClassName - 按钮自定义类名
 * @param props.align - 菜单对齐方式，默认 end
 * @param props.placeholder - 占位文本，默认"默认模型"
 * @returns 渲染好的模型选择下拉组件
 */
export function ModelConfigDropdown({
  models,
  value,
  onChange,
  disabled = false,
  buttonClassName,
  align = 'end',
  placeholder = '默认模型',
}: ModelConfigDropdownProps) {
  // 查找当前选中的模型
  const selected = models.find((item) => item.id === value) || null;
  // 显示标签：优先模型名称，其次模型标识，最后占位文本
  const label = selected?.name || selected?.model || placeholder;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <UIButton
          variant="outline"
          disabled={disabled || models.length === 0}
          className={cn(DEFAULT_MODEL_BUTTON_CLASS, buttonClassName)}
          title={label}
        >
          <span className="min-w-0 truncate">{label}</span>
          <IconChevronDown className="size-[12px] shrink-0" />
        </UIButton>
      </DropdownMenuTrigger>
      <DropdownMenuContent align={align} className={MENU_CONTENT_CLASS}>
        {models.length === 0 ? (
          // 无可用模型时的占位项
          <DropdownMenuItem disabled className={MENU_ITEM_CLASS}>
            暂无可用模型
          </DropdownMenuItem>
        ) : (
          models.map((model) => (
            <DropdownMenuItem
              key={model.id}
              className={MENU_ITEM_CLASS}
              onSelect={() => onChange(model.id)}
            >
              <span className="flex min-w-0 flex-1 flex-col">
                <strong className="truncate text-[13px] text-foreground">{model.name || model.model}</strong>
                <em className="truncate text-[11px] not-italic text-[#858b9c]">
                  {/* 默认模型显示"· 默认"标记 */}
                  {model.is_default ? `${model.model} · 默认` : model.model}
                </em>
              </span>
              {/* 当前选中模型显示勾选标记 */}
              {value === model.id && <CheckOutlined />}
            </DropdownMenuItem>
          ))
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

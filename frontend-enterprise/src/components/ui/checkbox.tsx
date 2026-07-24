/**
 * @file checkbox.tsx
 * @description 复选框组件，基于 Radix UI 的 Checkbox 原语封装。
 *              支持选中/未选中/不确定状态，带有焦点环和错误状态样式。
 *              本项目中作为 shadcn/ui 基础组件，在表单、列表选择等场景中使用。
 */

import * as React from "react"
import { Checkbox as CheckboxPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"
import { CheckIcon } from "lucide-react"

/**
 * 复选框组件。
 * @param className - 额外的自定义类名，与默认样式合并。
 * @param props - 透传给 Radix Checkbox Root 的属性（如 checked、onCheckedChange 等）。
 * @returns 渲染一个带勾选图标的复选框，选中时背景变为主色调。
 */
function Checkbox({
  className,
  ...props
}: React.ComponentProps<typeof CheckboxPrimitive.Root>) {
  return (
    <CheckboxPrimitive.Root
      data-slot="checkbox"
      className={cn(
        "peer relative flex size-4 shrink-0 items-center justify-center rounded-[4px] border border-input transition-colors outline-none group-has-disabled/field:opacity-50 after:absolute after:-inset-x-3 after:-inset-y-2 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 aria-invalid:aria-checked:border-primary data-checked:border-primary data-checked:bg-primary data-checked:text-primary-foreground",
        className
      )}
      {...props}
    >
      {/* 选中状态指示器，使用对勾图标 */}
      <CheckboxPrimitive.Indicator
        data-slot="checkbox-indicator"
        className="grid place-content-center text-current transition-none [&>svg]:size-3.5"
      >
        <CheckIcon
        />
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  )
}

export { Checkbox }

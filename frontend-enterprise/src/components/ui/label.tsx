/**
 * @file label.tsx
 * @description 标签组件，基于 Radix UI 的 Label 原语封装。
 *              用于为表单元素提供可访问的文本标签，支持与表单控件的关联。
 *              本项目中作为 shadcn/ui 基础组件，在表单场景中标注字段名称。
 */

import * as React from "react"
import { Label as LabelPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

/**
 * 标签组件。
 * @param className - 额外的自定义类名，与默认样式合并。
 * @param props - 透传给 Radix Label Root 的属性（如 htmlFor 用于关联表单控件）。
 * @returns 渲染一个中等字重的标签文本，支持 disabled 状态样式联动。
 */
function Label({
  className,
  ...props
}: React.ComponentProps<typeof LabelPrimitive.Root>) {
  return (
    <LabelPrimitive.Root
      data-slot="label"
      className={cn(
        "flex items-center gap-2 text-sm leading-none font-medium select-none group-data-[disabled=true]:pointer-events-none group-data-[disabled=true]:opacity-50 peer-disabled:cursor-not-allowed peer-disabled:opacity-50",
        className
      )}
      {...props}
    />
  )
}

export { Label }

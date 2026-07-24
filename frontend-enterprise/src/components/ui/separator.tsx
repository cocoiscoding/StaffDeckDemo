/**
 * @file separator.tsx
 * @description 分隔线组件，基于 Radix UI 的 Separator 原语封装。
 *              支持水平和垂直两种方向，用于在视觉上分隔内容区块。
 *              本项目中作为 shadcn/ui 基础组件，在菜单、卡片、表单等场景中使用。
 */

"use client"

import * as React from "react"
import { Separator as SeparatorPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

/**
 * 分隔线组件。
 * @param className - 额外的自定义类名。
 * @param orientation - 方向，"horizontal"（水平，默认）或 "vertical"（垂直）。
 * @param decorative - 是否为纯装饰（非语义化），默认为 true。
 * @param props - 透传给 Radix Separator Root 的属性。
 * @returns 渲染一条水平或垂直的分隔线。
 */
function Separator({
  className,
  orientation = "horizontal",
  decorative = true,
  ...props
}: React.ComponentProps<typeof SeparatorPrimitive.Root>) {
  return (
    <SeparatorPrimitive.Root
      data-slot="separator"
      decorative={decorative}
      orientation={orientation}
      className={cn(
        "shrink-0 bg-border data-horizontal:h-px data-horizontal:w-full data-vertical:w-px data-vertical:self-stretch",
        className
      )}
      {...props}
    />
  )
}

export { Separator }

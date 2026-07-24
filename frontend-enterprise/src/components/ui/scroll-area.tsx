/**
 * @file scroll-area.tsx
 * @description 自定义滚动区域组件，基于 Radix UI 的 ScrollArea 原语封装。
 *              提供自定义样式的滚动条（替代浏览器原生滚动条），支持垂直和水平方向。
 *              本项目中作为 shadcn/ui 基础组件，用于需要自定义滚动条样式的容器。
 */

import * as React from "react"
import { ScrollArea as ScrollAreaPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

/**
 * 滚动区域容器。
 * @param className - 额外的自定义类名。
 * @param children - 可滚动区域的内容。
 * @param props - 透传给 Radix ScrollArea Root 的属性。
 * @returns 渲染一个带自定义滚动条的滚动容器（含垂直滚动条和角落组件）。
 */
function ScrollArea({
  className,
  children,
  ...props
}: React.ComponentProps<typeof ScrollAreaPrimitive.Root>) {
  return (
    <ScrollAreaPrimitive.Root
      data-slot="scroll-area"
      className={cn("relative", className)}
      {...props}
    >
      {/* 视口区域，内容在此滚动 */}
      <ScrollAreaPrimitive.Viewport
        data-slot="scroll-area-viewport"
        className="size-full rounded-[inherit] transition-[color,box-shadow] outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-1"
      >
        {children}
      </ScrollAreaPrimitive.Viewport>
      {/* 垂直滚动条 */}
      <ScrollBar />
      {/* 滚动区域角落（水平和垂直滚动条交汇处） */}
      <ScrollAreaPrimitive.Corner />
    </ScrollAreaPrimitive.Root>
  )
}

/**
 * 自定义滚动条。
 * @param className - 额外的自定义类名。
 * @param orientation - 滚动条方向，"vertical"（垂直，默认）或 "horizontal"（水平）。
 * @param props - 透传给 Radix ScrollArea Scrollbar 的属性。
 * @returns 渲染一个自定义样式的滚动条（含可拖动的滑块 thumb）。
 */
function ScrollBar({
  className,
  orientation = "vertical",
  ...props
}: React.ComponentProps<typeof ScrollAreaPrimitive.ScrollAreaScrollbar>) {
  return (
    <ScrollAreaPrimitive.ScrollAreaScrollbar
      data-slot="scroll-area-scrollbar"
      data-orientation={orientation}
      orientation={orientation}
      className={cn(
        "flex touch-none p-px transition-colors select-none data-horizontal:h-2.5 data-horizontal:flex-col data-horizontal:border-t data-horizontal:border-t-transparent data-vertical:h-full data-vertical:w-2.5 data-vertical:border-l data-vertical:border-l-transparent",
        className
      )}
      {...props}
    >
      {/* 可拖动的滑块 */}
      <ScrollAreaPrimitive.ScrollAreaThumb
        data-slot="scroll-area-thumb"
        className="relative flex-1 rounded-full bg-border"
      />
    </ScrollAreaPrimitive.ScrollAreaScrollbar>
  )
}

export { ScrollArea, ScrollBar }

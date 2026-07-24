/**
 * @file popover.tsx
 * @description 气泡卡片（弹出层）组件，基于 Radix UI 的 Popover 原语封装。
 *              点击触发器后在旁边弹出浮动面板，与 Tooltip 的区别是 Popover 内容可交互。
 *              本项目中作为 shadcn/ui 基础组件，用于筛选器、颜色选择器、确认弹窗等场景。
 */

"use client"

import * as React from "react"
import { Popover as PopoverPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

/**
 * 气泡卡片根容器，控制弹出层的显示/隐藏状态。
 * @param props - 透传给 Radix Popover Root 的属性。
 * @returns 渲染一个 Popover 根节点。
 */
function Popover({
  ...props
}: React.ComponentProps<typeof PopoverPrimitive.Root>) {
  return <PopoverPrimitive.Root data-slot="popover" {...props} />
}

/**
 * 气泡卡片触发器，点击后弹出内容。
 * @param props - 透传给 Radix Popover Trigger 的属性。
 * @returns 渲染一个触发元素。
 */
function PopoverTrigger({
  ...props
}: React.ComponentProps<typeof PopoverPrimitive.Trigger>) {
  return <PopoverPrimitive.Trigger data-slot="popover-trigger" {...props} />
}

/**
 * 气泡卡片内容容器。
 * @param className - 额外的自定义类名。
 * @param align - 水平对齐方式，默认为 "center"（居中）。
 * @param sideOffset - 与触发器的间距，默认为 4px。
 * @param props - 透传给 Radix Popover Content 的属性。
 * @returns 渲染浮动面板，带圆角、阴影和滑入动画。
 */
function PopoverContent({
  className,
  align = "center",
  sideOffset = 4,
  ...props
}: React.ComponentProps<typeof PopoverPrimitive.Content>) {
  return (
    <PopoverPrimitive.Portal>
      <PopoverPrimitive.Content
        data-slot="popover-content"
        align={align}
        sideOffset={sideOffset}
        className={cn(
          "z-50 flex w-72 origin-(--radix-popover-content-transform-origin) flex-col gap-2.5 rounded-lg bg-popover p-2.5 text-sm text-popover-foreground shadow-md ring-1 ring-foreground/10 outline-hidden duration-100 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95",
          className
        )}
        {...props}
      />
    </PopoverPrimitive.Portal>
  )
}

/**
 * 锚点元素，用于自定义气泡卡片的定位参考点。
 * @param props - 透传给 Radix Popover Anchor 的属性。
 * @returns 渲染一个锚点元素。
 */
function PopoverAnchor({
  ...props
}: React.ComponentProps<typeof PopoverPrimitive.Anchor>) {
  return <PopoverPrimitive.Anchor data-slot="popover-anchor" {...props} />
}

/**
 * 气泡卡片箭头，指向触发器方向。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 Radix Popover Arrow 的属性。
 * @returns 渲染一个三角形箭头。
 */
function PopoverArrow({
  className,
  ...props
}: React.ComponentProps<typeof PopoverPrimitive.Arrow>) {
  return (
    <PopoverPrimitive.Arrow
      data-slot="popover-arrow"
      className={cn("fill-popover", className)}
      {...props}
    />
  )
}

/**
 * 气泡卡片头部区域，包含标题和描述。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染头部垂直布局容器。
 */
function PopoverHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="popover-header"
      className={cn("flex flex-col gap-0.5 text-sm", className)}
      {...props}
    />
  )
}

/**
 * 气泡卡片标题。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 h2 元素的属性。
 * @returns 渲染中等字重的标题文本。
 */
function PopoverTitle({ className, ...props }: React.ComponentProps<"h2">) {
  return (
    <div
      data-slot="popover-title"
      className={cn("font-medium", className)}
      {...props}
    />
  )
}

/**
 * 气泡卡片描述文本。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 p 元素的属性。
 * @returns 渲染灰色辅助描述文本。
 */
function PopoverDescription({
  className,
  ...props
}: React.ComponentProps<"p">) {
  return (
    <p
      data-slot="popover-description"
      className={cn("text-muted-foreground", className)}
      {...props}
    />
  )
}

export {
  Popover,
  PopoverAnchor,
  PopoverArrow,
  PopoverContent,
  PopoverDescription,
  PopoverHeader,
  PopoverTitle,
  PopoverTrigger,
}

/**
 * @file hover-card.tsx
 * @description 悬停卡片组件，基于 Radix UI 的 HoverCard 原语封装。
 *              当鼠标悬停在触发元素上时，展示一个浮动卡片，移开后自动收起。
 *              适用于用户信息预览、链接摘要、提示详情等场景。
 *              本项目中作为 shadcn/ui 基础组件使用。
 */

import * as React from "react"
import { HoverCard as HoverCardPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

/**
 * 悬停卡片根容器，控制卡片的显示/隐藏状态。
 * @param props - 透传给 Radix HoverCard Root 的属性（如 open、onOpenChange 等）。
 * @returns 渲染一个 HoverCard 根节点。
 */
function HoverCard({
  ...props
}: React.ComponentProps<typeof HoverCardPrimitive.Root>) {
  return <HoverCardPrimitive.Root data-slot="hover-card" {...props} />
}

/**
 * 悬停卡片触发器，鼠标悬停在其上时显示卡片。
 * @param props - 透传给 Radix HoverCard Trigger 的属性。
 * @returns 渲染一个触发元素。
 */
function HoverCardTrigger({
  ...props
}: React.ComponentProps<typeof HoverCardPrimitive.Trigger>) {
  return <HoverCardPrimitive.Trigger data-slot="hover-card-trigger" {...props} />
}

/**
 * 悬停卡片内容容器。
 * @param className - 额外的自定义类名。
 * @param align - 水平对齐方式，默认为 "center"（居中对齐）。
 * @param sideOffset - 与触发器的间距，默认为 4px。
 * @param props - 透传给 Radix HoverCard Content 的属性。
 * @returns 渲染浮动卡片面板，带圆角、阴影和滑入动画。
 */
function HoverCardContent({
  className,
  align = "center",
  sideOffset = 4,
  ...props
}: React.ComponentProps<typeof HoverCardPrimitive.Content>) {
  return (
    <HoverCardPrimitive.Portal>
      <HoverCardPrimitive.Content
        data-slot="hover-card-content"
        align={align}
        sideOffset={sideOffset}
        className={cn(
          "z-50 w-64 origin-(--radix-hover-card-content-transform-origin) rounded-lg bg-popover p-2 text-popover-foreground shadow-md ring-1 ring-foreground/10 outline-hidden duration-100 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95",
          className
        )}
        {...props}
      />
    </HoverCardPrimitive.Portal>
  )
}

export { HoverCard, HoverCardTrigger, HoverCardContent }

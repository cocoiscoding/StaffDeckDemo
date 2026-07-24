/**
 * @file tooltip.tsx
 * @description 文字提示（气泡提示）组件，基于 Radix UI 的 Tooltip 原语封装。
 *              当鼠标悬停在触发元素上时，显示简短的辅助提示文本。
 *              与 Popover 的区别：Tooltip 仅用于展示不可交互的提示信息。
 *              本项目中作为 shadcn/ui 基础组件，在图标按钮、链接等元素上提供悬停说明。
 */

import * as React from "react"
import { Tooltip as TooltipPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

/**
 * Tooltip 提供者，配置全局 Tooltip 行为（如延迟时间）。
 * @param delayDuration - 悬停后显示提示的延迟时间（毫秒），默认为 0（立即显示）。
 * @param props - 透传给 Radix Tooltip Provider 的属性。
 * @returns 渲染一个 Tooltip Provider。
 */
function TooltipProvider({
  delayDuration = 0,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Provider>) {
  return (
    <TooltipPrimitive.Provider
      data-slot="tooltip-provider"
      delayDuration={delayDuration}
      {...props}
    />
  )
}

/**
 * Tooltip 根容器，控制提示的显示/隐藏状态。
 * @param props - 透传给 Radix Tooltip Root 的属性。
 * @returns 渲染一个 Tooltip 根节点。
 */
function Tooltip({
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Root>) {
  return <TooltipPrimitive.Root data-slot="tooltip" {...props} />
}

/**
 * Tooltip 触发器，鼠标悬停在其上时显示提示。
 * @param props - 透传给 Radix Tooltip Trigger 的属性。
 * @returns 渲染一个触发元素。
 */
function TooltipTrigger({
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Trigger>) {
  return <TooltipPrimitive.Trigger data-slot="tooltip-trigger" {...props} />
}

/**
 * Tooltip 内容容器。
 * @param className - 额外的自定义类名。
 * @param sideOffset - 与触发器的间距，默认为 0。
 * @param children - 提示文本内容。
 * @param props - 透传给 Radix Tooltip Content 的属性。
 * @returns 渲染深色背景的浮动提示气泡，底部带三角箭头和淡入缩放动画。
 */
function TooltipContent({
  className,
  sideOffset = 0,
  children,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Content>) {
  return (
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Content
        data-slot="tooltip-content"
        sideOffset={sideOffset}
        className={cn(
          "z-50 inline-flex w-fit max-w-xs origin-(--radix-tooltip-content-transform-origin) items-center gap-1.5 rounded-md bg-foreground px-3 py-1.5 text-xs text-background has-data-[slot=kbd]:pr-1.5 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 **:data-[slot=kbd]:relative **:data-[slot=kbd]:isolate **:data-[slot=kbd]:z-50 **:data-[slot=kbd]:rounded-sm data-[state=delayed-open]:animate-in data-[state=delayed-open]:fade-in-0 data-[state=delayed-open]:zoom-in-95 data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95",
          className
        )}
        {...props}
      >
        {children}
        {/* 三角箭头，指向触发器方向 */}
        <TooltipPrimitive.Arrow className="z-50 size-2.5 translate-y-[calc(-50%_-_2px)] rotate-45 rounded-[2px] bg-foreground fill-foreground" />
      </TooltipPrimitive.Content>
    </TooltipPrimitive.Portal>
  )
}

export { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger }

/**
 * @file accordion.tsx
 * @description 手风琴（折叠面板）组件，基于 Radix UI 的 Accordion 原语封装。
 *              用于在垂直方向上展开/折叠内容区域，适合 FAQ、设置分组等场景。
 *              本项目中作为 shadcn/ui 基础组件供业务页面使用。
 */

import * as React from "react"
import { Accordion as AccordionPrimitive } from "radix-ui"
import { ChevronDownIcon } from "lucide-react"

import { cn } from "@/lib/utils"

/**
 * 手风琴容器组件，包裹所有可折叠项。
 * @param props - 透传给 Radix Accordion Root 的属性（如 type、collapsible、value 等）。
 * @returns 渲染一个手风琴根容器。
 */
function Accordion({
  ...props
}: React.ComponentProps<typeof AccordionPrimitive.Root>) {
  return <AccordionPrimitive.Root data-slot="accordion" {...props} />
}

/**
 * 手风琴单项，每个 Item 包含一个 Trigger（触发器）和一个 Content（内容区）。
 * @param className - 额外的自定义类名，与默认样式合并。
 * @param props - 透传给 Radix Accordion Item 的属性。
 * @returns 渲染一个带有底部边框（最后一项除外）的可折叠项。
 */
function AccordionItem({
  className,
  ...props
}: React.ComponentProps<typeof AccordionPrimitive.Item>) {
  return (
    <AccordionPrimitive.Item
      data-slot="accordion-item"
      className={cn("border-b last:border-b-0", className)}
      {...props}
    />
  )
}

/**
 * 手风琴触发器，点击后展开或折叠对应的 Content。
 * @param className - 额外的自定义类名。
 * @param children - 触发器中显示的文本内容。
 * @param props - 透传给 Radix Accordion Trigger 的属性。
 * @returns 渲染一个带下拉箭头图标的触发按钮，展开时箭头旋转 180°。
 */
function AccordionTrigger({
  className,
  children,
  ...props
}: React.ComponentProps<typeof AccordionPrimitive.Trigger>) {
  return (
    <AccordionPrimitive.Header className="flex">
      <AccordionPrimitive.Trigger
        data-slot="accordion-trigger"
        className={cn(
          "flex flex-1 items-start justify-between gap-4 rounded-md py-4 text-left text-sm font-medium outline-none transition-all hover:underline focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:opacity-50 [&[data-open]>svg]:rotate-180",
          className
        )}
        {...props}
      >
        {children}
        {/* 下拉箭头图标，展开时通过 CSS 旋转动画指示状态 */}
        <ChevronDownIcon className="pointer-events-none size-4 shrink-0 translate-y-0.5 text-muted-foreground transition-transform duration-200" />
      </AccordionPrimitive.Trigger>
    </AccordionPrimitive.Header>
  )
}

/**
 * 手风琴内容区，在 Trigger 被激活时展开显示。
 * @param className - 内容区内层容器的额外自定义类名。
 * @param children - 折叠面板展开后显示的内容。
 * @param props - 透传给 Radix Accordion Content 的属性。
 * @returns 渲染一个带展开/折叠动画的内容容器。
 */
function AccordionContent({
  className,
  children,
  ...props
}: React.ComponentProps<typeof AccordionPrimitive.Content>) {
  return (
    <AccordionPrimitive.Content
      data-slot="accordion-content"
      className="overflow-hidden text-sm data-closed:animate-accordion-up data-open:animate-accordion-down"
      {...props}
    >
      <div className={cn("pt-0 pb-4", className)}>{children}</div>
    </AccordionPrimitive.Content>
  )
}

export { Accordion, AccordionItem, AccordionTrigger, AccordionContent }

/**
 * @file tabs.tsx
 * @description 选项卡（标签页）组件，基于 Radix UI 的 Tabs 原语封装。
 *              使用 class-variance-authority (cva) 管理选项卡列表的样式变体（default / line）。
 *              支持水平和垂直两种排列方向。
 *              本项目中作为 shadcn/ui 基础组件，在内容分组切换、面板切换等场景中使用。
 */

"use client"

import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { Tabs as TabsPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

/**
 * 选项卡根容器，管理当前激活的标签页。
 * @param className - 额外的自定义类名。
 * @param orientation - 排列方向，"horizontal"（水平，默认）或 "vertical"（垂直）。
 * @param props - 透传给 Radix Tabs Root 的属性（如 value、onValueChange 等）。
 * @returns 渲染一个选项卡根容器，水平方向使用 flex-col 布局。
 */
function Tabs({
  className,
  orientation = "horizontal",
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Root>) {
  return (
    <TabsPrimitive.Root
      data-slot="tabs"
      data-orientation={orientation}
      className={cn(
        "group/tabs flex gap-2 data-horizontal:flex-col",
        className
      )}
      {...props}
    />
  )
}

// 选项卡列表样式变体定义
const tabsListVariants = cva(
  "group/tabs-list inline-flex w-fit items-center justify-center rounded-lg p-[3px] text-muted-foreground group-data-horizontal/tabs:h-8 group-data-vertical/tabs:h-fit group-data-vertical/tabs:flex-col data-[variant=line]:rounded-none",
  {
    variants: {
      variant: {
        // 默认变体：灰色背景
        default: "bg-muted",
        // 线条变体：透明背景，下方带激活指示线
        line: "gap-1 bg-transparent",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

/**
 * 选项卡列表容器，包裹所有标签页触发器。
 * @param className - 额外的自定义类名。
 * @param variant - 变体，"default"（灰色背景，默认）或 "line"（下划线样式）。
 * @param props - 透传给 Radix Tabs List 的属性。
 * @returns 渲染选项卡列表条，根据方向自适应布局。
 */
function TabsList({
  className,
  variant = "default",
  ...props
}: React.ComponentProps<typeof TabsPrimitive.List> &
  VariantProps<typeof tabsListVariants>) {
  return (
    <TabsPrimitive.List
      data-slot="tabs-list"
      data-variant={variant}
      className={cn(tabsListVariants({ variant }), className)}
      {...props}
    />
  )
}

/**
 * 选项卡触发器（单个标签按钮），点击后切换到对应面板。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 Radix Tabs Trigger 的属性（如 value 等）。
 * @returns 渲染一个标签按钮，激活时高亮显示，line 变体下显示底部指示线。
 */
function TabsTrigger({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      data-slot="tabs-trigger"
      className={cn(
        "relative inline-flex h-[calc(100%-1px)] flex-1 items-center justify-center gap-1.5 rounded-md border border-transparent px-1.5 py-0.5 text-sm font-medium whitespace-nowrap text-foreground/60 transition-all group-data-vertical/tabs:w-full group-data-vertical/tabs:justify-start hover:text-foreground focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-1 focus-visible:outline-ring disabled:pointer-events-none disabled:opacity-50 has-data-[icon=inline-end]:pr-1 has-data-[icon=inline-start]:pl-1 group-data-[variant=default]/tabs-list:data-active:shadow-sm group-data-[variant=line]/tabs-list:data-active:shadow-none [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
        // line 变体下背景透明
        "group-data-[variant=line]/tabs-list:bg-transparent group-data-[variant=line]/tabs-list:data-active:bg-transparent",
        // 激活时背景变白
        "data-active:bg-background data-active:text-foreground",
        // 底部/侧边指示线（line 变体激活时显示）
        "after:absolute after:bg-foreground after:opacity-0 after:transition-opacity group-data-horizontal/tabs:after:inset-x-0 group-data-horizontal/tabs:after:bottom-[-5px] group-data-horizontal/tabs:after:h-0.5 group-data-vertical/tabs:after:inset-y-0 group-data-vertical/tabs:after:-right-1 group-data-vertical/tabs:after:w-0.5 group-data-[variant=line]/tabs-list:data-active:after:opacity-100",
        className
      )}
      {...props}
    />
  )
}

/**
 * 选项卡内容面板，仅在对应标签激活时显示。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 Radix Tabs Content 的属性。
 * @returns 渲染选项卡的内容区域。
 */
function TabsContent({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return (
    <TabsPrimitive.Content
      data-slot="tabs-content"
      className={cn("flex-1 text-sm outline-none", className)}
      {...props}
    />
  )
}

export { Tabs, TabsList, TabsTrigger, TabsContent, tabsListVariants }

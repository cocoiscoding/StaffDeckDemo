/**
 * @file card.tsx
 * @description 卡片组件，由多个子组件组合而成（Card / CardHeader / CardTitle / CardDescription /
 *              CardAction / CardContent / CardFooter）。
 *              用于在页面中展示分组信息，如数据展示、设置面板、用户信息等。
 *              本项目中作为 shadcn/ui 基础组件，广泛用于各类内容展示场景。
 */

import * as React from "react"

import { cn } from "@/lib/utils"

/**
 * 卡片容器，是所有卡片子组件的根容器。
 * @param className - 额外的自定义类名。
 * @param size - 卡片间距尺寸，"default"（间距大）或 "sm"（间距小），通过 CSS 变量控制内边距。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染一个带圆角、阴影和内边距的卡片容器。
 */
function Card({
  className,
  size = "default",
  ...props
}: React.ComponentProps<"div"> & { size?: "default" | "sm" }) {
  return (
    <div
      data-slot="card"
      data-size={size}
      className={cn(
        "group/card flex flex-col gap-(--card-spacing) overflow-hidden rounded-xl bg-card py-(--card-spacing) text-sm text-card-foreground ring-1 ring-foreground/10 [--card-spacing:--spacing(4)] has-data-[slot=card-footer]:pb-0 has-[>img:first-child]:pt-0 data-[size=sm]:[--card-spacing:--spacing(3)] data-[size=sm]:has-data-[slot=card-footer]:pb-0 *:[img:first-child]:rounded-t-xl *:[img:last-child]:rounded-b-xl",
        className
      )}
      {...props}
    />
  )
}

/**
 * 卡片头部区域，包含标题、描述和可选的操作区。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染卡片顶部网格布局区域，自动适配是否含操作按钮。
 */
function CardHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-header"
      className={cn(
        "group/card-header @container/card-header grid auto-rows-min items-start gap-1 rounded-t-xl px-(--card-spacing) has-data-[slot=card-action]:grid-cols-[1fr_auto] has-data-[slot=card-description]:grid-rows-[auto_auto] [.border-b]:pb-(--card-spacing)",
        className
      )}
      {...props}
    />
  )
}

/**
 * 卡片标题文本。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染使用标题字体的标题文本。
 */
function CardTitle({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-title"
      className={cn(
        "font-heading text-base leading-snug font-medium group-data-[size=sm]/card:text-sm",
        className
      )}
      {...props}
    />
  )
}

/**
 * 卡片描述文本，位于标题下方。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染灰色辅助描述文本。
 */
function CardDescription({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-description"
      className={cn("text-sm text-muted-foreground", className)}
      {...props}
    />
  )
}

/**
 * 卡片头部操作区，定位在头部右侧。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染定位在卡片头右上角的操作容器。
 */
function CardAction({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-action"
      className={cn(
        "col-start-2 row-span-2 row-start-1 self-start justify-self-end",
        className
      )}
      {...props}
    />
  )
}

/**
 * 卡片主体内容区。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染卡片的主要内容容器。
 */
function CardContent({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-content"
      className={cn("px-(--card-spacing)", className)}
      {...props}
    />
  )
}

/**
 * 卡片底部区域，通常放置操作按钮。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染带顶部分隔线的卡片底部区域。
 */
function CardFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-footer"
      className={cn(
        "flex items-center rounded-b-xl border-t bg-muted/50 p-(--card-spacing)",
        className
      )}
      {...props}
    />
  )
}

export {
  Card,
  CardHeader,
  CardFooter,
  CardTitle,
  CardAction,
  CardDescription,
  CardContent,
}

/**
 * @file alert.tsx
 * @description 警告/提示横幅组件，使用 class-variance-authority (cva) 管理样式变体。
 *              用于在页面内联展示重要信息（如操作成功、错误提示等）。
 *              本项目中作为 shadcn/ui 基础组件，供表单反馈、状态提示等场景使用。
 */

import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

// 警告横幅样式变体定义
const alertVariants = cva(
  "group/alert relative grid w-full gap-0.5 rounded-lg border px-2.5 py-2 text-left text-sm has-data-[slot=alert-action]:relative has-data-[slot=alert-action]:pr-18 has-[>svg]:grid-cols-[auto_1fr] has-[>svg]:gap-x-2 *:[svg]:row-span-2 *:[svg]:translate-y-0.5 *:[svg]:text-current *:[svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        // 默认变体：使用卡片背景色
        default: "bg-card text-card-foreground",
        // 危险变体：使用红色主题，用于错误提示
        destructive:
          "bg-card text-destructive *:data-[slot=alert-description]:text-destructive/90 *:[svg]:text-current",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

/**
 * 警告横幅主组件。
 * @param className - 额外的自定义类名，与变体样式合并。
 * @param variant - 视觉变体，"default"（默认）或 "destructive"（危险/错误）。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染一个带有 role="alert" 的提示横幅。
 */
function Alert({
  className,
  variant,
  ...props
}: React.ComponentProps<"div"> & VariantProps<typeof alertVariants>) {
  return (
    <div
      data-slot="alert"
      role="alert"
      className={cn(alertVariants({ variant }), className)}
      {...props}
    />
  )
}

/**
 * 警告横幅标题。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染中等字重的标题文本。
 */
function AlertTitle({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="alert-title"
      className={cn(
        "font-medium group-has-[>svg]/alert:col-start-2 [&_a]:underline [&_a]:underline-offset-3 [&_a]:hover:text-foreground",
        className
      )}
      {...props}
    />
  )
}

/**
 * 警告横幅描述文本。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染灰色辅助描述文本。
 */
function AlertDescription({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="alert-description"
      className={cn(
        "text-sm text-balance text-muted-foreground md:text-pretty [&_a]:underline [&_a]:underline-offset-3 [&_a]:hover:text-foreground [&_p:not(:last-child)]:mb-4",
        className
      )}
      {...props}
    />
  )
}

/**
 * 警告横幅操作区，用于放置按钮等交互元素。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染定位在右上角的操作容器。
 */
function AlertAction({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="alert-action"
      className={cn("absolute top-2 right-2", className)}
      {...props}
    />
  )
}

export { Alert, AlertTitle, AlertDescription, AlertAction }

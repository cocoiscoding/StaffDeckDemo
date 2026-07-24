/**
 * @file badge.tsx
 * @description 徽章/标签组件，使用 class-variance-authority (cva) 管理样式变体。
 *              用于展示状态标记、计数、分类标签等小型信息。
 *              本项目中作为 shadcn/ui 基础组件，在列表项、卡片等场景中显示状态。
 */

import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { Slot } from "radix-ui"

import { cn } from "@/lib/utils"

// 徽章样式变体定义，基础样式包含圆角、固定高度、内边距、文本大小等
const badgeVariants = cva(
  "group/badge inline-flex h-5 w-fit shrink-0 items-center justify-center gap-1 overflow-hidden rounded-4xl border border-transparent px-2 py-0.5 text-xs font-medium whitespace-nowrap transition-all focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 has-data-[icon=inline-end]:pr-1.5 has-data-[icon=inline-start]:pl-1.5 aria-invalid:border-destructive aria-invalid:ring-destructive/20 [&>svg]:pointer-events-none [&>svg]:size-3!",
  {
    variants: {
      variant: {
        // 主色调（默认）
        default: "bg-primary text-primary-foreground [a]:hover:bg-primary/80",
        // 次要色调
        secondary:
          "bg-secondary text-secondary-foreground [a]:hover:bg-secondary/80",
        // 危险/错误色调
        destructive:
          "bg-destructive/10 text-destructive focus-visible:ring-destructive/20 [a]:hover:bg-destructive/20",
        // 描边样式
        outline:
          "border-border text-foreground [a]:hover:bg-muted [a]:hover:text-muted-foreground",
        // 幽灵样式（透明背景，悬停变灰）
        ghost:
          "hover:bg-muted hover:text-muted-foreground",
        // 链接样式
        link: "text-primary underline-offset-4 hover:underline",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

/**
 * 徽章组件。
 * @param className - 额外的自定义类名，与变体样式合并。
 * @param variant - 视觉变体，可选 default / secondary / destructive / outline / ghost / link。
 * @param asChild - 是否将样式合并到子元素上（而非渲染 span），用于将徽章变为链接等场景。
 * @param props - 透传给 span（或 Slot）的属性。
 * @returns 渲染一个内联徽章元素。
 */
function Badge({
  className,
  variant = "default",
  asChild = false,
  ...props
}: React.ComponentProps<"span"> &
  VariantProps<typeof badgeVariants> & { asChild?: boolean }) {
  // asChild 为 true 时使用 Slot 将样式透传给子元素
  const Comp = asChild ? Slot.Root : "span"

  return (
    <Comp
      data-slot="badge"
      data-variant={variant}
      className={cn(badgeVariants({ variant }), className)}
      {...props}
    />
  )
}

export { Badge, badgeVariants }

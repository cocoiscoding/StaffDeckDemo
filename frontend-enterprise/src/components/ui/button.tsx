/**
 * @file button.tsx
 * @description 按钮组件，使用 class-variance-authority (cva) 管理样式变体和尺寸。
 *              支持 variant（视觉变体）和 size（尺寸）两个维度的样式组合。
 *              通过 asChild 属性支持将按钮样式合并到子元素（如链接 a 标签）上。
 *              本项目中作为 shadcn/ui 基础组件，是使用频率最高的交互元素之一。
 */

import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { Slot } from "radix-ui"

import { cn } from "@/lib/utils"

// 按钮样式变体与尺寸定义
const buttonVariants = cva(
  "group/button inline-flex shrink-0 items-center justify-center rounded-lg border border-transparent bg-clip-padding text-sm font-medium whitespace-nowrap transition-all outline-none select-none active:not-aria-[haspopup]:translate-y-px disabled:pointer-events-none disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        // 主色调（默认）：实心背景
        default: "bg-primary text-primary-foreground hover:bg-primary/80",
        // 描边样式：带边框，悬停变灰
        outline:
          "border-border bg-background hover:bg-muted hover:text-foreground aria-expanded:bg-muted aria-expanded:text-foreground",
        // 次要色调：灰色背景
        secondary:
          "bg-secondary text-secondary-foreground hover:bg-[color-mix(in_oklch,var(--secondary),var(--foreground)_5%)] aria-expanded:bg-secondary aria-expanded:text-secondary-foreground",
        // 幽灵样式：透明背景，悬停变灰
        ghost:
          "hover:bg-muted hover:text-foreground aria-expanded:bg-muted aria-expanded:text-foreground",
        // 危险样式：红色文本，浅红背景
        destructive:
          "bg-destructive/10 text-destructive hover:bg-destructive/20",
        // 链接样式：无边框，文本下划线
        link: "text-primary underline-offset-4 hover:underline",
      },
      size: {
        // 默认尺寸：高度 32px
        default:
          "h-8 gap-1.5 px-2.5 has-data-[icon=inline-end]:pr-2 has-data-[icon=inline-start]:pl-2",
        // 超小尺寸：高度 24px
        xs: "h-6 gap-1 rounded-[min(var(--radius-md),10px)] px-2 text-xs in-data-[slot=button-group]:rounded-lg has-data-[icon=inline-end]:pr-1.5 has-data-[icon=inline-start]:pl-1.5 [&_svg:not([class*='size-'])]:size-3",
        // 小尺寸：高度 28px
        sm: "h-7 gap-1 rounded-[min(var(--radius-md),12px)] px-2.5 text-[0.8rem] in-data-[slot=button-group]:rounded-lg has-data-[icon=inline-end]:pr-1.5 has-data-[icon=inline-start]:pl-1.5 [&_svg:not([class*='size-'])]:size-3.5",
        // 大尺寸：高度 36px
        lg: "h-9 gap-1.5 px-2.5 has-data-[icon=inline-end]:pr-2 has-data-[icon=inline-start]:pl-2",
        // 图标按钮（正方形，默认大小）
        icon: "size-8",
        // 超小图标按钮
        "icon-xs":
          "size-6 rounded-[min(var(--radius-md),10px)] in-data-[slot=button-group]:rounded-lg [&_svg:not([class*='size-'])]:size-3",
        // 小图标按钮
        "icon-sm":
          "size-7 rounded-[min(var(--radius-md),12px)] in-data-[slot=button-group]:rounded-lg",
        // 大图标按钮
        "icon-lg": "size-9",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

/**
 * 按钮组件，使用 forwardRef 转发 ref 引用。
 * @param className - 额外的自定义类名，与变体样式合并。
 * @param variant - 视觉变体，可选 default / outline / secondary / ghost / destructive / link。
 * @param size - 尺寸，可选 default / xs / sm / lg / icon / icon-xs / icon-sm / icon-lg。
 * @param asChild - 是否将按钮样式合并到子元素上（用于将 button 变为 a 链接等场景）。
 * @param ref - 转发给底层 DOM 元素的 ref。
 * @param props - 透传给 button（或 Slot）的属性。
 * @returns 渲染一个按钮元素。
 */
const Button = React.forwardRef<
  HTMLButtonElement,
  React.ComponentProps<"button"> &
    VariantProps<typeof buttonVariants> & {
      asChild?: boolean
    }
>(function Button(
  { className, variant = "default", size = "default", asChild = false, ...props },
  ref
) {
  // asChild 为 true 时使用 Slot 将按钮样式透传给子元素（如 <a>）
  const Comp = asChild ? Slot.Root : "button"

  return (
    <Comp
      ref={ref}
      data-slot="button"
      data-variant={variant}
      data-size={size}
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  )
})

export { Button, buttonVariants }

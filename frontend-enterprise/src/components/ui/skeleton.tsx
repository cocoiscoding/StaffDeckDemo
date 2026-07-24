/**
 * @file skeleton.tsx
 * @description 骨架屏（加载占位）组件。
 *              使用脉冲动画模拟内容加载中的效果。
 *              本项目中作为 shadcn/ui 基础组件，在数据加载完成前提供视觉占位。
 */

import { cn } from "@/lib/utils"

/**
 * 骨架屏组件。
 * @param className - 额外的自定义类名，用于控制占位区域的尺寸和形状。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染一个带脉冲动画的灰色占位块。
 */
function Skeleton({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="skeleton"
      className={cn("animate-pulse rounded-md bg-muted", className)}
      {...props}
    />
  )
}

export { Skeleton }

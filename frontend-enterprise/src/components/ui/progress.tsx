/**
 * @file progress.tsx
 * @description 进度条组件，基于 Radix UI 的 Progress 原语封装。
 *              通过 translateX 变换驱动指示器宽度来展示进度。
 *              本项目中作为 shadcn/ui 基础组件，用于文件上传、任务加载等进度展示场景。
 */

import * as React from "react"
import { Progress as ProgressPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

/**
 * 进度条组件。
 * @param className - 进度条轨道的自定义类名。
 * @param indicatorClassName - 进度条指示器（填充部分）的自定义类名。
 * @param value - 当前进度值（0-100）。
 * @param props - 透传给 Radix Progress Root 的属性。
 * @returns 渲染一个圆角进度条，指示器宽度随 value 值变化。
 */
function Progress({
  className,
  indicatorClassName,
  value,
  ...props
}: React.ComponentProps<typeof ProgressPrimitive.Root> & { indicatorClassName?: string }) {
  return (
    <ProgressPrimitive.Root
      data-slot="progress"
      className={cn(
        "relative h-2 w-full overflow-hidden rounded-full bg-primary/20",
        className
      )}
      {...props}
    >
      <ProgressPrimitive.Indicator
        data-slot="progress-indicator"
        className={cn("h-full w-full flex-1 bg-primary transition-all", indicatorClassName)}
        // 通过 translateX 控制指示器可见区域，模拟进度填充效果
        style={{ transform: `translateX(-${100 - (value || 0)}%)` }}
      />
    </ProgressPrimitive.Root>
  )
}

export { Progress }

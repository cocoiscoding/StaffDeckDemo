/**
 * @file input.tsx
 * @description 文本输入框组件。
 *              支持国际化（i18n）自动翻译 placeholder、title 和 aria-label。
 *              本项目中作为 shadcn/ui 基础组件，在所有表单输入场景中使用。
 */

import * as React from "react"

import { useI18n } from "@/i18n"
import { cn } from "@/lib/utils"

/**
 * 文本输入框组件。
 * @param className - 额外的自定义类名，与默认样式合并。
 * @param type - input 的 type 属性（如 text、email、password 等）。
 * @param props - 透传给 input 元素的属性，其中 placeholder、title、aria-label 会被自动国际化翻译。
 * @returns 渲染一个带边框、圆角的文本输入框。
 */
function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  const { t } = useI18n()
  // 对 placeholder、title 和 aria-label 进行国际化处理
  const localizedProps = {
    ...props,
    placeholder: typeof props.placeholder === "string" ? t(props.placeholder) : props.placeholder,
    title: typeof props.title === "string" ? t(props.title) : props.title,
    "aria-label": typeof props["aria-label"] === "string" ? t(props["aria-label"]) : props["aria-label"],
  }

  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        "h-8 w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 py-1 text-base transition-colors outline-none file:inline-flex file:h-6 file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-muted-foreground disabled:pointer-events-none disabled:cursor-not-allowed disabled:bg-input/50 disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 md:text-sm",
        className
      )}
      {...localizedProps}
    />
  )
}

export { Input }

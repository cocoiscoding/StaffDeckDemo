/**
 * @file textarea.tsx
 * @description 多行文本输入框组件。
 *              支持国际化（i18n）自动翻译 placeholder、title 和 aria-label。
 *              本项目中作为 shadcn/ui 基础组件，在表单大段文本输入场景中使用。
 */

import * as React from "react"

import { useI18n } from "@/i18n"
import { cn } from "@/lib/utils"

/**
 * 多行文本输入框组件。
 * @param className - 额外的自定义类名，与默认样式合并。
 * @param props - 透传给 textarea 元素的属性，其中 placeholder、title、aria-label 会被自动国际化翻译。
 * @returns 渲染一个带边框、圆角的多行文本输入框。
 */
function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  const { t } = useI18n()
  // 对 placeholder、title 和 aria-label 进行国际化处理
  const localizedProps = {
    ...props,
    placeholder: typeof props.placeholder === "string" ? t(props.placeholder) : props.placeholder,
    title: typeof props.title === "string" ? t(props.title) : props.title,
    "aria-label": typeof props["aria-label"] === "string" ? t(props["aria-label"]) : props["aria-label"],
  }

  return (
    <textarea
      data-slot="textarea"
      className={cn(
        "flex field-sizing-fixed min-h-16 w-full overflow-y-auto rounded-lg border border-input bg-transparent px-2.5 py-2 text-base transition-colors outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:bg-input/50 disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 md:text-sm",
        className
      )}
      {...localizedProps}
    />
  )
}

export { Textarea }

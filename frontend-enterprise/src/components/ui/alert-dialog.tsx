/**
 * @file alert-dialog.tsx
 * @description 警告对话框组件，基于 Radix UI 的 AlertDialog 原语封装。
 *              与普通 Dialog 的区别：警告对话框要求用户明确确认或取消，
 *              不支持点击遮罩或按 Esc 关闭（Esc 被拦截），适用于删除等不可逆操作。
 *              本项目中作为 shadcn/ui 基础组件，在需要用户二次确认的场景中使用。
 */

"use client"

import * as React from "react"
import { AlertDialog as AlertDialogPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"

/**
 * 警告对话框根容器，控制对话框的打开/关闭状态。
 * @param props - 透传给 Radix AlertDialog Root 的属性（如 open、onOpenChange 等）。
 * @returns 渲染一个 AlertDialog 根节点。
 */
function AlertDialog({
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Root>) {
  return <AlertDialogPrimitive.Root data-slot="alert-dialog" {...props} />
}

/**
 * 对话框触发器，点击后打开对话框。
 * @param props - 透传给 Radix AlertDialog Trigger 的属性。
 * @returns 渲染一个触发按钮元素。
 */
function AlertDialogTrigger({
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Trigger>) {
  return (
    <AlertDialogPrimitive.Trigger data-slot="alert-dialog-trigger" {...props} />
  )
}

/**
 * Portal 传送门，将对话框内容渲染到 document.body 下，避免层叠上下文问题。
 * @param props - 透传给 Radix AlertDialog Portal 的属性。
 * @returns 渲染一个 Portal 容器。
 */
function AlertDialogPortal({
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Portal>) {
  return (
    <AlertDialogPrimitive.Portal data-slot="alert-dialog-portal" {...props} />
  )
}

/**
 * 遮罩层，覆盖在页面其余内容之上，带有毛玻璃模糊效果。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 Radix AlertDialog Overlay 的属性。
 * @returns 渲染一个全屏半透明遮罩，带淡入淡出动画。
 */
function AlertDialogOverlay({
  className,
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Overlay>) {
  return (
    <AlertDialogPrimitive.Overlay
      data-slot="alert-dialog-overlay"
      className={cn(
        "fixed inset-0 z-50 bg-black/10 duration-100 supports-backdrop-filter:backdrop-blur-xs data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0",
        className
      )}
      {...props}
    />
  )
}

/**
 * 对话框主体内容容器。
 * @param className - 额外的自定义类名。
 * @param size - 对话框尺寸，"default" 或 "sm"，影响最大宽度。
 * @param onEscapeKeyDown - 自定义 Esc 键处理回调（注意：Esc 默认被拦截以防止误关闭）。
 * @param props - 透传给 Radix AlertDialog Content 的属性。
 * @returns 渲染居中显示的对话框面板，包含遮罩层，带缩放淡入动画。
 */
function AlertDialogContent({
  className,
  size = "default",
  onEscapeKeyDown,
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Content> & {
  size?: "default" | "sm"
}) {
  return (
    <AlertDialogPortal>
      <AlertDialogOverlay />
      <AlertDialogPrimitive.Content
        data-slot="alert-dialog-content"
        data-size={size}
        // 拦截 Esc 关闭，强制用户通过按钮进行确认/取消
        onEscapeKeyDown={(event) => {
          event.preventDefault()
          onEscapeKeyDown?.(event)
        }}
        className={cn(
          "group/alert-dialog-content fixed top-1/2 left-1/2 z-50 grid w-full -translate-x-1/2 -translate-y-1/2 gap-4 rounded-xl bg-popover p-4 text-popover-foreground ring-1 ring-foreground/10 duration-100 outline-none data-[size=default]:max-w-xs data-[size=sm]:max-w-xs data-[size=default]:sm:max-w-sm data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95",
          className
        )}
        {...props}
      />
    </AlertDialogPortal>
  )
}

/**
 * 对话框头部区域，包含标题和描述，布局根据是否含媒体内容自适应。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染对话框顶部标题区，内容居中或左对齐（取决于 size）。
 */
function AlertDialogHeader({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="alert-dialog-header"
      className={cn(
        "grid grid-rows-[auto_1fr] place-items-center gap-1.5 text-center has-data-[slot=alert-dialog-media]:grid-rows-[auto_auto_1fr] has-data-[slot=alert-dialog-media]:gap-x-4 sm:group-data-[size=default]/alert-dialog-content:place-items-start sm:group-data-[size=default]/alert-dialog-content:text-left sm:group-data-[size=default]/alert-dialog-content:has-data-[slot=alert-dialog-media]:grid-rows-[auto_1fr]",
        className
      )}
      {...props}
    />
  )
}

/**
 * 对话框底部区域，包含操作按钮（确认/取消），布局在不同尺寸下自适应。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染对话框底部按钮区。
 */
function AlertDialogFooter({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="alert-dialog-footer"
      className={cn(
        "-mx-4 -mb-4 flex flex-col-reverse gap-2 rounded-b-xl border-t bg-muted/50 p-4 group-data-[size=sm]/alert-dialog-content:grid group-data-[size=sm]/alert-dialog-content:grid-cols-2 sm:flex-row sm:justify-end",
        className
      )}
      {...props}
    />
  )
}

/**
 * 对话框媒体区域，用于展示图标或插画。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染一个圆角方块媒体容器。
 */
function AlertDialogMedia({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="alert-dialog-media"
      className={cn(
        "mb-2 inline-flex size-10 items-center justify-center rounded-md bg-muted sm:group-data-[size=default]/alert-dialog-content:row-span-2 *:[svg:not([class*='size-'])]:size-6",
        className
      )}
      {...props}
    />
  )
}

/**
 * 对话框标题文本。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 Radix AlertDialog Title 的属性。
 * @returns 渲染标题文本，使用标题字体。
 */
function AlertDialogTitle({
  className,
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Title>) {
  return (
    <AlertDialogPrimitive.Title
      data-slot="alert-dialog-title"
      className={cn(
        "font-heading text-base font-medium sm:group-data-[size=default]/alert-dialog-content:group-has-data-[slot=alert-dialog-media]/alert-dialog-content:col-start-2",
        className
      )}
      {...props}
    />
  )
}

/**
 * 对话框描述文本，用于补充说明。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 Radix AlertDialog Description 的属性。
 * @returns 渲染灰色辅助说明文本。
 */
function AlertDialogDescription({
  className,
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Description>) {
  return (
    <AlertDialogPrimitive.Description
      data-slot="alert-dialog-description"
      className={cn(
        "text-sm text-balance text-muted-foreground md:text-pretty *:[a]:underline *:[a]:underline-offset-3 *:[a]:hover:text-foreground",
        className
      )}
      {...props}
    />
  )
}

/**
 * 确认操作按钮，点击后会关闭对话框。
 * @param className - 额外的自定义类名。
 * @param variant - 按钮视觉变体，默认为 "default"（主色调）。
 * @param size - 按钮尺寸，默认为 "default"。
 * @param props - 透传给 Radix AlertDialog Action 的属性。
 * @returns 渲染一个使用 Button 样式的确认按钮。
 */
function AlertDialogAction({
  className,
  variant = "default",
  size = "default",
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Action> &
  Pick<React.ComponentProps<typeof Button>, "variant" | "size">) {
  return (
    <Button variant={variant} size={size} asChild>
      <AlertDialogPrimitive.Action
        data-slot="alert-dialog-action"
        className={cn(className)}
        {...props}
      />
    </Button>
  )
}

/**
 * 取消按钮，点击后关闭对话框但不执行操作。
 * @param className - 额外的自定义类名。
 * @param variant - 按钮视觉变体，默认为 "outline"（描边样式）。
 * @param size - 按钮尺寸，默认为 "default"。
 * @param props - 透传给 Radix AlertDialog Cancel 的属性。
 * @returns 渲染一个使用 Button 样式的取消按钮。
 */
function AlertDialogCancel({
  className,
  variant = "outline",
  size = "default",
  ...props
}: React.ComponentProps<typeof AlertDialogPrimitive.Cancel> &
  Pick<React.ComponentProps<typeof Button>, "variant" | "size">) {
  return (
    <Button variant={variant} size={size} asChild>
      <AlertDialogPrimitive.Cancel
        data-slot="alert-dialog-cancel"
        className={cn(className)}
        {...props}
      />
    </Button>
  )
}

export {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogMedia,
  AlertDialogOverlay,
  AlertDialogPortal,
  AlertDialogTitle,
  AlertDialogTrigger,
}

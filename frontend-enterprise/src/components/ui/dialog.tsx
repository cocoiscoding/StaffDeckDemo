/**
 * @file dialog.tsx
 * @description 对话框（模态弹窗）组件，基于 Radix UI 的 Dialog 原语封装。
 *              用于在页面中央弹出模态窗口，展示表单、详情等信息。
 *              支持 Esc 关闭拦截、自定义关闭按钮等特性。
 *              本项目中作为 shadcn/ui 基础组件，是最常用的弹窗方案。
 */

"use client"

import * as React from "react"
import { Dialog as DialogPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { XIcon } from "lucide-react"

/**
 * 对话框根容器，控制对话框的打开/关闭状态。
 * @param props - 透传给 Radix Dialog Root 的属性（如 open、onOpenChange 等）。
 * @returns 渲染一个 Dialog 根节点。
 */
function Dialog({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Root>) {
  return <DialogPrimitive.Root data-slot="dialog" {...props} />
}

/**
 * 对话框触发器，点击后打开对话框。
 * @param props - 透传给 Radix Dialog Trigger 的属性。
 * @param ref - 转发给底层 DOM 元素的 ref。
 * @returns 渲染一个触发元素。
 */
const DialogTrigger = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Trigger>
>(function DialogTrigger(props, ref) {
  return <DialogPrimitive.Trigger ref={ref} data-slot="dialog-trigger" {...props} />
})

/**
 * Portal 传送门，将对话框内容渲染到 document.body 下。
 * @param props - 透传给 Radix Dialog Portal 的属性。
 * @returns 渲染一个 Portal 容器。
 */
function DialogPortal({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Portal>) {
  return <DialogPrimitive.Portal data-slot="dialog-portal" {...props} />
}

/**
 * 关闭触发器，点击后关闭对话框。
 * @param props - 透传给 Radix Dialog Close 的属性。
 * @param ref - 转发给底层 DOM 元素的 ref。
 * @returns 渲染一个关闭触发元素。
 */
const DialogClose = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Close>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Close>
>(function DialogClose(props, ref) {
  return <DialogPrimitive.Close ref={ref} data-slot="dialog-close" {...props} />
})

/**
 * 遮罩层，覆盖在页面其余内容之上，带有毛玻璃模糊效果。
 * @param className - 额外的自定义类名。
 * @param ref - 转发给底层 DOM 元素的 ref。
 * @param props - 透传给 Radix Dialog Overlay 的属性。
 * @returns 渲染一个全屏半透明遮罩，带淡入淡出动画。
 */
const DialogOverlay = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(function DialogOverlay({ className, ...props }, ref) {
  return (
    <DialogPrimitive.Overlay
      ref={ref}
      data-slot="dialog-overlay"
      className={cn(
        "fixed inset-0 isolate z-50 bg-black/10 duration-100 supports-backdrop-filter:backdrop-blur-xs data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0",
        className
      )}
      {...props}
    />
  )
})

/**
 * 对话框主体内容容器。
 * @param className - 额外的自定义类名。
 * @param children - 对话框内容。
 * @param showCloseButton - 是否显示右上角关闭按钮，默认为 true。
 * @param onEscapeKeyDown - 自定义 Esc 键处理回调（注意：Esc 默认被拦截）。
 * @param ref - 转发给底层 DOM 元素的 ref。
 * @param props - 透传给 Radix Dialog Content 的属性。
 * @returns 渲染居中显示的对话框面板，包含遮罩层和可选关闭按钮。
 */
const DialogContent = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> & {
    showCloseButton?: boolean
  }
>(function DialogContent(
  { className, children, showCloseButton = true, onEscapeKeyDown, ...props },
  ref
) {
  return (
    <DialogPortal>
      <DialogOverlay />
      <DialogPrimitive.Content
        ref={ref}
        data-slot="dialog-content"
        // 拦截 Esc 关闭，由调用方通过 showCloseButton 或受控 open 来管理关闭
        onEscapeKeyDown={(event) => {
          event.preventDefault()
          onEscapeKeyDown?.(event)
        }}
        className={cn(
          "fixed top-1/2 left-1/2 z-50 grid w-full max-w-[calc(100%-2rem)] -translate-x-1/2 -translate-y-1/2 gap-4 rounded-xl bg-popover p-4 text-sm text-popover-foreground ring-1 ring-foreground/10 duration-100 outline-none sm:max-w-sm data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95",
          className
        )}
        {...props}
      >
        {children}
        {/* 右上角关闭按钮 */}
        {showCloseButton && (
          <DialogPrimitive.Close data-slot="dialog-close" asChild>
            <Button
              variant="ghost"
              className="absolute top-2 right-2 text-[#858b9c] hover:bg-[#f2f3f7] hover:text-[#18181a]"
              size="icon-sm"
            >
              <XIcon
              />
              <span className="sr-only">Close</span>
            </Button>
          </DialogPrimitive.Close>
        )}
      </DialogPrimitive.Content>
    </DialogPortal>
  )
})

/**
 * 对话框头部区域，包含标题和描述。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染对话框顶部垂直排列的容器。
 */
function DialogHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dialog-header"
      className={cn("flex flex-col gap-2", className)}
      {...props}
    />
  )
}

/**
 * 对话框底部区域，包含操作按钮。
 * @param className - 额外的自定义类名。
 * @param showCloseButton - 是否在底部显示关闭按钮，默认为 false。
 * @param children - 底部内容。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染对话框底部水平排列的按钮区域。
 */
function DialogFooter({
  className,
  showCloseButton = false,
  children,
  ...props
}: React.ComponentProps<"div"> & {
  showCloseButton?: boolean
}) {
  return (
    <div
      data-slot="dialog-footer"
      className={cn(
        "flex flex-col-reverse gap-2 bg-white px-[24px] py-[12px] sm:flex-row sm:justify-end",
        className
      )}
      {...props}
    >
      {children}
      {showCloseButton && (
        <DialogPrimitive.Close asChild>
          <Button variant="outline">Close</Button>
        </DialogPrimitive.Close>
      )}
    </div>
  )
}

/**
 * 对话框标题文本。
 * @param className - 额外的自定义类名。
 * @param ref - 转发给底层 DOM 元素的 ref。
 * @param props - 透传给 Radix Dialog Title 的属性。
 * @returns 渲染使用标题字体的标题文本。
 */
const DialogTitle = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(function DialogTitle({ className, ...props }, ref) {
  return (
    <DialogPrimitive.Title
      ref={ref}
      data-slot="dialog-title"
      className={cn(
        "font-heading text-base leading-none font-medium",
        className
      )}
      {...props}
    />
  )
})

/**
 * 对话框描述文本。
 * @param className - 额外的自定义类名。
 * @param ref - 转发给底层 DOM 元素的 ref。
 * @param props - 透传给 Radix Dialog Description 的属性。
 * @returns 渲染灰色辅助描述文本。
 */
const DialogDescription = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(function DialogDescription({ className, ...props }, ref) {
  return (
    <DialogPrimitive.Description
      ref={ref}
      data-slot="dialog-description"
      className={cn(
        "text-sm text-muted-foreground *:[a]:underline *:[a]:underline-offset-3 *:[a]:hover:text-foreground",
        className
      )}
      {...props}
    />
  )
})

export {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogOverlay,
  DialogPortal,
  DialogTitle,
  DialogTrigger,
}

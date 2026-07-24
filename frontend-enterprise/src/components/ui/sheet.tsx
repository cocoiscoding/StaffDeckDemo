/**
 * @file sheet.tsx
 * @description 侧边抽屉（滑出面板）组件，基于 Radix UI 的 Dialog 原语封装。
 *              与 Dialog 的区别：Sheet 从屏幕边缘滑入（上/下/左/右），而非居中弹出。
 *              适用于导航菜单、筛选面板、详情面板等需要从侧边展开的场景。
 *              本项目中作为 shadcn/ui 基础组件，在移动端导航等场景中使用。
 */

import * as React from "react"
import { Dialog as SheetPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { XIcon } from "lucide-react"

/**
 * 抽屉根容器，控制打开/关闭状态。
 * @param props - 透传给 Radix Dialog Root 的属性。
 * @returns 渲染一个 Sheet 根节点。
 */
function Sheet({ ...props }: React.ComponentProps<typeof SheetPrimitive.Root>) {
  return <SheetPrimitive.Root data-slot="sheet" {...props} />
}

/**
 * 抽屉触发器，点击后打开抽屉。
 * @param props - 透传给 Radix Dialog Trigger 的属性。
 * @returns 渲染一个触发元素。
 */
function SheetTrigger({
  ...props
}: React.ComponentProps<typeof SheetPrimitive.Trigger>) {
  return <SheetPrimitive.Trigger data-slot="sheet-trigger" {...props} />
}

/**
 * 关闭触发器，点击后关闭抽屉。
 * @param props - 透传给 Radix Dialog Close 的属性。
 * @returns 渲染一个关闭触发元素。
 */
function SheetClose({
  ...props
}: React.ComponentProps<typeof SheetPrimitive.Close>) {
  return <SheetPrimitive.Close data-slot="sheet-close" {...props} />
}

/**
 * Portal 传送门，将抽屉内容渲染到 document.body 下。
 * @param props - 透传给 Radix Dialog Portal 的属性。
 * @returns 渲染一个 Portal 容器。
 */
function SheetPortal({
  ...props
}: React.ComponentProps<typeof SheetPrimitive.Portal>) {
  return <SheetPrimitive.Portal data-slot="sheet-portal" {...props} />
}

/**
 * 遮罩层，覆盖在页面其余内容之上。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 Radix Dialog Overlay 的属性。
 * @returns 渲染一个全屏半透明遮罩，带毛玻璃效果和淡入淡出动画。
 */
function SheetOverlay({
  className,
  ...props
}: React.ComponentProps<typeof SheetPrimitive.Overlay>) {
  return (
    <SheetPrimitive.Overlay
      data-slot="sheet-overlay"
      className={cn(
        "fixed inset-0 z-50 bg-black/10 duration-100 supports-backdrop-filter:backdrop-blur-xs data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0",
        className
      )}
      {...props}
    />
  )
}

/**
 * 抽屉主体内容容器。
 * @param className - 额外的自定义类名。
 * @param children - 抽屉内容。
 * @param side - 滑出方向，"top" / "right"（默认）/ "bottom" / "left"。
 * @param showCloseButton - 是否显示关闭按钮，默认为 true。
 * @param onEscapeKeyDown - 自定义 Esc 键处理回调（Esc 默认被拦截）。
 * @param props - 透传给 Radix Dialog Content 的属性。
 * @returns 渲染从指定方向滑入的抽屉面板，带遮罩层和滑入动画。
 */
function SheetContent({
  className,
  children,
  side = "right",
  showCloseButton = true,
  onEscapeKeyDown,
  ...props
}: React.ComponentProps<typeof SheetPrimitive.Content> & {
  side?: "top" | "right" | "bottom" | "left"
  showCloseButton?: boolean
}) {
  return (
    <SheetPortal>
      <SheetOverlay />
      <SheetPrimitive.Content
        data-slot="sheet-content"
        data-side={side}
        // 拦截 Esc 关闭，由调用方通过受控 open 或关闭按钮来管理关闭
        onEscapeKeyDown={(event) => {
          event.preventDefault()
          onEscapeKeyDown?.(event)
        }}
        className={cn(
          "fixed z-50 flex flex-col gap-4 bg-popover bg-clip-padding text-sm text-popover-foreground shadow-lg transition duration-200 ease-in-out data-[side=bottom]:inset-x-0 data-[side=bottom]:bottom-0 data-[side=bottom]:h-auto data-[side=bottom]:border-t data-[side=left]:inset-y-0 data-[side=left]:left-0 data-[side=left]:h-full data-[side=left]:w-3/4 data-[side=left]:border-r data-[side=right]:inset-y-0 data-[side=right]:right-0 data-[side=right]:h-full data-[side=right]:w-3/4 data-[side=right]:border-l data-[side=top]:inset-x-0 data-[side=top]:top-0 data-[side=top]:h-auto data-[side=top]:border-b data-[side=left]:sm:max-w-sm data-[side=right]:sm:max-w-sm data-open:animate-in data-open:fade-in-0 data-[side=bottom]:data-open:slide-in-from-bottom-10 data-[side=left]:data-open:slide-in-from-left-10 data-[side=right]:data-open:slide-in-from-right-10 data-[side=top]:data-open:slide-in-from-top-10 data-closed:animate-out data-closed:fade-out-0 data-[side=bottom]:data-closed:slide-out-to-bottom-10 data-[side=left]:data-closed:slide-out-to-left-10 data-[side=right]:data-closed:slide-out-to-right-10 data-[side=top]:data-closed:slide-out-to-top-10",
          className
        )}
        {...props}
      >
        {children}
        {/* 关闭按钮 */}
        {showCloseButton && (
          <SheetPrimitive.Close data-slot="sheet-close" asChild>
            <Button
              variant="ghost"
              className="absolute top-3 right-3"
              size="icon-sm"
            >
              <XIcon
              />
              <span className="sr-only">Close</span>
            </Button>
          </SheetPrimitive.Close>
        )}
      </SheetPrimitive.Content>
    </SheetPortal>
  )
}

/**
 * 抽屉头部区域。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染抽屉顶部垂直布局容器。
 */
function SheetHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sheet-header"
      className={cn("flex flex-col gap-0.5 p-4", className)}
      {...props}
    />
  )
}

/**
 * 抽屉底部区域，通常放置操作按钮。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染通过 mt-auto 定位到底部的按钮容器。
 */
function SheetFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sheet-footer"
      className={cn("mt-auto flex flex-col gap-2 p-4", className)}
      {...props}
    />
  )
}

/**
 * 抽屉标题。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 Radix Dialog Title 的属性。
 * @returns 渲染使用标题字体的标题文本。
 */
function SheetTitle({
  className,
  ...props
}: React.ComponentProps<typeof SheetPrimitive.Title>) {
  return (
    <SheetPrimitive.Title
      data-slot="sheet-title"
      className={cn(
        "font-heading text-base font-medium text-foreground",
        className
      )}
      {...props}
    />
  )
}

/**
 * 抽屉描述文本。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 Radix Dialog Description 的属性。
 * @returns 渲染灰色辅助描述文本。
 */
function SheetDescription({
  className,
  ...props
}: React.ComponentProps<typeof SheetPrimitive.Description>) {
  return (
    <SheetPrimitive.Description
      data-slot="sheet-description"
      className={cn("text-sm text-muted-foreground", className)}
      {...props}
    />
  )
}

export {
  Sheet,
  SheetTrigger,
  SheetClose,
  SheetContent,
  SheetHeader,
  SheetFooter,
  SheetTitle,
  SheetDescription,
}

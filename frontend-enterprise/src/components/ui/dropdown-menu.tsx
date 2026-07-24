/**
 * @file dropdown-menu.tsx
 * @description 下拉菜单组件，基于 Radix UI 的 DropdownMenu 原语封装。
 *              支持普通菜单项、复选框项、单选项、子菜单、分隔符、快捷键标签等丰富的菜单类型。
 *              本项目中作为 shadcn/ui 基础组件，用于行操作菜单、设置菜单、筛选菜单等场景。
 */

import * as React from "react"
import { DropdownMenu as DropdownMenuPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"
import { CheckIcon, ChevronRightIcon } from "lucide-react"

/**
 * 下拉菜单根容器，管理菜单的打开/关闭状态。
 * @param props - 透传给 Radix DropdownMenu Root 的属性。
 * @returns 渲染一个下拉菜单根节点。
 */
function DropdownMenu({
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Root>) {
  return <DropdownMenuPrimitive.Root data-slot="dropdown-menu" {...props} />
}

/**
 * Portal 传送门，将菜单内容渲染到 document.body 下。
 * @param props - 透传给 Radix DropdownMenu Portal 的属性。
 * @returns 渲染一个 Portal 容器。
 */
function DropdownMenuPortal({
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Portal>) {
  return (
    <DropdownMenuPrimitive.Portal data-slot="dropdown-menu-portal" {...props} />
  )
}

/**
 * 下拉菜单触发器，点击后打开菜单。
 * @param props - 透传给 Radix DropdownMenu Trigger 的属性。
 * @returns 渲染一个触发元素。
 */
function DropdownMenuTrigger({
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Trigger>) {
  return (
    <DropdownMenuPrimitive.Trigger
      data-slot="dropdown-menu-trigger"
      {...props}
    />
  )
}

/**
 * 下拉菜单内容容器，自动根据可用空间设置最大高度和宽度。
 * @param className - 额外的自定义类名。
 * @param align - 对齐方式，默认为 "start"（左对齐）。
 * @param sideOffset - 与触发器的间距，默认为 4px。
 * @param props - 透传给 Radix DropdownMenu Content 的属性。
 * @returns 渲染菜单面板，带圆角、阴影和滑入动画。
 */
function DropdownMenuContent({
  className,
  align = "start",
  sideOffset = 4,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Content>) {
  return (
    <DropdownMenuPrimitive.Portal>
      <DropdownMenuPrimitive.Content
        data-slot="dropdown-menu-content"
        sideOffset={sideOffset}
        align={align}
        className={cn("z-50 max-h-(--radix-dropdown-menu-content-available-height) w-(--radix-dropdown-menu-trigger-width) min-w-32 origin-(--radix-dropdown-menu-content-transform-origin) overflow-x-hidden overflow-y-auto rounded-lg bg-popover p-1 text-popover-foreground shadow-md ring-1 ring-foreground/10 duration-100 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 data-[state=closed]:overflow-hidden data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95", className )}
        {...props}
      />
    </DropdownMenuPrimitive.Portal>
  )
}

/**
 * 菜单项分组容器，用于将相关菜单项组织在一起。
 * @param props - 透传给 Radix DropdownMenu Group 的属性。
 * @returns 渲染一个菜单分组容器。
 */
function DropdownMenuGroup({
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Group>) {
  return (
    <DropdownMenuPrimitive.Group data-slot="dropdown-menu-group" {...props} />
  )
}

/**
 * 普通菜单项，点击后执行操作并关闭菜单。
 * @param className - 额外的自定义类名。
 * @param inset - 是否缩进左侧（用于分组层级）。
 * @param variant - 变体，"default"（默认）或 "destructive"（危险操作红色文字）。
 * @param props - 透传给 Radix DropdownMenu Item 的属性。
 * @returns 渲染一个可点击的菜单项。
 */
function DropdownMenuItem({
  className,
  inset,
  variant = "default",
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Item> & {
  inset?: boolean
  variant?: "default" | "destructive"
}) {
  return (
    <DropdownMenuPrimitive.Item
      data-slot="dropdown-menu-item"
      data-inset={inset}
      data-variant={variant}
      className={cn(
        "group/dropdown-menu-item relative flex cursor-default items-center gap-1.5 rounded-md px-1.5 py-1 text-sm outline-hidden select-none focus:bg-accent focus:text-accent-foreground not-data-[variant=destructive]:focus:**:text-accent-foreground data-inset:pl-7 data-[variant=destructive]:text-destructive data-[variant=destructive]:focus:bg-destructive/10 data-[variant=destructive]:focus:text-destructive data-disabled:pointer-events-none data-disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4 data-[variant=destructive]:*:[svg]:text-destructive",
        className
      )}
      {...props}
    />
  )
}

/**
 * 复选框菜单项，支持勾选状态。
 * @param className - 额外的自定义类名。
 * @param children - 菜单项文本内容。
 * @param checked - 是否勾选。
 * @param inset - 是否缩进左侧。
 * @param props - 透传给 Radix DropdownMenu CheckboxItem 的属性。
 * @returns 渲染一个带勾选指示器的菜单项。
 */
function DropdownMenuCheckboxItem({
  className,
  children,
  checked,
  inset,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.CheckboxItem> & {
  inset?: boolean
}) {
  return (
    <DropdownMenuPrimitive.CheckboxItem
      data-slot="dropdown-menu-checkbox-item"
      data-inset={inset}
      className={cn(
        "relative flex cursor-default items-center gap-1.5 rounded-md py-1 pr-8 pl-1.5 text-sm outline-hidden select-none focus:bg-accent focus:text-accent-foreground focus:**:text-accent-foreground data-inset:pl-7 data-disabled:pointer-events-none data-disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
        className
      )}
      checked={checked}
      {...props}
    >
      {/* 右侧勾选指示器 */}
      <span
        className="pointer-events-none absolute right-2 flex items-center justify-center"
        data-slot="dropdown-menu-checkbox-item-indicator"
      >
        <DropdownMenuPrimitive.ItemIndicator>
          <CheckIcon
          />
        </DropdownMenuPrimitive.ItemIndicator>
      </span>
      {children}
    </DropdownMenuPrimitive.CheckboxItem>
  )
}

/**
 * 单选菜单组容器，管理一组单选项的状态。
 * @param props - 透传给 Radix DropdownMenu RadioGroup 的属性。
 * @returns 渲染一个单选组容器。
 */
function DropdownMenuRadioGroup({
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.RadioGroup>) {
  return (
    <DropdownMenuPrimitive.RadioGroup
      data-slot="dropdown-menu-radio-group"
      {...props}
    />
  )
}

/**
 * 单选菜单项，在同一组内只能选中一个。
 * @param className - 额外的自定义类名。
 * @param children - 菜单项文本内容。
 * @param inset - 是否缩进左侧。
 * @param props - 透传给 Radix DropdownMenu RadioItem 的属性。
 * @returns 渲染一个带选中指示器的单选菜单项。
 */
function DropdownMenuRadioItem({
  className,
  children,
  inset,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.RadioItem> & {
  inset?: boolean
}) {
  return (
    <DropdownMenuPrimitive.RadioItem
      data-slot="dropdown-menu-radio-item"
      data-inset={inset}
      className={cn(
        "relative flex cursor-default items-center gap-1.5 rounded-md py-1 pr-8 pl-1.5 text-sm outline-hidden select-none focus:bg-accent focus:text-accent-foreground focus:**:text-accent-foreground data-inset:pl-7 data-disabled:pointer-events-none data-disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
        className
      )}
      {...props}
    >
      {/* 右侧选中指示器 */}
      <span
        className="pointer-events-none absolute right-2 flex items-center justify-center"
        data-slot="dropdown-menu-radio-item-indicator"
      >
        <DropdownMenuPrimitive.ItemIndicator>
          <CheckIcon
          />
        </DropdownMenuPrimitive.ItemIndicator>
      </span>
      {children}
    </DropdownMenuPrimitive.RadioItem>
  )
}

/**
 * 菜单分组标签，用于标识一组菜单项的名称。
 * @param className - 额外的自定义类名。
 * @param inset - 是否缩进左侧。
 * @param props - 透传给 Radix DropdownMenu Label 的属性。
 * @returns 渲染灰色小号标签文本。
 */
function DropdownMenuLabel({
  className,
  inset,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Label> & {
  inset?: boolean
}) {
  return (
    <DropdownMenuPrimitive.Label
      data-slot="dropdown-menu-label"
      data-inset={inset}
      className={cn(
        "px-1.5 py-1 text-xs font-medium text-muted-foreground data-inset:pl-7",
        className
      )}
      {...props}
    />
  )
}

/**
 * 菜单分隔线，用于在视觉上分隔不同分组的菜单项。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 Radix DropdownMenu Separator 的属性。
 * @returns 渲染一条水平分隔线。
 */
function DropdownMenuSeparator({
  className,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Separator>) {
  return (
    <DropdownMenuPrimitive.Separator
      data-slot="dropdown-menu-separator"
      className={cn("-mx-1 my-1 h-px bg-border", className)}
      {...props}
    />
  )
}

/**
 * 菜单项快捷键标签，通常显示在菜单项右侧（如 ⌘S）。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 span 元素的属性。
 * @returns 渲染右对齐的灰色快捷键文本。
 */
function DropdownMenuShortcut({
  className,
  ...props
}: React.ComponentProps<"span">) {
  return (
    <span
      data-slot="dropdown-menu-shortcut"
      className={cn(
        "ml-auto text-xs tracking-widest text-muted-foreground group-focus/dropdown-menu-item:text-accent-foreground",
        className
      )}
      {...props}
    />
  )
}

/**
 * 子菜单容器，管理子菜单的展开/收起状态。
 * @param props - 透传给 Radix DropdownMenu Sub 的属性。
 * @returns 渲染一个子菜单根节点。
 */
function DropdownMenuSub({
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Sub>) {
  return <DropdownMenuPrimitive.Sub data-slot="dropdown-menu-sub" {...props} />
}

/**
 * 子菜单触发器，点击或悬停后展开子菜单。
 * @param className - 额外的自定义类名。
 * @param inset - 是否缩进左侧。
 * @param children - 触发器文本内容。
 * @param props - 透传给 Radix DropdownMenu SubTrigger 的属性。
 * @returns 渲染一个带右箭头的子菜单触发项。
 */
function DropdownMenuSubTrigger({
  className,
  inset,
  children,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.SubTrigger> & {
  inset?: boolean
}) {
  return (
    <DropdownMenuPrimitive.SubTrigger
      data-slot="dropdown-menu-sub-trigger"
      data-inset={inset}
      className={cn(
        "flex cursor-default items-center gap-1.5 rounded-md px-1.5 py-1 text-sm outline-hidden select-none focus:bg-accent focus:text-accent-foreground not-data-[variant=destructive]:focus:**:text-accent-foreground data-inset:pl-7 data-open:bg-accent data-open:text-accent-foreground [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
        className
      )}
      {...props}
    >
      {children}
      {/* 右箭头图标，指示存在子菜单 */}
      <ChevronRightIcon className="ml-auto" />
    </DropdownMenuPrimitive.SubTrigger>
  )
}

/**
 * 子菜单内容容器。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 Radix DropdownMenu SubContent 的属性。
 * @returns 渲染子菜单面板，带圆角、阴影和滑入动画。
 */
function DropdownMenuSubContent({
  className,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.SubContent>) {
  return (
    <DropdownMenuPrimitive.SubContent
      data-slot="dropdown-menu-sub-content"
      className={cn("z-50 min-w-[96px] origin-(--radix-dropdown-menu-content-transform-origin) overflow-hidden rounded-lg bg-popover p-1 text-popover-foreground shadow-lg ring-1 ring-foreground/10 duration-100 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95", className )}
      {...props}
    />
  )
}

export {
  DropdownMenu,
  DropdownMenuPortal,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuLabel,
  DropdownMenuItem,
  DropdownMenuCheckboxItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuSub,
  DropdownMenuSubTrigger,
  DropdownMenuSubContent,
}

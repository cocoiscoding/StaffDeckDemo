/**
 * @file sidebar.tsx
 * @description 侧边栏组件，是一个功能完整、可组合的复合组件系统。
 *              使用 React Context 管理侧边栏的展开/折叠/移动端状态，
 *              支持桌面端可折叠（图标模式/离屏模式）、移动端抽屉模式。
 *              使用 class-variance-authority (cva) 管理菜单按钮的样式变体。
 *              本项目中作为 shadcn/ui 基础组件，是后台管理系统布局的核心组件。
 */

"use client"

import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { Slot } from "radix-ui"

import { useIsMobile } from "@/hooks/use-mobile"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { PanelLeftIcon } from "lucide-react"

// Cookie 名称，用于持久化存储侧边栏展开/折叠状态
const SIDEBAR_COOKIE_NAME = "sidebar_state"
// Cookie 最大存活时间：7 天
const SIDEBAR_COOKIE_MAX_AGE = 60 * 60 * 24 * 7
// 侧边栏展开宽度：16rem (256px)
const SIDEBAR_WIDTH = "16rem"
// 侧边栏折叠为图标模式时的宽度：3rem (48px)
const SIDEBAR_WIDTH_ICON = "3rem"
// 切换侧边栏的键盘快捷键：Cmd/Ctrl + B
const SIDEBAR_KEYBOARD_SHORTCUT = "b"

/** 侧边栏上下文属性类型 */
type SidebarContextProps = {
  /** 当前状态：展开或折叠 */
  state: "expanded" | "collapsed"
  /** 桌面端是否展开 */
  open: boolean
  /** 设置桌面端展开状态 */
  setOpen: (open: boolean) => void
  /** 移动端是否展开（抽屉模式） */
  openMobile: boolean
  /** 设置移动端展开状态 */
  setOpenMobile: (open: boolean) => void
  /** 是否为移动端设备 */
  isMobile: boolean
  /** 切换侧边栏展开/折叠 */
  toggleSidebar: () => void
}

/** 侧边栏上下文，用于在组件树中共享状态 */
const SidebarContext = React.createContext<SidebarContextProps | null>(null)

/**
 * 侧边栏状态 Hook，必须在 SidebarProvider 内部使用。
 * @returns 侧边栏上下文状态对象。
 * @throws 如果在 SidebarProvider 外部调用则抛出错误。
 */
function useSidebar() {
  const context = React.useContext(SidebarContext)
  if (!context) {
    throw new Error("useSidebar must be used within a SidebarProvider.")
  }

  return context
}

/**
 * 侧边栏状态提供者，管理展开/折叠/移动端状态，并注册键盘快捷键。
 * @param defaultOpen - 初始是否展开，默认为 true。
 * @param open - 受控的展开状态（外部控制）。
 * @param onOpenChange - 展开状态变化回调（受控模式）。
 * @param className - 额外的自定义类名。
 * @param style - 额外的内联样式。
 * @param children - 子内容。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染一个 Provider 包裹的容器，设置 CSS 变量并提供侧边栏上下文。
 */
function SidebarProvider({
  defaultOpen = true,
  open: openProp,
  onOpenChange: setOpenProp,
  className,
  style,
  children,
  ...props
}: React.ComponentProps<"div"> & {
  defaultOpen?: boolean
  open?: boolean
  onOpenChange?: (open: boolean) => void
}) {
  const isMobile = useIsMobile()
  const [openMobile, setOpenMobile] = React.useState(false)

  // 这是侧边栏的内部状态。
  // 使用 openProp 和 setOpenProp 实现从组件外部受控。
  const [_open, _setOpen] = React.useState(defaultOpen)
  const open = openProp ?? _open
  const setOpen = React.useCallback(
    (value: boolean | ((value: boolean) => boolean)) => {
      const openState = typeof value === "function" ? value(open) : value
      if (setOpenProp) {
        setOpenProp(openState)
      } else {
        _setOpen(openState)
      }

      // 通过 Cookie 持久化侧边栏状态
      document.cookie = `${SIDEBAR_COOKIE_NAME}=${openState}; path=/; max-age=${SIDEBAR_COOKIE_MAX_AGE}`
    },
    [setOpenProp, open]
  )

  // 切换侧边栏的辅助函数。
  // 始终驱动桌面端展开状态，确保侧边栏在小屏幕上保持可见的图标轨道，而非离屏抽屉。
  const toggleSidebar = React.useCallback(() => {
    return setOpen((open) => !open)
  }, [setOpen])

  // 注册键盘快捷键 Cmd/Ctrl + B 切换侧边栏
  React.useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (
        event.key === SIDEBAR_KEYBOARD_SHORTCUT &&
        (event.metaKey || event.ctrlKey)
      ) {
        event.preventDefault()
        toggleSidebar()
      }
    }

    window.addEventListener("keydown", handleKeyDown)
    return () => window.removeEventListener("keydown", handleKeyDown)
  }, [toggleSidebar])

  // 计算 data-state 属性值，方便用 Tailwind 样式控制
  const state = open ? "expanded" : "collapsed"

  const contextValue = React.useMemo<SidebarContextProps>(
    () => ({
      state,
      open,
      setOpen,
      isMobile,
      openMobile,
      setOpenMobile,
      toggleSidebar,
    }),
    [state, open, setOpen, isMobile, openMobile, setOpenMobile, toggleSidebar]
  )

  return (
    <SidebarContext.Provider value={contextValue}>
      <div
        data-slot="sidebar-wrapper"
        style={
          {
            "--sidebar-width": SIDEBAR_WIDTH,
            "--sidebar-width-icon": SIDEBAR_WIDTH_ICON,
            ...style,
          } as React.CSSProperties
        }
        className={cn(
          "group/sidebar-wrapper flex min-h-svh w-full has-data-[variant=inset]:bg-sidebar",
          className
        )}
        {...props}
      >
        {children}
      </div>
    </SidebarContext.Provider>
  )
}

/**
 * 侧边栏主容器。
 * @param side - 侧边位置，"left"（默认）或 "right"。
 * @param variant - 变体，"sidebar"（默认）、"floating"（浮动）或 "inset"（内嵌）。
 * @param collapsible - 折叠模式，"offcanvas"（离屏，默认）、"icon"（图标模式）或 "none"（不可折叠）。
 * @param className - 额外的自定义类名。
 * @param children - 侧边栏内容。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染侧边栏容器，包含间距占位元素和实际内容容器。
 */
function Sidebar({
  side = "left",
  variant = "sidebar",
  collapsible = "offcanvas",
  className,
  children,
  ...props
}: React.ComponentProps<"div"> & {
  side?: "left" | "right"
  variant?: "sidebar" | "floating" | "inset"
  collapsible?: "offcanvas" | "icon" | "none"
}) {
  const { state } = useSidebar()

  // 不可折叠时直接渲染固定宽度容器
  if (collapsible === "none") {
    return (
      <div
        data-slot="sidebar"
        className={cn(
          "flex h-full w-(--sidebar-width) flex-col bg-sidebar text-sidebar-foreground",
          className
        )}
        {...props}
      >
        {children}
      </div>
    )
  }

  return (
    <div
      className="group peer block text-sidebar-foreground"
      data-state={state}
      data-collapsible={state === "collapsed" ? collapsible : ""}
      data-variant={variant}
      data-side={side}
      data-slot="sidebar"
    >
      {/* 间距占位元素，在桌面端负责为侧边栏预留空间 */}
      <div
        data-slot="sidebar-gap"
        className={cn(
          "relative w-(--sidebar-width) bg-transparent transition-[width] duration-300 ease-[cubic-bezier(0.32,0.72,0,1)]",
          "group-data-[collapsible=offcanvas]:w-0",
          "group-data-[side=right]:rotate-180",
          variant === "floating" || variant === "inset"
            ? "group-data-[collapsible=icon]:w-[calc(var(--sidebar-width-icon)+(--spacing(4)))]"
            : "group-data-[collapsible=icon]:w-(--sidebar-width-icon)"
        )}
      />
      {/* 侧边栏实际内容容器，fixed 定位 */}
      <div
        data-slot="sidebar-container"
        data-side={side}
        className={cn(
          "fixed inset-y-0 z-10 flex h-svh w-(--sidebar-width) transition-[left,right,width] duration-300 ease-[cubic-bezier(0.32,0.72,0,1)] data-[side=left]:left-0 data-[side=left]:group-data-[collapsible=offcanvas]:left-[calc(var(--sidebar-width)*-1)] data-[side=right]:right-0 data-[side=right]:group-data-[collapsible=offcanvas]:right-[calc(var(--sidebar-width)*-1)]",
          // floating 和 inset 变体调整内边距
          variant === "floating" || variant === "inset"
            ? "p-2 group-data-[collapsible=icon]:w-[calc(var(--sidebar-width-icon)+(--spacing(4))+2px)]"
            : "group-data-[collapsible=icon]:w-(--sidebar-width-icon) group-data-[side=left]:border-r group-data-[side=right]:border-l",
          className
        )}
        {...props}
      >
        <div
          data-sidebar="sidebar"
          data-slot="sidebar-inner"
          className="flex size-full flex-col bg-sidebar group-data-[variant=floating]:rounded-lg group-data-[variant=floating]:shadow-sm group-data-[variant=floating]:ring-1 group-data-[variant=floating]:ring-sidebar-border"
        >
          {children}
        </div>
      </div>
    </div>
  )
}

/**
 * 侧边栏切换触发器按钮。
 * @param className - 额外的自定义类名。
 * @param onClick - 自定义点击回调（在切换侧边栏之前执行）。
 * @param props - 透传给 Button 的属性。
 * @returns 渲染一个带面板图标的幽灵按钮，点击后切换侧边栏展开/折叠。
 */
function SidebarTrigger({
  className,
  onClick,
  ...props
}: React.ComponentProps<typeof Button>) {
  const { toggleSidebar } = useSidebar()

  return (
    <Button
      data-sidebar="trigger"
      data-slot="sidebar-trigger"
      variant="ghost"
      size="icon-sm"
      className={cn(className)}
      onClick={(event) => {
        onClick?.(event)
        toggleSidebar()
      }}
      {...props}
    >
      <PanelLeftIcon />
      <span className="sr-only">Toggle Sidebar</span>
    </Button>
  )
}

/**
 * 侧边栏拖拽轨道，位于侧边栏边缘，点击或拖拽可切换展开/折叠。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 button 元素的属性。
 * @returns 渲染一个隐藏的拖拽轨道按钮（仅桌面端显示）。
 */
function SidebarRail({ className, ...props }: React.ComponentProps<"button">) {
  const { toggleSidebar } = useSidebar()

  return (
    <button
      data-sidebar="rail"
      data-slot="sidebar-rail"
      aria-label="Toggle Sidebar"
      tabIndex={-1}
      onClick={toggleSidebar}
      title="Toggle Sidebar"
      className={cn(
        "absolute inset-y-0 z-20 hidden w-4 transition-all ease-linear group-data-[side=left]:-right-4 group-data-[side=right]:left-0 after:absolute after:inset-y-0 after:start-1/2 after:w-[2px] hover:after:bg-sidebar-border sm:flex ltr:-translate-x-1/2 rtl:-translate-x-1/2",
        "in-data-[side=left]:cursor-w-resize in-data-[side=right]:cursor-e-resize",
        "[[data-side=left][data-state=collapsed]_&]:cursor-e-resize [[data-side=right][data-state=collapsed]_&]:cursor-w-resize",
        "group-data-[collapsible=offcanvas]:translate-x-0 group-data-[collapsible=offcanvas]:after:left-full hover:group-data-[collapsible=offcanvas]:bg-sidebar",
        "[[data-side=left][data-collapsible=offcanvas]_&]:-right-2",
        "[[data-side=right][data-collapsible=offcanvas]_&]:-left-2",
        className
      )}
      {...props}
    />
  )
}

/**
 * 主内容区域容器，与侧边栏配合使用。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 main 元素的属性。
 * @returns 渲染主内容区，在 inset 变体下有额外的边距和圆角。
 */
function SidebarInset({ className, ...props }: React.ComponentProps<"main">) {
  return (
    <main
      data-slot="sidebar-inset"
      className={cn(
        "relative flex w-full flex-1 flex-col bg-background md:peer-data-[variant=inset]:m-2 md:peer-data-[variant=inset]:ml-0 md:peer-data-[variant=inset]:rounded-xl md:peer-data-[variant=inset]:shadow-sm md:peer-data-[variant=inset]:peer-data-[state=collapsed]:ml-2",
        className
      )}
      {...props}
    />
  )
}

/**
 * 侧边栏内嵌的搜索输入框。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 Input 的属性。
 * @returns 渲染一个适配侧边栏样式的输入框。
 */
function SidebarInput({
  className,
  ...props
}: React.ComponentProps<typeof Input>) {
  return (
    <Input
      data-slot="sidebar-input"
      data-sidebar="input"
      className={cn("h-8 w-full bg-background shadow-none", className)}
      {...props}
    />
  )
}

/**
 * 侧边栏头部区域。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染侧边栏顶部垂直布局容器。
 */
function SidebarHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sidebar-header"
      data-sidebar="header"
      className={cn("flex flex-col gap-2 p-2", className)}
      {...props}
    />
  )
}

/**
 * 侧边栏底部区域。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染侧边栏底部垂直布局容器。
 */
function SidebarFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sidebar-footer"
      data-sidebar="footer"
      className={cn("flex flex-col gap-2", className)}
      {...props}
    />
  )
}

/**
 * 侧边栏分隔线。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 Separator 的属性。
 * @returns 渲染一条侧边栏主题色的分隔线。
 */
function SidebarSeparator({
  className,
  ...props
}: React.ComponentProps<typeof Separator>) {
  return (
    <Separator
      data-slot="sidebar-separator"
      data-sidebar="separator"
      className={cn("mx-2 w-auto bg-sidebar-border", className)}
      {...props}
    />
  )
}

/**
 * 侧边栏可滚动内容区域。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染一个可滚动的内容区域，折叠为图标模式时隐藏滚动条。
 */
function SidebarContent({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sidebar-content"
      data-sidebar="content"
      className={cn(
        "no-scrollbar flex min-h-0 flex-1 flex-col gap-0 overflow-auto group-data-[collapsible=icon]:overflow-hidden",
        className
      )}
      {...props}
    />
  )
}

/**
 * 侧边栏菜单分组容器。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染一个垂直布局的分组容器。
 */
function SidebarGroup({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sidebar-group"
      data-sidebar="group"
      className={cn("relative flex w-full min-w-0 flex-col p-2", className)}
      {...props}
    />
  )
}

/**
 * 分组标签文本。
 * @param className - 额外的自定义类名。
 * @param asChild - 是否合并到子元素上。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染分组标题文本，图标模式时自动隐藏。
 */
function SidebarGroupLabel({
  className,
  asChild = false,
  ...props
}: React.ComponentProps<"div"> & { asChild?: boolean }) {
  const Comp = asChild ? Slot.Root : "div"

  return (
    <Comp
      data-slot="sidebar-group-label"
      data-sidebar="group-label"
      className={cn(
        "flex h-8 shrink-0 items-center rounded-md px-2 text-xs font-medium text-sidebar-foreground/70 ring-sidebar-ring outline-hidden transition-[margin,opacity] duration-200 ease-linear group-data-[collapsible=icon]:-mt-8 group-data-[collapsible=icon]:opacity-0 focus-visible:ring-2 [&>svg]:size-4 [&>svg]:shrink-0",
        className
      )}
      {...props}
    />
  )
}

/**
 * 分组操作按钮，定位在分组右上角。
 * @param className - 额外的自定义类名。
 * @param asChild - 是否合并到子元素上。
 * @param props - 透传给 button 元素的属性。
 * @returns 渲染一个右上角的操作按钮，图标模式时隐藏。
 */
function SidebarGroupAction({
  className,
  asChild = false,
  ...props
}: React.ComponentProps<"button"> & { asChild?: boolean }) {
  const Comp = asChild ? Slot.Root : "button"

  return (
    <Comp
      data-slot="sidebar-group-action"
      data-sidebar="group-action"
      className={cn(
        "absolute top-3.5 right-3 flex aspect-square w-5 items-center justify-center rounded-md p-0 text-sidebar-foreground ring-sidebar-ring outline-hidden transition-transform group-data-[collapsible=icon]:hidden after:absolute after:-inset-2 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:ring-2 md:after:hidden [&>svg]:size-4 [&>svg]:shrink-0",
        className
      )}
      {...props}
    />
  )
}

/**
 * 分组内容容器。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染分组的内容区域。
 */
function SidebarGroupContent({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sidebar-group-content"
      data-sidebar="group-content"
      className={cn("w-full text-sm", className)}
      {...props}
    />
  )
}

/**
 * 侧边栏菜单列表容器。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 ul 元素的属性。
 * @returns 渲染一个垂直布局的菜单列表。
 */
function SidebarMenu({ className, ...props }: React.ComponentProps<"ul">) {
  return (
    <ul
      data-slot="sidebar-menu"
      data-sidebar="menu"
      className={cn("flex w-full min-w-0 flex-col gap-0", className)}
      {...props}
    />
  )
}

/**
 * 菜单项，包裹单个菜单按钮及其子元素。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 li 元素的属性。
 * @returns 渲染一个菜单项容器。
 */
function SidebarMenuItem({ className, ...props }: React.ComponentProps<"li">) {
  return (
    <li
      data-slot="sidebar-menu-item"
      data-sidebar="menu-item"
      className={cn("group/menu-item relative", className)}
      {...props}
    />
  )
}

// 菜单按钮样式变体定义
const sidebarMenuButtonVariants = cva(
  "peer/menu-button group/menu-button flex w-full items-center gap-2 overflow-hidden rounded-md p-2 text-left text-sm ring-sidebar-ring outline-hidden transition-[width,height,padding] group-has-data-[sidebar=menu-action]/menu-item:pr-8 group-data-[collapsible=icon]:size-8! group-data-[collapsible=icon]:p-2! hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:ring-2 active:bg-sidebar-accent active:text-sidebar-accent-foreground disabled:pointer-events-none disabled:opacity-50 aria-disabled:pointer-events-none aria-disabled:opacity-50 data-open:hover:bg-sidebar-accent data-open:hover:text-sidebar-accent-foreground data-active:bg-sidebar-accent data-active:font-medium data-active:text-sidebar-accent-foreground [&_svg]:size-4 [&_svg]:shrink-0 [&>span:last-child]:truncate",
  {
    variants: {
      variant: {
        // 默认变体
        default: "hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
        // 描边变体：带阴影边框
        outline:
          "bg-background shadow-[0_0_0_1px_var(--sidebar-border)] hover:bg-sidebar-accent hover:text-sidebar-accent-foreground hover:shadow-[0_0_0_1px_var(--sidebar-accent)]",
      },
      size: {
        // 默认尺寸：高度 32px
        default: "h-8 text-sm",
        // 小尺寸：高度 28px
        sm: "h-7 text-xs",
        // 大尺寸：高度 48px
        lg: "h-12 text-sm group-data-[collapsible=icon]:p-0!",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

/**
 * 菜单按钮，侧边栏中最核心的交互元素。
 * @param asChild - 是否合并到子元素上（用于渲染为 a 链接等）。
 * @param isActive - 是否为当前激活项（高亮显示）。
 * @param variant - 样式变体，"default" 或 "outline"。
 * @param size - 尺寸，"default" / "sm" / "lg"。
 * @param tooltip - 折叠时显示的 Tooltip 提示文本或配置对象。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 button 元素的属性。
 * @returns 渲染菜单按钮，折叠为图标模式时自动添加 Tooltip 提示。
 */
function SidebarMenuButton({
  asChild = false,
  isActive = false,
  variant = "default",
  size = "default",
  tooltip,
  className,
  ...props
}: React.ComponentProps<"button"> & {
  asChild?: boolean
  isActive?: boolean
  tooltip?: string | React.ComponentProps<typeof TooltipContent>
} & VariantProps<typeof sidebarMenuButtonVariants>) {
  const Comp = asChild ? Slot.Root : "button"
  const { isMobile, state } = useSidebar()

  const button = (
    <Comp
      data-slot="sidebar-menu-button"
      data-sidebar="menu-button"
      data-size={size}
      data-active={isActive}
      className={cn(sidebarMenuButtonVariants({ variant, size }), className)}
      {...props}
    />
  )

  // 无 tooltip 时直接返回按钮
  if (!tooltip) {
    return button
  }

  // 字符串类型 tooltip 转换为配置对象
  if (typeof tooltip === "string") {
    tooltip = {
      children: tooltip,
    }
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>{button}</TooltipTrigger>
      <TooltipContent
        side="right"
        align="center"
        // 仅在折叠状态且非移动端时显示 tooltip
        hidden={state !== "collapsed" || isMobile}
        {...tooltip}
      />
    </Tooltip>
  )
}

/**
 * 菜单项操作按钮，定位在菜单项右侧。
 * @param className - 额外的自定义类名。
 * @param asChild - 是否合并到子元素上。
 * @param showOnHover - 是否仅在悬停时显示。
 * @param props - 透传给 button 元素的属性。
 * @returns 渲染一个定位在菜单项右侧的操作按钮。
 */
function SidebarMenuAction({
  className,
  asChild = false,
  showOnHover = false,
  ...props
}: React.ComponentProps<"button"> & {
  asChild?: boolean
  showOnHover?: boolean
}) {
  const Comp = asChild ? Slot.Root : "button"

  return (
    <Comp
      data-slot="sidebar-menu-action"
      data-sidebar="menu-action"
      className={cn(
        "absolute top-1.5 right-1 flex aspect-square w-5 items-center justify-center rounded-md p-0 text-sidebar-foreground ring-sidebar-ring outline-hidden transition-transform group-data-[collapsible=icon]:hidden peer-hover/menu-button:text-sidebar-accent-foreground peer-data-[size=default]/menu-button:top-1.5 peer-data-[size=lg]/menu-button:top-2.5 peer-data-[size=sm]/menu-button:top-1 after:absolute after:-inset-2 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:ring-2 md:after:hidden [&>svg]:size-4 [&>svg]:shrink-0",
        showOnHover &&
          "group-focus-within/menu-item:opacity-100 group-hover/menu-item:opacity-100 peer-data-active/menu-button:text-sidebar-accent-foreground aria-expanded:opacity-100 md:opacity-0",
        className
      )}
      {...props}
    />
  )
}

/**
 * 菜单项徽章，定位在菜单项右侧显示数字或标记。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染一个右对齐的徽章容器，图标模式时隐藏。
 */
function SidebarMenuBadge({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sidebar-menu-badge"
      data-sidebar="menu-badge"
      className={cn(
        "pointer-events-none absolute right-1 flex h-5 min-w-5 items-center justify-center rounded-md px-1 text-xs font-medium text-sidebar-foreground tabular-nums select-none group-data-[collapsible=icon]:hidden peer-hover/menu-button:text-sidebar-accent-foreground peer-data-[size=default]/menu-button:top-1.5 peer-data-[size=lg]/menu-button:top-2.5 peer-data-[size=sm]/menu-button:top-1 peer-data-active/menu-button:text-sidebar-accent-foreground",
        className
      )}
      {...props}
    />
  )
}

/**
 * 菜单项骨架屏，用于加载状态占位。
 * @param className - 额外的自定义类名。
 * @param showIcon - 是否显示图标占位。
 * @param props - 透传给 div 元素的属性。
 * @returns 渲染一个带随机宽度文本占位和可选图标占位的骨架屏。
 */
function SidebarMenuSkeleton({
  className,
  showIcon = false,
  ...props
}: React.ComponentProps<"div"> & {
  showIcon?: boolean
}) {
  // 随机生成 50%~90% 的宽度
  const [width] = React.useState(() => {
    return `${Math.floor(Math.random() * 40) + 50}%`
  })

  return (
    <div
      data-slot="sidebar-menu-skeleton"
      data-sidebar="menu-skeleton"
      className={cn("flex h-8 items-center gap-2 rounded-md px-2", className)}
      {...props}
    >
      {showIcon && (
        <Skeleton
          className="size-4 rounded-md"
          data-sidebar="menu-skeleton-icon"
        />
      )}
      <Skeleton
        className="h-4 max-w-(--skeleton-width) flex-1"
        data-sidebar="menu-skeleton-text"
        style={
          {
            "--skeleton-width": width,
          } as React.CSSProperties
        }
      />
    </div>
  )
}

/**
 * 菜单子菜单列表（二级菜单）。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 ul 元素的属性。
 * @returns 渲染带左边框线的子菜单列表，图标模式时隐藏。
 */
function SidebarMenuSub({ className, ...props }: React.ComponentProps<"ul">) {
  return (
    <ul
      data-slot="sidebar-menu-sub"
      data-sidebar="menu-sub"
      className={cn(
        "mx-3.5 flex min-w-0 translate-x-px flex-col gap-1 border-l border-sidebar-border px-2.5 py-0.5 group-data-[collapsible=icon]:hidden",
        className
      )}
      {...props}
    />
  )
}

/**
 * 子菜单项。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 li 元素的属性。
 * @returns 渲染一个子菜单项容器。
 */
function SidebarMenuSubItem({
  className,
  ...props
}: React.ComponentProps<"li">) {
  return (
    <li
      data-slot="sidebar-menu-sub-item"
      data-sidebar="menu-sub-item"
      className={cn("group/menu-sub-item relative", className)}
      {...props}
    />
  )
}

/**
 * 子菜单按钮（二级菜单项的链接按钮）。
 * @param asChild - 是否合并到子元素上。
 * @param size - 尺寸，"sm" 或 "md"（默认）。
 * @param isActive - 是否为当前激活项。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 a 元素的属性。
 * @returns 渲染一个子菜单链接按钮，图标模式时隐藏。
 */
function SidebarMenuSubButton({
  asChild = false,
  size = "md",
  isActive = false,
  className,
  ...props
}: React.ComponentProps<"a"> & {
  asChild?: boolean
  size?: "sm" | "md"
  isActive?: boolean
}) {
  const Comp = asChild ? Slot.Root : "a"

  return (
    <Comp
      data-slot="sidebar-menu-sub-button"
      data-sidebar="menu-sub-button"
      data-size={size}
      data-active={isActive}
      className={cn(
        "flex h-7 min-w-0 -translate-x-px items-center gap-2 overflow-hidden rounded-md px-2 text-sidebar-foreground ring-sidebar-ring outline-hidden group-data-[collapsible=icon]:hidden hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:ring-2 active:bg-sidebar-accent active:text-sidebar-accent-foreground disabled:pointer-events-none disabled:opacity-50 aria-disabled:pointer-events-none aria-disabled:opacity-50 data-[size=md]:text-sm data-[size=sm]:text-xs data-active:bg-sidebar-accent data-active:text-sidebar-accent-foreground [&>span:last-child]:truncate [&>svg]:size-4 [&>svg]:shrink-0 [&>svg]:text-sidebar-accent-foreground",
        className
      )}
      {...props}
    />
  )
}

export {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupAction,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInput,
  SidebarInset,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSkeleton,
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
  SidebarProvider,
  SidebarRail,
  SidebarSeparator,
  SidebarTrigger,
  useSidebar,
}

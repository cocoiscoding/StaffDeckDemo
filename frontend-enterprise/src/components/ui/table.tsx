/**
 * @file table.tsx
 * @description 表格组件，由多个子组件组合而成（Table / TableHeader / TableBody / TableFooter /
 *              TableRow / TableHead / TableCell / TableCaption）。
 *              外层包裹了水平滚动容器，适配数据量较大的表格场景。
 *              本项目中作为 shadcn/ui 基础组件，在数据列表、报表等场景中使用。
 */

"use client"

import * as React from "react"

import { cn } from "@/lib/utils"

/**
 * 表格容器，外层包裹水平滚动容器。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 table 元素的属性。
 * @returns 渲染一个带水平滚动包裹的表格。
 */
function Table({ className, ...props }: React.ComponentProps<"table">) {
  return (
    <div
      data-slot="table-container"
      className="relative w-full overflow-x-auto"
    >
      <table
        data-slot="table"
        className={cn("w-full caption-bottom text-sm", className)}
        {...props}
      />
    </div>
  )
}

/**
 * 表格头部（thead），包裹列标题行。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 thead 元素的属性。
 * @returns 渲染表格头部，行带底部边框。
 */
function TableHeader({ className, ...props }: React.ComponentProps<"thead">) {
  return (
    <thead
      data-slot="table-header"
      className={cn("[&_tr]:border-b", className)}
      {...props}
    />
  )
}

/**
 * 表格主体（tbody），包裹数据行。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 tbody 元素的属性。
 * @returns 渲染表格主体区域。
 */
function TableBody({ className, ...props }: React.ComponentProps<"tbody">) {
  return (
    <tbody
      data-slot="table-body"
      className={cn("[&_tr:last-child]:border-0", className)}
      {...props}
    />
  )
}

/**
 * 表格底部（tfoot），通常用于汇总行。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 tfoot 元素的属性。
 * @returns 渲染带灰色背景和顶部边框的表格底部。
 */
function TableFooter({ className, ...props }: React.ComponentProps<"tfoot">) {
  return (
    <tfoot
      data-slot="table-footer"
      className={cn(
        "border-t bg-muted/50 font-medium [&>tr]:last:border-b-0",
        className
      )}
      {...props}
    />
  )
}

/**
 * 表格行（tr），支持悬停高亮和选中状态。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 tr 元素的属性。
 * @returns 渲染一个带底部边框的表格行，悬停时变灰。
 */
function TableRow({ className, ...props }: React.ComponentProps<"tr">) {
  return (
    <tr
      data-slot="table-row"
      className={cn(
        "border-b transition-colors hover:bg-muted/50 has-aria-expanded:bg-muted/50 data-[state=selected]:bg-muted",
        className
      )}
      {...props}
    />
  )
}

/**
 * 表头单元格（th）。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 th 元素的属性。
 * @returns 渲染一个左对齐、中等字宽的表头单元格。
 */
function TableHead({ className, ...props }: React.ComponentProps<"th">) {
  return (
    <th
      data-slot="table-head"
      className={cn(
        "h-10 px-2 text-left align-middle font-medium text-foreground has-[[role=checkbox]]:pr-0",
        className
      )}
      {...props}
    />
  )
}

/**
 * 表格数据单元格（td）。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 td 元素的属性。
 * @returns 渲染一个垂直居中的数据单元格。
 */
function TableCell({ className, ...props }: React.ComponentProps<"td">) {
  return (
    <td
      data-slot="table-cell"
      className={cn(
        "p-2 align-middle has-[[role=checkbox]]:pr-0",
        className
      )}
      {...props}
    />
  )
}

/**
 * 表格标题（caption），描述表格内容。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 caption 元素的属性。
 * @returns 渲染表格底部灰色描述文本。
 */
function TableCaption({
  className,
  ...props
}: React.ComponentProps<"caption">) {
  return (
    <caption
      data-slot="table-caption"
      className={cn("mt-4 text-sm text-muted-foreground", className)}
      {...props}
    />
  )
}

export {
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableHead,
  TableRow,
  TableCell,
  TableCaption,
}

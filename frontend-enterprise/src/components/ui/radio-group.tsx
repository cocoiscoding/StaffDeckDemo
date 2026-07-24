/**
 * @file radio-group.tsx
 * @description 单选按钮组组件，基于 Radix UI 的 RadioGroup 原语封装。
 *              由 RadioGroup（容器）和 RadioGroupItem（单个选项）组成，
 *              同一组内只能选中一个选项。
 *              本项目中作为 shadcn/ui 基础组件，在表单单选场景中使用。
 */

"use client"

import * as React from "react"
import { RadioGroup as RadioGroupPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

/**
 * 单选按钮组容器。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 Radix RadioGroup Root 的属性（如 value、onValueChange 等）。
 * @returns 渲染一个网格布局的单选组容器。
 */
function RadioGroup({
  className,
  ...props
}: React.ComponentProps<typeof RadioGroupPrimitive.Root>) {
  return (
    <RadioGroupPrimitive.Root
      data-slot="radio-group"
      className={cn("grid w-full gap-2", className)}
      {...props}
    />
  )
}

/**
 * 单个单选按钮项。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 Radix RadioGroup Item 的属性（如 value 等）。
 * @returns 渲染一个圆形单选按钮，选中时背景变为主色调并显示内圆点。
 */
function RadioGroupItem({
  className,
  ...props
}: React.ComponentProps<typeof RadioGroupPrimitive.Item>) {
  return (
    <RadioGroupPrimitive.Item
      data-slot="radio-group-item"
      className={cn(
        "group/radio-group-item peer relative flex aspect-square size-4 shrink-0 rounded-full border border-input outline-none after:absolute after:-inset-x-3 after:-inset-y-2 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 aria-invalid:aria-checked:border-primary data-checked:border-primary data-checked:bg-primary data-checked:text-primary-foreground",
        className
      )}
      {...props}
    >
      {/* 选中状态指示器，显示居中的小圆点 */}
      <RadioGroupPrimitive.Indicator
        data-slot="radio-group-indicator"
        className="flex size-4 items-center justify-center"
      >
        <span className="absolute top-1/2 left-1/2 size-2 -translate-x-1/2 -translate-y-1/2 rounded-full bg-primary-foreground" />
      </RadioGroupPrimitive.Indicator>
    </RadioGroupPrimitive.Item>
  )
}

export { RadioGroup, RadioGroupItem }

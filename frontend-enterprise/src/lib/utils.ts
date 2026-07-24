/**
 * @file 通用工具函数模块。
 *
 * 当前仅提供 CSS 类名合并工具 `cn`，是整个前端项目中最基础的工具函数，
 * 被几乎所有 UI 组件引用。结合 clsx（条件类名拼接）和 tailwind-merge（Tailwind 冲突去重）。
 */

import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

/**
 * 合并 CSS 类名，自动处理条件表达式和 Tailwind 类名冲突。
 *
 * 工作原理（两步管道）：
 * 1. `clsx` — 将各种形式的输入（字符串、对象、数组、布尔条件）展平为空格分隔的类名字符串；
 * 2. `twMerge` — 对结果中的 Tailwind 类名做智能去重，后定义的覆盖先定义的
 *    （如 `"px-2 px-4"` → `"px-4"`）。
 *
 * @param inputs 任意形式的类名输入（字符串、条件对象、数组等）
 * @returns 合并去重后的最终类名字符串
 *
 * @example
 * cn('px-2', condition && 'text-red-500', { 'hidden': !visible })
 * // => "px-2 text-red-500" 或 "px-2 hidden"
 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

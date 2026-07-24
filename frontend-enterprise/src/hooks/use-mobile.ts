/**
 * @file 移动端视口检测 Hook。
 *
 * 提供响应式的设备类型判断，用于侧边栏折叠、布局切换等移动端适配场景。
 * 通过监听 CSS 媒体查询断点变化，实时反映当前视口是否处于移动端宽度。
 */

import * as React from "react"

/** 移动端宽度断点（px），视口宽度 < 此值视为移动端 */
const MOBILE_BREAKPOINT = 768

/**
 * 检测当前视口是否为移动端尺寸。
 *
 * 实现：
 * - 初始值为 `undefined`（首帧 SSR 安全），首次 `useEffect` 执行后确定真实值；
 * - 通过 `window.matchMedia` 注册媒体查询变化监听，视口跨越断点时自动更新；
 * - 组件卸载时移除监听，避免内存泄漏。
 *
 * @returns 当前视口宽度是否小于移动端断点（`true` = 移动端）
 */
export function useIsMobile() {
  const [isMobile, setIsMobile] = React.useState<boolean | undefined>(undefined)

  React.useEffect(() => {
    // 监听断点变化的媒体查询（max-width: 767px）
    const mql = window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT - 1}px)`)
    const onChange = () => {
      setIsMobile(window.innerWidth < MOBILE_BREAKPOINT)
    }
    mql.addEventListener("change", onChange)
    // 初始化时立即同步当前状态
    setIsMobile(window.innerWidth < MOBILE_BREAKPOINT)
    return () => mql.removeEventListener("change", onChange)
  }, [])

  // !! 将 undefined 转为 false，保证返回值始终为 boolean
  return !!isMobile
}

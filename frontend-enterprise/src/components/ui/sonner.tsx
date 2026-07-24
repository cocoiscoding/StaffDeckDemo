/**
 * @file sonner.tsx
 * @description Toast 通知容器组件，基于 sonner 库的 Toaster 封装。
 *              配置了项目自定义的图标主题（success/info/warning/error/loading）和 CSS 变量，
 *              使 Toast 样式与项目整体设计规范保持一致。
 *              本项目中作为全局 Toast 渲染容器，通常在应用根节点挂载一次。
 */

import type { CSSProperties } from 'react';
import { CircleCheckIcon, InfoIcon, Loader2Icon, OctagonXIcon, TriangleAlertIcon } from 'lucide-react';
import { Toaster as Sonner, type ToasterProps } from 'sonner';

/**
 * Toast 容器组件，配置主题色、图标和圆角等全局样式。
 * @param props - 透传给 sonner Toaster 的属性。
 * @returns 渲染一个配置好主题色和图标的 Toast 容器。
 */
const Toaster = ({ ...props }: ToasterProps) => {
  return (
    <Sonner
      theme="light"
      className="toaster group"
      // 自定义各状态图标
      icons={{
        success: <CircleCheckIcon className="size-4" />,
        info: <InfoIcon className="size-4" />,
        warning: <TriangleAlertIcon className="size-4" />,
        error: <OctagonXIcon className="size-4" />,
        loading: <Loader2Icon className="size-4 animate-spin" />,
      }}
      // 通过 CSS 变量对齐项目设计令牌
      style={
        {
          '--normal-bg': 'var(--popover)',
          '--normal-text': 'var(--popover-foreground)',
          '--normal-border': 'var(--border)',
          '--border-radius': 'var(--radius)',
        } as CSSProperties
      }
      toastOptions={{
        classNames: {
          toast: 'cn-toast',
        },
      }}
      {...props}
    />
  );
};

export { Toaster };

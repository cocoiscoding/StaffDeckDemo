/**
 * @file pagination.tsx
 * @description 分页组件，由多个子组件组合而成（Pagination / PaginationContent / PaginationItem /
 *              PaginationLink / PaginationPrevious / PaginationNext / PaginationEllipsis）。
 *              复用 Button 的 buttonVariants 样式实现页码按钮外观。
 *              本项目中作为 shadcn/ui 基础组件，用于列表、表格等数据的分页导航。
 */

import * as React from 'react';
import { ChevronLeftIcon, ChevronRightIcon, MoreHorizontalIcon } from 'lucide-react';

import { cn } from '@/lib/utils';
import { buttonVariants } from './button';
import type { Button } from './button';

/**
 * 分页导航容器。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 nav 元素的属性。
 * @returns 渲染一个居中对齐的分页导航栏，带有 aria-label="pagination"。
 */
function Pagination({ className, ...props }: React.ComponentProps<'nav'>) {
  return (
    <nav
      role="navigation"
      aria-label="pagination"
      data-slot="pagination"
      className={cn('mx-auto flex w-full justify-center', className)}
      {...props}
    />
  );
}

/**
 * 分页内容列表容器，包裹所有页码项。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 ul 元素的属性。
 * @returns 渲染一个水平排列的列表。
 */
function PaginationContent({ className, ...props }: React.ComponentProps<'ul'>) {
  return (
    <ul
      data-slot="pagination-content"
      className={cn('flex flex-row items-center gap-1', className)}
      {...props}
    />
  );
}

/**
 * 分页列表项，包裹单个页码链接。
 * @param props - 透传给 li 元素的属性。
 * @returns 渲染一个列表项元素。
 */
function PaginationItem({ ...props }: React.ComponentProps<'li'>) {
  return <li data-slot="pagination-item" {...props} />;
}

/** 分页链接属性类型 */
type PaginationLinkProps = {
  isActive?: boolean;
} & Pick<React.ComponentProps<typeof Button>, 'size'> &
  React.ComponentProps<'a'>;

/**
 * 分页页码链接。
 * @param className - 额外的自定义类名。
 * @param isActive - 是否为当前页，活跃页使用 outline 样式。
 * @param size - 按钮尺寸，默认为 "icon"。
 * @param props - 透传给 a 元素的属性。
 * @returns 渲染一个使用 buttonVariants 样式的页码链接。
 */
function PaginationLink({ className, isActive, size = 'icon', ...props }: PaginationLinkProps) {
  return (
    <a
      aria-current={isActive ? 'page' : undefined}
      data-slot="pagination-link"
      data-active={isActive}
      className={cn(
        buttonVariants({
          // 当前页使用 outline 样式，非当前页使用 ghost 样式
          variant: isActive ? 'outline' : 'ghost',
          size,
        }),
        className,
      )}
      {...props}
    />
  );
}

/**
 * 上一页按钮。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 PaginationLink 的属性。
 * @returns 渲染一个带左箭头图标的上一页链接。
 */
function PaginationPrevious({ className, ...props }: React.ComponentProps<typeof PaginationLink>) {
  return (
    <PaginationLink
      aria-label="Go to previous page"
      size="default"
      className={cn('gap-1 px-2.5 sm:pl-2.5', className)}
      {...props}
    >
      <ChevronLeftIcon />
      <span className="hidden sm:block">Previous</span>
    </PaginationLink>
  );
}

/**
 * 下一页按钮。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 PaginationLink 的属性。
 * @returns 渲染一个带右箭头图标的下一页链接。
 */
function PaginationNext({ className, ...props }: React.ComponentProps<typeof PaginationLink>) {
  return (
    <PaginationLink
      aria-label="Go to next page"
      size="default"
      className={cn('gap-1 px-2.5 sm:pr-2.5', className)}
      {...props}
    >
      <span className="hidden sm:block">Next</span>
      <ChevronRightIcon />
    </PaginationLink>
  );
}

/**
 * 分页省略号，用于跳过中间的页码。
 * @param className - 额外的自定义类名。
 * @param props - 透传给 span 元素的属性。
 * @returns 渲染一个三点省略号图标，带屏幕阅读器提示文本。
 */
function PaginationEllipsis({ className, ...props }: React.ComponentProps<'span'>) {
  return (
    <span
      aria-hidden
      data-slot="pagination-ellipsis"
      className={cn('flex size-9 items-center justify-center', className)}
      {...props}
    >
      <MoreHorizontalIcon className="size-4" />
      <span className="sr-only">More pages</span>
    </span>
  );
}

export {
  Pagination,
  PaginationContent,
  PaginationLink,
  PaginationItem,
  PaginationPrevious,
  PaginationNext,
  PaginationEllipsis,
};

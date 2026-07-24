/**
 * @file Paginator.tsx
 * @module components/Paginator
 * @description 分页器组件。基于 shadcn Pagination 原语构建，提供 SD1 设计风格的分页：
 *              20px 圆角页码按钮、10px 零填充页码、#f6f6f6 激活态填充、左右箭头导航。
 *              支持省略号（ellipsis）折叠和零填充数字显示。
 */

import { ChevronLeftIcon, ChevronRightIcon } from 'lucide-react';

import { cn } from '@/lib/utils';

import { Pagination, PaginationContent, PaginationItem } from './ui';

/**
 * 分页器组件的属性定义。
 */
export type PaginatorProps = {
  /** 当前页码（1-based）。 */
  page: number;
  /** 总页数。 */
  pageCount: number;
  /** 页码变更回调。 */
  onChange: (page: number) => void;
  /** 当前页两侧各显示的页码数量。 */
  siblingCount?: number;
  /** 是否零填充页码（01, 02, …），默认 true。 */
  padZero?: boolean;
  /** 附加 CSS 类名。 */
  className?: string;
  /** 无障碍标签。 */
  'aria-label'?: string;
};

/** 页码按钮基础样式 */
const PILL_CLASS =
  'flex h-[20px] min-w-[20px] items-center justify-center rounded-[6px] px-[12px] text-[10px] leading-none transition-colors';
/** 箭头按钮基础样式 */
const ARROW_CLASS =
  'flex size-[14px] shrink-0 items-center justify-center text-[#464c5e] transition-opacity disabled:cursor-not-allowed disabled:opacity-30';

/**
 * 计算分页范围。生成包含首页、末页、当前页附近页码和省略号的数组。
 *
 * @param current - 当前页码
 * @param total - 总页数
 * @param siblingCount - 当前页两侧显示的页码数
 * @returns 分页范围数组（数字或 'ellipsis' 标记）
 */
function getPaginationRange(
  current: number,
  total: number,
  siblingCount: number,
): (number | 'ellipsis')[] {
  // 总显示页码数 = 两侧兄弟页码 + 首页 + 末页 + 当前页 + 两个省略号位
  const totalNumbers = siblingCount * 2 + 5;
  // 页数不足时全部展示
  if (total <= totalNumbers) {
    return Array.from({ length: total }, (_, index) => index + 1);
  }
  const items: (number | 'ellipsis')[] = [1];
  const start = Math.max(2, current - siblingCount);
  const end = Math.min(total - 1, current + siblingCount);
  // 左侧需要省略号
  if (start > 2) items.push('ellipsis');
  for (let page = start; page <= end; page += 1) items.push(page);
  // 右侧需要省略号
  if (end < total - 1) items.push('ellipsis');
  items.push(total);
  return items;
}

/**
 * 分页器组件。基于 shadcn Pagination 原语，提供 SD1 设计风格的分页交互。
 *
 * @param props - 组件属性
 * @param props.page - 当前页码（1-based）
 * @param props.pageCount - 总页数
 * @param props.onChange - 页码变更回调
 * @param props.siblingCount - 当前页两侧页码数，默认 1
 * @param props.padZero - 是否零填充页码，默认 true
 * @param props.className - 附加类名
 * @returns 渲染好的分页器，总页数 < 1 时返回 null
 */
export function Paginator({
  page,
  pageCount,
  onChange,
  siblingCount = 1,
  padZero = true,
  className,
  'aria-label': ariaLabel,
}: PaginatorProps) {
  if (pageCount < 1) return null;
  const range = getPaginationRange(page, pageCount, siblingCount);
  // 页码标签格式化：零填充或普通数字
  const label = (value: number) => (padZero ? String(value).padStart(2, '0') : String(value));
  /** 安全跳转到指定页码（限制在有效范围内） */
  const goTo = (target: number) => {
    const next = Math.min(Math.max(target, 1), pageCount);
    if (next !== page) onChange(next);
  };
  return (
    <Pagination aria-label={ariaLabel} className={cn('mt-[16px]', className)}>
      <PaginationContent className="gap-[16px]">
        {/* 上一页箭头 */}
        <PaginationItem>
          <button
            type="button"
            className={ARROW_CLASS}
            disabled={page <= 1}
            onClick={() => goTo(page - 1)}
            aria-label="上一页"
          >
            <ChevronLeftIcon className="size-[14px]" />
          </button>
        </PaginationItem>
        {/* 页码按钮 / 省略号 */}
        {range.map((item, index) =>
          item === 'ellipsis' ? (
            <PaginationItem key={`ellipsis-${index}`}>
              <span
                aria-hidden="true"
                className="flex h-[20px] items-center justify-center px-[4px] text-[10px] leading-none text-[#999]"
              >
                ···
              </span>
            </PaginationItem>
          ) : (
            <PaginationItem key={item}>
              <button
                type="button"
                aria-current={item === page ? 'page' : undefined}
                onClick={() => goTo(item)}
                className={cn(
                  PILL_CLASS,
                  // 当前页使用高亮背景
                  item === page
                    ? 'bg-[#f6f6f6] text-[#464c5e]'
                    : 'text-[#999] hover:bg-[#f2f3f7] hover:text-[#464c5e]',
                )}
              >
                {label(item)}
              </button>
            </PaginationItem>
          ),
        )}
        {/* 下一页箭头 */}
        <PaginationItem>
          <button
            type="button"
            className={ARROW_CLASS}
            disabled={page >= pageCount}
            onClick={() => goTo(page + 1)}
            aria-label="下一页"
          >
            <ChevronRightIcon className="size-[14px]" />
          </button>
        </PaginationItem>
      </PaginationContent>
    </Pagination>
  );
}

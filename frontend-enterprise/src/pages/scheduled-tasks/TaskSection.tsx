/**
 * @file scheduled-tasks/TaskSection.tsx
 * @description 定时任务页面的通用列表分区组件。
 *
 * 该组件是一个泛型展示组件，被任务列表和执行记录列表共用。
 * 结构包含：
 * 1. 区块标题栏（图标 + 标题）
 * 2. 下划线筛选标签栏（UnderlineTabs）
 * 3. 移动端卡片列表（<768px）
 * 4. 桌面端数据表格（DataTable）+ 分页器（Paginator）
 *
 * 通过泛型参数 TFilter 和 TRow 实现类型安全的筛选与行数据绑定。
 */

import type { ReactNode } from 'react';

import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { Paginator } from '@/components/Paginator';
import { UnderlineTabs, type UnderlineTabItem } from '@/components/ui';

export type TaskSectionProps<TFilter extends string, TRow> = {
  /** 区块标题前的引导图标（14px） */
  icon: ReactNode;
  /** 区块标题文字 */
  title: string;
  /** 筛选标签栏配置项数组 */
  filterTabs: UnderlineTabItem<TFilter>[];
  /** 当前激活的筛选值 */
  filter: TFilter;
  /** 筛选值变更回调 */
  onFilterChange: (value: TFilter) => void;
  /** 经筛选后的全部数据行，驱动移动端列表渲染与分页器显隐 */
  rows: TRow[];
  /** 当前页的数据行，驱动桌面端表格渲染 */
  pagedRows: TRow[];
  /** 桌面端表格列定义 */
  columns: DataTableColumn<TRow>[];
  /** 行唯一键的生成函数 */
  rowKey: (row: TRow, index: number) => string | number;
  /** 是否处于加载态 */
  loading?: boolean;
  /** 空数据时的占位文案 */
  emptyText: string;
  /** 表格尺寸：默认或紧凑 */
  tableSize?: 'default' | 'compact';
  /** 表格行是否斑马纹 */
  striped?: boolean;
  /** 表格是否显示边框 */
  bordered?: boolean;
  /** 当前页码 */
  page: number;
  /** 总页数 */
  pageCount: number;
  /** 翻页回调 */
  onPageChange: (page: number) => void;
  /** 移动端（<768px）单行卡片的渲染函数 */
  renderMobileCard: (row: TRow) => ReactNode;
};

/**
 * 带标题与筛选的响应式列表分区组件。
 *
 * 桌面端（≥768px）使用 DataTable + Paginator 展示数据；
 * 移动端（<768px）使用 renderMobileCard 渲染卡片列表。
 * 任务列表和执行记录列表均复用此组件。
 *
 * @typeParam TFilter 筛选值的字符串字面量类型
 * @typeParam TRow 行数据类型
 * @param props 组件属性，见 TaskSectionProps
 * @returns 渲染好的 section 区块
 */
export function TaskSection<TFilter extends string, TRow>({
  icon,
  title,
  filterTabs,
  filter,
  onFilterChange,
  rows,
  pagedRows,
  columns,
  rowKey,
  loading,
  emptyText,
  tableSize = 'default',
  striped = false,
  bordered = false,
  page,
  pageCount,
  onPageChange,
  renderMobileCard,
}: TaskSectionProps<TFilter, TRow>) {
  return (
    <section aria-label={title}>
      {/* 区块标题：图标 + 文字 */}
      <div className="mb-[16px] flex items-center gap-[6px] px-[12px] text-[#757f9c]">
        {icon}
        <span className="text-[14px] font-normal leading-none">{title}</span>
      </div>
      {/* 筛选标签栏 */}
      <UnderlineTabs
        aria-label={`${title}筛选`}
        variant="line"
        className="mb-[16px]"
        value={filter}
        onChange={onFilterChange}
        items={filterTabs}
      />
      {/* 移动端：卡片列表 */}
      <div className="grid gap-[10px] md:hidden">
        {rows.length ? (
          rows.map(renderMobileCard)
        ) : (
          <div className="py-[40px] text-center text-[13px] text-[#858b9c]">{emptyText}</div>
        )}
      </div>
      {/* 桌面端：数据表格 + 分页器 */}
      <div className="hidden md:block">
        <DataTable
          aria-label={title}
          columns={columns}
          data={pagedRows}
          rowKey={rowKey}
          loading={loading}
          emptyText={emptyText}
          size={tableSize}
          striped={striped}
          bordered={bordered}
        />
        {rows.length > 0 && (
          <Paginator
            aria-label={`${title}分页`}
            page={page}
            pageCount={pageCount}
            onChange={onPageChange}
          />
        )}
      </div>
    </section>
  );
}

/**
 * @file DataTable.tsx
 * @module components/DataTable
 * @description 业务数据表格组件。基于 shadcn Table 原语构建，提供产品特定的样式：
 *              圆角 #f2f3f7 边框、灰色表头行、白色数据行、细线分隔。
 *              支持行点击、斑马纹、全网格边框、紧凑/默认行高、空状态/加载状态。
 */

import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from './ui';

/**
 * 数据表格列配置类型。
 */
export type DataTableColumn<T> = {
  /** 列唯一键。 */
  key: string;
  /** 表头单元格内容。 */
  title: ReactNode;
  /** 单元格渲染函数。省略时回退到 row[dataIndex]。 */
  render?: (row: T, index: number) => ReactNode;
  /** 无 render 时读取普通字段的快捷方式。 */
  dataIndex?: keyof T;
  /** 固定列宽（px 数字或任意 CSS 宽度值）。 */
  width?: number | string;
  /** 单元格对齐方式。 */
  align?: 'left' | 'center' | 'right';
  /** 数据单元格的附加 CSS 类名。 */
  className?: string;
  /** 表头单元格的附加 CSS 类名。 */
  headClassName?: string;
};

/**
 * 数据表格组件的属性定义。
 */
export type DataTableProps<T> = {
  /** 列配置数组。 */
  columns: DataTableColumn<T>[];
  /** 数据数组。 */
  data: T[];
  /** 行键生成函数。 */
  rowKey: (row: T, index: number) => string | number;
  /** 是否处于加载状态。 */
  loading?: boolean;
  /** 空数据时的占位文本。 */
  emptyText?: ReactNode;
  /** 加载中的占位文本。 */
  loadingText?: ReactNode;
  /** 行点击回调。 */
  onRowClick?: (row: T, index: number) => void;
  /** 数据行高度。default = 64px，compact = 46px（SD1 执行日志风格）。 */
  size?: 'default' | 'compact';
  /** 斑马纹：偶数行使用 #fbfbfb 浅色填充。 */
  striped?: boolean;
  /** 全网格：每个单元格都有边框，而非仅行间分隔线。 */
  bordered?: boolean;
  /** 外层圆角容器的附加 CSS 类名。 */
  className?: string;
  /** 无障碍标签。 */
  'aria-label'?: string;
};

/** 对齐方式 CSS 类名映射 */
const ALIGN_CLASS = {
  left: 'text-left',
  center: 'text-center',
  right: 'text-right',
} as const;

/** 表头单元格样式 */
const HEAD_CELL_CLASS =
  'h-[36px] bg-[#f2f3f7] px-[16px] py-[12px] align-middle text-[12px] font-normal text-[#464c5e]';
/** 数据单元格样式 */
const BODY_CELL_CLASS = 'px-[16px] py-[12px] align-middle text-[12px] text-[#858b9c]';
/** 行高样式映射 */
const BODY_HEIGHT = {
  default: 'min-h-[64px]',
  compact: 'min-h-[46px]',
} as const;
/** 单元格边框样式 */
const CELL_BORDER = 'border border-[#f2f3f7]';

/**
 * 业务数据表格组件。基于 shadcn Table 原语，提供产品特定的样式。
 * 支持自定义列渲染、行点击、斑马纹、全网格边框、紧凑/默认行高等特性。
 *
 * @param props - 组件属性
 * @param props.columns - 列配置
 * @param props.data - 数据数组
 * @param props.rowKey - 行键函数
 * @param props.loading - 是否加载中，默认 false
 * @param props.emptyText - 空数据文本，默认"暂无数据"
 * @param props.loadingText - 加载文本，默认"加载中…"
 * @param props.onRowClick - 行点击回调
 * @param props.size - 行高模式，默认 default
 * @param props.striped - 是否斑马纹，默认 false
 * @param props.bordered - 是否全网格边框，默认 false
 * @param props.className - 附加类名
 * @returns 渲染好的数据表格
 */
export function DataTable<T>({
  columns,
  data,
  rowKey,
  loading = false,
  emptyText = '暂无数据',
  loadingText = '加载中…',
  onRowClick,
  size = 'default',
  striped = false,
  bordered = false,
  className,
  'aria-label': ariaLabel,
}: DataTableProps<T>) {
  const hasData = data.length > 0;
  return (
    <div
      className={cn(
        'overflow-hidden rounded-[14px] border border-[#f2f3f7]',
        className,
      )}
    >
      <Table className="w-full table-fixed text-[12px]" aria-label={ariaLabel}>
        {/* 表头 */}
        <TableHeader>
          <TableRow className="border-0 hover:bg-transparent">
            {columns.map((column) => (
              <TableHead
                key={column.key}
                style={column.width ? { width: column.width } : undefined}
                className={cn(
                  HEAD_CELL_CLASS,
                  bordered && CELL_BORDER,
                  ALIGN_CLASS[column.align ?? 'left'],
                  column.headClassName,
                )}
              >
                {column.title}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        {/* 表体 */}
        <TableBody>
          {hasData ? (
            data.map((row, index) => (
              <TableRow
                key={rowKey(row, index)}
                onClick={onRowClick ? () => onRowClick(row, index) : undefined}
                className={cn(
                  'has-aria-expanded:bg-transparent',
                  bordered
                    ? 'border-0'
                    : 'border-b border-[#f2f3f7] last:border-0',
                  // 斑马纹：奇数索引行使用浅色背景
                  striped
                    ? index % 2 === 1
                      ? 'bg-[#fbfbfb] hover:bg-[#f2f3f7]'
                      : 'bg-white hover:bg-[#f2f3f7]'
                    : 'hover:bg-[#fafbfc]',
                  onRowClick && 'cursor-pointer',
                )}
              >
                {columns.map((column) => (
                  <TableCell
                    key={column.key}
                    className={cn(
                      BODY_CELL_CLASS,
                      BODY_HEIGHT[size],
                      bordered && CELL_BORDER,
                      ALIGN_CLASS[column.align ?? 'left'],
                      column.className,
                    )}
                  >
                    {/* 单元格内容：优先使用 render 函数，其次 dataIndex 取值 */}
                    {column.render
                      ? column.render(row, index)
                      : column.dataIndex != null
                        ? (row[column.dataIndex] as ReactNode)
                        : null}
                  </TableCell>
                ))}
              </TableRow>
            ))
          ) : (
            // 空数据/加载状态占位行
            <TableRow className="hover:bg-transparent">
              <TableCell colSpan={columns.length} className="h-[160px] text-center align-middle text-[13px] text-[#858b9c]">
                {loading ? loadingText : emptyText}
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </div>
  );
}

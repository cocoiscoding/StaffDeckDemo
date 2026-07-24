/**
 * @file 客户端分页 Hook。
 *
 * 为内存中的数组列表提供纯前端的分页能力，无需后端分页接口。
 * 适用于数据量不大、可一次性加载到前端的列表场景（如当前租户下的员工列表、技能列表等）。
 *
 * 核心概念：
 * - **page**：从 1 开始的当前页码。
 * - **pageCount**：总页数，根据 `items.length / pageSize` 向上取整，最小为 1。
 * - **resetKey**：当此依赖值变化时（如切换筛选条件），自动重置回第 1 页。
 */

import { useEffect, useMemo, useState } from 'react';

/**
 * 客户端分页 Hook 的返回值。
 *
 * @typeparam T 列表项类型
 */
export type ClientPagination<T> = {
  /** 当前页码（从 1 开始） */
  page: number;
  /** 设置当前页码的函数 */
  setPage: (page: number) => void;
  /** 总页数（永远不会小于 1） */
  pageCount: number;
  /** 当前页包含的列表项 */
  pagedItems: T[];
};

/**
 * 对内存中的列表进行客户端分页。
 *
 * 行为细节：
 * - **页码收敛**：当列表缩短导致总页数减少时，自动将当前页码收敛到有效范围内。
 * - **重置触发**：当 `resetKey` 变化时（如筛选条件切换），自动跳回第 1 页。
 * - **性能优化**：使用 `useMemo` 缓存当前页切片结果，仅当 `items`、`page`、`pageSize` 变化时重新计算。
 *
 * @param items    完整的数据列表
 * @param pageSize 每页条数
 * @param resetKey 可选的重置依赖值，变化时回到第 1 页
 * @returns 分页状态与当前页数据
 */
export function useClientPagination<T>(
  items: T[],
  pageSize: number,
  resetKey?: unknown,
): ClientPagination<T> {
  const [page, setPage] = useState(1);
  // 总页数向上取整，Math.max 保证至少 1 页
  const pageCount = Math.max(1, Math.ceil(items.length / pageSize));

  // 列表缩短时，将当前页收敛到有效范围
  useEffect(() => {
    setPage((prev) => Math.min(prev, pageCount));
  }, [pageCount]);

  // resetKey 变化（如切换筛选）时重置回第 1 页
  useEffect(() => {
    setPage(1);
  }, [resetKey]);

  // 计算当前页的切片数据
  const pagedItems = useMemo(
    () => items.slice((page - 1) * pageSize, page * pageSize),
    [items, page, pageSize],
  );

  return { page, setPage, pageCount, pagedItems };
}

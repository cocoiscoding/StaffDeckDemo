/**
 * @file pages/TracesPage.tsx
 * @description 会话追踪记录（Trace）页面。
 *
 * 展示所有对话会话的 Trace 摘要列表，包括会话 ID、用户 ID、
 * 当前技能/步骤、工具调用次数、状态和更新时间。
 * 点击"查看"可在右侧侧滑面板中展示完整的 Trace JSON 详情。
 *
 * 数据通过 /api/enterprise/traces 接口获取，支持前端分页。
 */

import { ReloadOutlined } from '../icons';
import { useEffect, useState } from 'react';
import {
  Button as UIButton,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  notify,
} from '@/components/ui';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { Paginator } from '@/components/Paginator';
import { useClientPagination } from '../hooks/useClientPagination';
import { api, TENANT_ID } from '../api/client';
import type { TraceSummary } from '../types';

/** Trace 列表每页条数 */
const TRACES_PAGE_SIZE = 10;

/**
 * 会话追踪记录页面主组件。
 * 加载并展示 Trace 摘要列表，支持查看详情侧滑面板。
 * @returns 渲染好的 Trace 页面
 */
export default function TracesPage() {
  // Trace 摘要列表
  const [rows, setRows] = useState<TraceSummary[]>([]);
  // 当前查看的 Trace 详情（JSON），为 null 时关闭侧滑面板
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);

  /** 从后端加载全部 Trace 摘要 */
  const load = () =>
    api
      .get<TraceSummary[]>(`/api/enterprise/traces?tenant_id=${TENANT_ID}`)
      .then(setRows)
      .catch((error) => notify.error(error.message));

  // 页面挂载时首次加载
  useEffect(() => {
    void load();
  }, []);

  // 前端分页
  const pagination = useClientPagination(rows, TRACES_PAGE_SIZE);

  /**
   * 打开指定会话的 Trace 详情侧滑面板。
   * @param row 当前行对应的 Trace 摘要
   */
  async function openDetail(row: TraceSummary) {
    const result = await api.get<Record<string, unknown>>(`/api/enterprise/traces/${row.session_id}?tenant_id=${TENANT_ID}`);
    setDetail(result);
  }

  const truncateCell = 'block truncate';
  // 桌面端表格列定义
  const columns: DataTableColumn<TraceSummary>[] = [
    { key: 'session_id', title: '会话 ID', width: 230, render: (row) => <span className={truncateCell}>{row.session_id}</span> },
    { key: 'user_id', title: '用户 ID', width: 150, render: (row) => <span className={truncateCell}>{row.user_id || '-'}</span> },
    { key: 'active_skill_id', title: '当前技能', width: 190, render: (row) => <span className={truncateCell}>{row.active_skill_id || '-'}</span> },
    { key: 'active_step_id', title: '当前 Step', width: 190, render: (row) => <span className={truncateCell}>{row.active_step_id || '-'}</span> },
    { key: 'tool_call_count', title: '工具调用', width: 96, render: (row) => row.tool_call_count },
    { key: 'status', title: '状态', width: 96, render: (row) => row.status },
    { key: 'updated_at', title: '更新时间', width: 210, render: (row) => <span className={truncateCell}>{row.updated_at}</span> },
    {
      key: 'actions',
      title: '操作',
      width: 96,
      render: (row) => (
        <UIButton
          variant="outline"
          size="sm"
          className="h-[28px] rounded-[8px] px-[12px] text-[12px]"
          onClick={() => void openDetail(row)}
        >
          查看
        </UIButton>
      ),
    },
  ];

  return (
    <>
      {/* 页面标题栏：标题 + 刷新按钮 */}
      <div className="page-title">
        <h3>Trace</h3>
        <UIButton variant="outline" onClick={() => void load()}>
          <ReloadOutlined />
          刷新
        </UIButton>
      </div>
      <Card className="data-card">
        <CardHeader>
          <CardTitle>会话 Trace</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-[16px]">
          <div className="overflow-x-auto">
            <DataTable
              aria-label="会话 Trace"
              columns={columns}
              data={pagination.pagedItems}
              rowKey={(row) => row.session_id}
              className="min-w-[1308px]"
              emptyText="暂无 Trace"
            />
          </div>
          {rows.length > 0 && (
            <Paginator
              aria-label="Trace 分页"
              className="mt-0"
              page={pagination.page}
              pageCount={pagination.pageCount}
              onChange={pagination.setPage}
            />
          )}
        </CardContent>
      </Card>
      {/* Trace 详情侧滑面板：以格式化 JSON 展示完整追踪数据 */}
      <Sheet open={Boolean(detail)} onOpenChange={(next) => { if (!next) setDetail(null); }}>
        <SheetContent side="right" className="w-[720px] sm:max-w-[720px]">
          <SheetHeader>
            <SheetTitle>Trace Detail</SheetTitle>
          </SheetHeader>
          <div className="min-h-0 flex-1 overflow-auto px-[16px] pb-[16px]">
            <pre className="text-[12px] whitespace-pre-wrap">{JSON.stringify(detail, null, 2)}</pre>
          </div>
        </SheetContent>
      </Sheet>
    </>
  );
}

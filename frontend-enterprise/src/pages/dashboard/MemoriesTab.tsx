/**
 * @file 记忆管理标签页组件
 * @description
 * 仪表盘"记忆"标签页，展示和管理数字员工对各个用户的长期记忆。
 *
 * 核心功能：
 * - 按用户名、用户 ID 或关键词搜索记忆
 * - 按用户分组展示记忆（每个用户显示用户名、ID、记忆类型、记忆数、最近更新时间、摘要）
 * - 点击"查看"打开记忆详情弹窗，展示该用户的所有记忆条目
 * - 支持清空当前员工范围内当前用户的长期记忆（不影响其他用户）
 *
 * 记忆按员工和用户隔离沉淀，支持 profile（用户画像）和 summary（总结）等类型。
 */

import { useEffect, useMemo, useState } from 'react';

import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { DetailField } from '@/components/DetailField';
import { Paginator } from '@/components/Paginator';
import { Button as UIButton } from '@/components/ui/button';
import { Dialog, DialogContent, DialogTitle } from '@/components/ui';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';
import { MOBILE_CARD_CLASS, formatDateTime } from '@/lib/enterprise-ui';

import { api, TENANT_ID } from '../../api/client';
import IconListBulleted from '../../assets/icons/list-bulleted.svg?react';
import IconHistory from '../../assets/icons/profile-history.svg?react';
import IconRefresh from '../../assets/icons/refresh.svg?react';
import IconSearch from '../../assets/icons/search.svg?react';
import { useClientPagination } from '../../hooks/useClientPagination';
import type { MemoryRead } from '../../types';

/** localStorage 中存储当前选中员工范围的键名。 */
const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';
/** 记忆列表每页显示条数。 */
const MEMORY_PAGE_SIZE = 10;

/** 记忆搜索筛选条件。 */
type MemoryFilter = {
  username: string;
  user_id: string;
  q: string;
};

/** 按用户分组后的记忆数据结构。 */
type MemoryUserGroup = {
  /** 分组键（用户名或用户 ID）。 */
  key: string;
  /** 用户名（可能为空）。 */
  username?: string;
  /** 用户 ID。 */
  user_id: string;
  /** 该用户的所有记忆列表（按更新时间降序）。 */
  memories: MemoryRead[];
  /** 去重排序后的记忆类型列表。 */
  kinds: string[];
  /** 最近更新时间。 */
  latest_at: string;
  /** 记忆内容预览摘要。 */
  preview: string;
};

/** 空筛选条件常量。 */
const EMPTY_FILTER: MemoryFilter = { username: '', user_id: '', q: '' };

/**
 * 记忆管理标签页主组件。
 *
 * 数据加载流程：
 * 1. 从 localStorage 获取当前员工范围
 * 2. 根据筛选条件构建查询参数，请求记忆列表
 * 3. 通过 groupMemories 将扁平记忆列表按用户分组
 * 4. 客户端分页展示分组后的列表
 *
 * @returns 记忆管理标签页 JSX
 */
export default function MemoriesTab() {
  const [rows, setRows] = useState<MemoryRead[]>([]);
  const [detail, setDetail] = useState<MemoryUserGroup | null>(null);
  const [loading, setLoading] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [agentId, setAgentId] = useState(
    () => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '',
  );
  const [filter, setFilter] = useState<MemoryFilter>(EMPTY_FILTER);

  /**
   * 加载记忆列表数据。
   * @param next - 可选的筛选条件，默认使用当前 filter 状态
   */
  async function load(next: MemoryFilter = filter) {
    setLoading(true);
    try {
      const params = new URLSearchParams({ tenant_id: TENANT_ID });
      if (agentId) params.set('agent_id', agentId);
      if (next.username.trim()) params.set('username', next.username.trim());
      if (next.user_id.trim()) params.set('user_id', next.user_id.trim());
      if (next.q.trim()) params.set('q', next.q.trim());
      params.set('limit', '500');
      const result = await api.get<MemoryRead[]>(`/api/enterprise/memories?${params.toString()}`);
      setRows(result);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '查询失败');
    } finally {
      setLoading(false);
    }
  }

  // 监听跨组件的员工范围变更事件
  useEffect(() => {
    const onScopeChange = (event: Event) => {
      const nextAgentId =
        (event as CustomEvent<{ agentId?: string }>).detail?.agentId ||
        window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) ||
        '';
      setAgentId(nextAgentId);
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

  // 员工范围变化时重新加载
  useEffect(() => {
    void load(filter);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  // 将扁平记忆列表按用户分组
  const groups = useMemo(() => groupMemories(rows), [rows]);
  const pagination = useClientPagination(groups, MEMORY_PAGE_SIZE, groups);
  const emptyText = agentId
    ? '当前员工暂无用户记忆；新的对话记忆会按员工和用户隔离沉淀。'
    : '暂无记忆';

  /** 重置筛选条件并重新加载。 */
  function resetFilter() {
    setFilter(EMPTY_FILTER);
    void load(EMPTY_FILTER);
  }

  /**
   * 清空当前员工范围内当前用户的长期记忆。
   * 会弹出确认对话框，确认后调用删除接口。
   */
  async function clearOwnMemories() {
    const scopeText = agentId ? '当前员工下你的长期记忆' : '当前租户下你的长期记忆';
    if (!window.confirm(`将清空${scopeText}，不会影响其他用户。确定继续？`)) {
      return;
    }
    setClearing(true);
    try {
      const params = new URLSearchParams({ tenant_id: TENANT_ID });
      if (agentId) params.set('agent_id', agentId);
      const result = await api.delete<{ deleted: number }>(`/api/enterprise/memories/me?${params.toString()}`);
      notify.success(result.deleted > 0 ? `已清空 ${result.deleted} 条记忆` : '没有可清空的记忆');
      setDetail(null);
      await load(filter);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '清空失败');
    } finally {
      setClearing(false);
    }
  }

  // 桌面端表格列定义
  const columns: DataTableColumn<MemoryUserGroup>[] = [
    {
      key: 'username',
      title: '用户名',
      width: 160,
      className: 'text-[#18181a]',
      render: (row) => <span className="truncate">{row.username || '-'}</span>,
    },
    {
      key: 'user_id',
      title: '用户ID',
      width: 180,
      render: (row) => <span className="block truncate">{row.user_id}</span>,
    },
    {
      key: 'kinds',
      title: '类型',
      width: 120,
      render: (row) => (
        <div className="flex flex-wrap gap-[4px]">
          {row.kinds.map((kind) => (
            <MemoryKindBadge key={kind} kind={kind} />
          ))}
        </div>
      ),
    },
    {
      key: 'count',
      title: '记忆数',
      width: 100,
      render: (row) => `${row.memories.length} 次`,
    },
    {
      key: 'latest',
      title: '最近更新',
      width: 170,
      render: (row) => formatDateTime(row.latest_at),
    },
    {
      key: 'preview',
      title: '摘要',
      className: 'whitespace-normal',
      render: (row) => <span className="wrap-break-word">{row.preview || '-'}</span>,
    },
    {
      key: 'actions',
      title: '操作',
      width: 100,
      render: (row) => (
        <UIButton
          variant="link"
          onClick={() => setDetail(row)}
          className="h-auto p-0 text-[12px] font-normal text-[#1a71ff] hover:text-[#4a8dff] hover:no-underline"
        >
          查看
        </UIButton>
      ),
    },
  ];

  // 移动端卡片渲染
  /**
   * 渲染移动端记忆卡片。
   * @param row - 按用户分组后的记忆数据
   * @returns 移动端卡片 JSX
   */
  const renderMobileCard = (row: MemoryUserGroup) => (
    <article className={MOBILE_CARD_CLASS} key={row.key}>
      <div className="flex min-w-0 items-start justify-between gap-[10px]">
        <strong className="min-w-0 truncate text-[14px] font-semibold text-[#18181a]">
          {row.username || row.user_id}
        </strong>
        <UIButton
          variant="link"
          onClick={() => setDetail(row)}
          className="h-auto shrink-0 p-0 text-[12px] font-normal text-[#1a71ff] hover:text-[#4a8dff] hover:no-underline"
        >
          查看
        </UIButton>
      </div>
      <div className="mt-[8px] flex flex-wrap gap-[4px]">
        {row.kinds.map((kind) => (
          <MemoryKindBadge key={kind} kind={kind} />
        ))}
      </div>
      <p className="mt-[8px] line-clamp-2 text-[12px] leading-[1.55] text-[#858b9c]">{row.preview || '-'}</p>
      <div className="mt-[10px] flex items-center justify-between text-[12px] text-[#858b9c]">
        <span>{row.memories.length} 条记忆</span>
        <span>{formatDateTime(row.latest_at)}</span>
      </div>
    </article>
  );

  return (
    <>
      <section
        aria-busy={loading}
        className="relative mt-[-2px] flex w-full min-w-0 max-w-full flex-col gap-[24px] overflow-hidden rounded-[18px] bg-white p-[14px] shadow-[0_20px_42px_rgba(21,26,38,0.045)] min-[521px]:p-[18px]"
      >
        <div className="flex flex-col gap-[18px]">
          {/* 区域标题 */}
          <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
            <IconHistory className="size-[14px] shrink-0" />
            <span className="text-[14px] font-normal leading-none">记忆查询</span>
          </div>

          {/* 搜索表单 */}
          <form
            className="flex flex-wrap items-center gap-[16px]"
            onSubmit={(event) => {
              event.preventDefault();
              void load(filter);
            }}
          >
            <PrefixInput
              label="用户名"
              placeholder="如 user_demo"
              value={filter.username}
              onChange={(value) => setFilter((prev) => ({ ...prev, username: value }))}
            />
            <PrefixInput
              label="用户ID"
              placeholder="如 user_demo"
              value={filter.user_id}
              onChange={(value) => setFilter((prev) => ({ ...prev, user_id: value }))}
            />
            <PrefixInput
              label="搜索"
              placeholder="用户名、用户 ID、记忆内容"
              value={filter.q}
              onChange={(value) => setFilter((prev) => ({ ...prev, q: value }))}
            />
            <UIButton
              type="submit"
              disabled={loading}
              className="h-[34px] w-[80px] gap-[4px] rounded-[10px] bg-[#18181a] px-[20px] text-[12px] font-normal text-white hover:bg-[#303030]"
            >
              <IconSearch className="size-[14px]" />
              查询
            </UIButton>
            <UIButton
              type="button"
              variant="outline"
              onClick={resetFilter}
              disabled={loading}
              className="h-[34px] w-[80px] gap-[4px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[20px] text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]"
            >
              <IconRefresh className={cn('size-[14px]', loading && 'animate-spin')} />
              重置
            </UIButton>
            <UIButton
              type="button"
              variant="outline"
              onClick={clearOwnMemories}
              disabled={loading || clearing}
              className="h-[34px] w-[112px] rounded-[10px] border-[0.5px] border-[#f0d3d3] bg-white px-[16px] text-[12px] font-normal text-[#c43d3d] hover:border-[#e1a8a8] hover:bg-[#fff7f7] hover:text-[#a92d2d]"
            >
              {clearing ? '清空中' : '清空我的记忆'}
            </UIButton>
          </form>

          {/* 移动端卡片列表 */}
          <div className="grid gap-[10px] md:hidden">
            {groups.length ? (
              pagination.pagedItems.map(renderMobileCard)
            ) : (
              <div className="py-[40px] text-center text-[13px] text-[#858b9c]">{emptyText}</div>
            )}
          </div>

          {/* 桌面端数据表格 */}
          <div className="hidden md:block">
            <DataTable
              aria-label="员工记忆"
              columns={columns}
              data={pagination.pagedItems}
              rowKey={(row) => row.key}
              loading={loading}
              emptyText={emptyText}
            />
          </div>

          {/* 分页器 */}
          {groups.length > 0 && (
            <Paginator
              aria-label="员工记忆分页"
              className="mt-0 mb-[6px]"
              page={pagination.page}
              pageCount={pagination.pageCount}
              onChange={pagination.setPage}
            />
          )}
        </div>
      </section>

      {/* 记忆详情弹窗 */}
      <MemoryDetailDialog detail={detail} onClose={() => setDetail(null)} />
    </>
  );
}

/**
 * 带前缀标签的输入框组件。
 * 左侧固定标签，右侧为文本输入区域。
 *
 * @param label - 前缀标签文案
 * @param placeholder - 输入框占位文案
 * @param value - 当前值
 * @param onChange - 值变更回调
 * @returns 输入框 JSX
 */
function PrefixInput({
  label,
  placeholder,
  value,
  onChange,
}: {
  label: string;
  placeholder?: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex h-[34px] w-[260px] items-center overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white transition-colors focus-within:border-[#18181a] max-[900px]:w-full">
      <span className="flex h-full w-[58px] shrink-0 items-center justify-center border-r-[0.5px] border-[#e3e7f1] bg-[#f6f6f6] text-[12px] text-[#858b9c]">
        {label}
      </span>
      <input
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        className="h-full min-w-0 flex-1 bg-transparent px-[12px] text-[12px] text-[#17191f] outline-none placeholder:text-[#c0c6d4]"
      />
    </label>
  );
}

/**
 * 记忆类型徽章组件。
 * 根据 kind 值匹配对应色调，未匹配时使用灰色。
 *
 * @param kind - 记忆类型（如 profile、summary）
 * @returns 类型徽章 JSX
 */
function MemoryKindBadge({ kind }: { kind: string }) {
  const tone = MEMORY_KIND_TONE[kind] ?? 'gray';
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-[12px] py-[4px] text-[12px] leading-none capitalize whitespace-nowrap',
        MEMORY_KIND_TONE_CLASS[tone],
      )}
    >
      {kind}
    </span>
  );
}

/**
 * 记忆详情弹窗组件。
 * 展示某用户在当前员工下的所有记忆条目，包括类型、时间、重要性、会话 ID 和内容。
 *
 * @param detail - 用户记忆分组数据，为 null 时弹窗关闭
 * @param onClose - 关闭弹窗回调
 * @returns 记忆详情弹窗 JSX
 */
function MemoryDetailDialog({
  detail,
  onClose,
}: {
  detail: MemoryUserGroup | null;
  onClose: () => void;
}) {
  return (
    <Dialog open={Boolean(detail)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        className="flex max-h-[calc(100dvh-4rem)] w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[720px]"
      >
        <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <IconListBulleted className="size-[14px] shrink-0" />
          <DialogTitle className="text-[14px] font-normal leading-none text-[#757f9c]">
            员工记忆详情
          </DialogTitle>
        </div>

        {detail && (
          <div className="flex min-h-0 flex-1 flex-col gap-[16px] overflow-y-auto px-[12px]">
            {/* 用户基本信息 */}
            <div className="grid grid-cols-2 gap-[10px] max-[520px]:grid-cols-1">
              <DetailField label="用户名">{detail.username || '-'}</DetailField>
              <DetailField label="用户ID">{detail.user_id}</DetailField>
              <DetailField label="记忆数">{detail.memories.length} 条</DetailField>
              <DetailField label="类型">
                <div className="flex flex-wrap gap-[4px]">
                  {detail.kinds.map((kind) => (
                    <MemoryKindBadge key={kind} kind={kind} />
                  ))}
                </div>
              </DetailField>
            </div>

            {/* 记忆条目列表 */}
            <div className="flex flex-col gap-[12px]">
              {detail.memories.map((item) => (
                <article
                  key={item.id}
                  className="rounded-[12px] border border-[#eef0f4] bg-white p-[14px]"
                >
                  <div className="flex items-center justify-between gap-[10px]">
                    <MemoryKindBadge kind={item.kind} />
                    <span className="text-[12px] text-[#858b9c]">{formatDateTime(item.updated_at)}</span>
                  </div>
                  <div className="mt-[10px] flex flex-wrap gap-x-[16px] gap-y-[4px] text-[12px] text-[#858b9c]">
                    <span>importance: {item.importance}</span>
                    <span>session: {item.session_id || '-'}</span>
                  </div>
                  <p className="mt-[8px] text-[13px] leading-[1.6] text-[#18181a] wrap-break-word">
                    {item.content}
                  </p>
                  {/* 可展开的 metadata JSON */}
                  {Object.keys(item.metadata || {}).length > 0 && (
                    <details className="mt-[10px] text-[12px] text-[#858b9c]">
                      <summary className="cursor-pointer select-none">metadata</summary>
                      <pre className="mt-[6px] overflow-x-auto rounded-[8px] bg-[#f6f6f6] p-[10px] text-[11px] leading-normal text-[#464c5e]">
                        {JSON.stringify(item.metadata, null, 2)}
                      </pre>
                    </details>
                  )}
                </article>
              ))}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

/** 记忆类型的色调枚举。 */
type MemoryTone = 'blue' | 'green' | 'gray';

/** 记忆类型到色调的映射表。profile 为蓝色，summary 为绿色，其余为灰色。 */
const MEMORY_KIND_TONE: Record<string, MemoryTone> = {
  profile: 'blue',
  summary: 'green',
};

/** 色调到 Tailwind 类名的映射表。 */
const MEMORY_KIND_TONE_CLASS: Record<MemoryTone, string> = {
  blue: 'bg-[#e8f0ff] text-[#1a71ff]',
  green: 'bg-[#e9f7ef] text-[#2cb360]',
  gray: 'bg-[#f2f3f7] text-[#858b9c]',
};

/**
 * 将扁平的记忆列表按用户分组。
 * 分组键优先使用用户名，无用户名时使用用户 ID。
 * 每组内的记忆按更新时间降序排列，各组之间也按最近更新时间降序排列。
 *
 * @param rows - 原始记忆列表
 * @returns 按用户分组后的数组
 */
function groupMemories(rows: MemoryRead[]): MemoryUserGroup[] {
  // 按用户名或 ID 建立分组映射
  const map = new Map<string, MemoryRead[]>();
  rows.forEach((row) => {
    const key = row.username || row.user_id;
    const existing = map.get(key) || [];
    existing.push(row);
    map.set(key, existing);
  });
  // 将每组转换为 MemoryUserGroup 结构
  return Array.from(map.entries())
    .map(([key, memories]) => {
      // 组内按更新时间降序排序
      const sorted = [...memories].sort(
        (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
      );
      // 去重并排序记忆类型
      const kinds = Array.from(new Set(sorted.map((item) => item.kind))).sort();
      return {
        key,
        username: sorted[0]?.username,
        user_id: sorted[0]?.user_id || key,
        memories: sorted,
        kinds,
        latest_at: sorted[0]?.updated_at,
        // 将所有记忆内容拼接为预览摘要
        preview: sorted
          .map((item) => item.content.replace(/\s+/g, ' ').trim())
          .filter(Boolean)
          .join(' / '),
      };
    })
    // 各组按最近更新时间降序排序
    .sort((a, b) => new Date(b.latest_at).getTime() - new Date(a.latest_at).getTime());
}

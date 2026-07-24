/**
 * @file 对话日志标签页组件
 * @description
 * 仪表盘"对话日志"标签页，展示数字员工的对话记录与反馈分析。
 *
 * 核心功能：
 * - 按好评/差评/未评价/能力不足/工具问题/知识缺失/SOP 问题等维度筛选对话
 * - 展示对话反馈统计（对话数、反馈数、好评/差评数）
 * - 展示反馈归因汇总（各问题类型的数量分布）
 * - 点击"查看"打开对话详情弹窗，展示完整消息流、执行 trace 和反馈分析
 * - 支持对分析失败的反馈重新提交后台分析
 *
 * 数据来源：通过 Promise.allSettled 并行请求会话、好评/差评反馈、反馈汇总、员工列表，
 * 任一接口失败不会导致整个标签页空白。
 */

import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  Clock,
  FileSearch,
  GitBranch,
  RefreshCw,
  Workflow,
  Wrench,
} from 'lucide-react';

import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { DetailField } from '@/components/DetailField';
import { Paginator } from '@/components/Paginator';
import { StatCard } from '@/components/StatCard';
import { Button as UIButton } from '@/components/ui/button';
import { Dialog, DialogContent, DialogTitle, UnderlineTabs, type UnderlineTabItem } from '@/components/ui';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';
import { formatDateTime } from '@/lib/enterprise-ui';

import { api, TENANT_ID } from '../../api/client';
import IconCalendar from '../../assets/icons/profile-calendar.svg?react';
import { employeeDisplayNameWithCreator } from '../../employee';
import { useClientPagination } from '../../hooks/useClientPagination';
import { StatusBadge } from '../scheduled-tasks/StatusBadge';
import type { BadgeTone } from '../scheduled-tasks/shared';
import type {
  AgentProfileRead,
  EnterpriseChatSessionRead,
  EnterpriseSessionDetailRead,
  FeedbackAnalysisRead,
  FeedbackMessageRead,
  FeedbackSessionDetailRead,
  FeedbackSessionRead,
  FeedbackSummaryRead,
  TraceLineRead,
  TurnTraceRead,
} from '../../types';

/** localStorage 中存储当前选中员工范围的键名。 */
const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';
/** 对话日志每页显示条数。 */
const FEEDBACK_PAGE_SIZE = 10;

/** 对话筛选维度：全部 / 好评 / 差评 / 未评价 / 以及四种问题归因类型。 */
type LogFilter = 'all' | 'up' | 'down' | 'unrated' | 'ability' | 'tool' | 'knowledge' | 'sop';

/** 对话日志表格行数据：会话信息 + 关联的好评/差评反馈。 */
type ConversationLogRow = EnterpriseChatSessionRead & {
  downFeedback?: FeedbackSessionRead;
  upFeedback?: FeedbackSessionRead;
};

/** 对话详情数据：会话元信息、消息列表、反馈列表、事件列表和执行 trace。 */
type ConversationDetail = {
  session: Record<string, unknown>;
  messages: FeedbackMessageRead[];
  feedback: Array<Record<string, unknown>>;
  events: EnterpriseSessionDetailRead['events'];
  traces: TurnTraceRead[];
};

/** 筛选标签配置数组。 */
const FILTER_TABS: UnderlineTabItem<LogFilter>[] = [
  { label: '全部', value: 'all' },
  { label: '好评', value: 'up' },
  { label: '差评', value: 'down' },
  { label: '未评价', value: 'unrated' },
  { label: '能力不足', value: 'ability' },
  { label: '工具问题', value: 'tool' },
  { label: '知识缺失', value: 'knowledge' },
  { label: 'SOP 问题', value: 'sop' },
];

const MOBILE_CARD_CLASS =
  'min-w-0 rounded-[8px] border border-[#eceef1] bg-white p-[14px]';

/**
 * 对话日志标签页主组件。
 *
 * 数据加载流程：
 * 1. 从 URL 参数或 localStorage 获取当前员工范围
 * 2. 并行请求会话列表、好评/差评反馈、反馈汇总、员工列表（使用 allSettled 容错）
 * 3. 将反馈按 session_id 关联到会话行
 * 4. 根据筛选维度过滤行数据
 * 5. 客户端分页展示
 *
 * 点击"查看"时异步加载对话详情（会话详情 + 执行 trace + 反馈详情）。
 *
 * @returns 对话日志标签页 JSX
 */
export default function ConversationLogsTab() {
  const [searchParams] = useSearchParams();
  const [scopedAgentId, setScopedAgentId] = useState(
    () => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '',
  );
  // 优先使用 URL 参数中的 agent_id，其次使用 localStorage 中的范围
  const agentId = searchParams.get('agent_id') || scopedAgentId;
  const [sessions, setSessions] = useState<EnterpriseChatSessionRead[]>([]);
  const [downRows, setDownRows] = useState<FeedbackSessionRead[]>([]);
  const [upRows, setUpRows] = useState<FeedbackSessionRead[]>([]);
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [summary, setSummary] = useState<FeedbackSummaryRead | null>(null);
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [filter, setFilter] = useState<LogFilter>('all');
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [reanalyzingId, setReanalyzingId] = useState<string | null>(null);

  // 监听跨组件的员工范围变更事件
  useEffect(() => {
    const onScopeChange = (event: Event) => {
      setScopedAgentId(
        (event as CustomEvent<{ agentId?: string }>).detail?.agentId ||
          window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) ||
          '',
      );
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

  /**
   * 并行加载所有数据源。
   * 使用 allSettled 确保单个接口失败不会清空整个标签页。
   */
  const load = async () => {
    setLoading(true);
    const agentQuery = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
    const [sessionResult, downResult, upResult, summaryResult, agentResult] = await Promise.allSettled([
      api.get<EnterpriseChatSessionRead[]>(`/api/enterprise/sessions?tenant_id=${TENANT_ID}${agentQuery}`),
      api.get<FeedbackSessionRead[]>(`/api/enterprise/feedback/sessions?tenant_id=${TENANT_ID}&rating=down${agentQuery}`),
      api.get<FeedbackSessionRead[]>(`/api/enterprise/feedback/sessions?tenant_id=${TENANT_ID}&rating=up${agentQuery}`),
      api.get<FeedbackSummaryRead>(`/api/enterprise/feedback/summary?tenant_id=${TENANT_ID}${agentQuery}`),
      api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`),
    ]);
    if (sessionResult.status === 'fulfilled') setSessions(sessionResult.value);
    if (downResult.status === 'fulfilled') setDownRows(downResult.value);
    if (upResult.status === 'fulfilled') setUpRows(upResult.value);
    if (summaryResult.status === 'fulfilled') setSummary(summaryResult.value);
    if (agentResult.status === 'fulfilled') setAgents(agentResult.value);
    // 仅在有请求失败时提示错误
    const failure = [sessionResult, downResult, upResult, summaryResult, agentResult].find(
      (item): item is PromiseRejectedResult => item.status === 'rejected',
    );
    if (failure) {
      notify.error(failure.reason instanceof Error ? failure.reason.message : '部分对话日志数据加载失败');
    }
    setLoading(false);
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  // 将好评/差评反馈按 session_id 关联到会话行
  const rows = useMemo<ConversationLogRow[]>(() => {
    const downBySession = new Map(downRows.map((item) => [item.session_id, item]));
    const upBySession = new Map(upRows.map((item) => [item.session_id, item]));
    return sessions
      .filter((session) => !agentId || session.agent_id === agentId)
      .map((session) => ({
        ...session,
        downFeedback: downBySession.get(session.id),
        upFeedback: upBySession.get(session.id),
      }));
  }, [agentId, downRows, sessions, upRows]);

  // 员工 ID 到展示名称的映射
  const agentsById = useMemo(() => new Map(agents.map((agent) => [agent.id, agent])), [agents]);

  /**
   * 根据 agent ID 获取展示标签。
   * @param rowAgentId - 员工 ID
   * @returns 员工展示名称或原始 ID
   */
  const agentLabelFromId = (rowAgentId?: string | null): string => {
    if (!rowAgentId) return '-';
    const agent = agentsById.get(rowAgentId);
    return agent ? employeeDisplayNameWithCreator(agent) : rowAgentId;
  };

  const agentLabel = (row: ConversationLogRow): string => agentLabelFromId(row.agent_id);

  // 按筛选维度过滤行数据
  const filteredRows = useMemo(
    () =>
      rows.filter((row) => {
        if (filter === 'up') return Boolean(row.upFeedback);
        if (filter === 'down') return Boolean(row.downFeedback);
        if (filter === 'unrated') return !row.upFeedback && !row.downFeedback;
        // 以下四种按差评的归因桶过滤
        if (filter === 'ability') return row.downFeedback?.primary_bucket === 'model_issue';
        if (filter === 'tool') return row.downFeedback?.primary_bucket === 'tool_or_system_issue';
        if (filter === 'sop') return row.downFeedback?.primary_bucket === 'skill_issue';
        if (filter === 'knowledge') return row.downFeedback?.primary_bucket === 'unknown';
        return true;
      }),
    [filter, rows],
  );

  const pagination = useClientPagination(filteredRows, FEEDBACK_PAGE_SIZE, filter);

  /**
   * 打开某行对话的详情弹窗。
   * 并行请求会话详情和执行 trace，如有反馈则额外请求反馈详情。
   * @param row - 对话日志行数据
   */
  const openDetail = async (row: ConversationLogRow) => {
    setDetailLoading(true);
    try {
      const [sessionDetail, traces] = await Promise.all([
        api.get<EnterpriseSessionDetailRead>(`/api/enterprise/sessions/${row.id}?tenant_id=${TENANT_ID}`),
        api
          .get<TurnTraceRead[]>(`/api/chat/sessions/${row.id}/trace?tenant_id=${TENANT_ID}`)
          .catch(() => [] as TurnTraceRead[]),
      ]);
      // 仅当存在好评/差评时才请求反馈详情
      let feedbackDetail: FeedbackSessionDetailRead | null = null;
      if (row.downFeedback || row.upFeedback) {
        try {
          feedbackDetail = await api.get<FeedbackSessionDetailRead>(
            `/api/enterprise/feedback/sessions/${row.id}?tenant_id=${TENANT_ID}`,
          );
        } catch {
          feedbackDetail = null;
        }
      }
      setDetail({
        session: feedbackDetail?.session || sessionDetail.session,
        messages: feedbackDetail?.messages || sessionDetail.messages,
        feedback: feedbackDetail?.feedback || [],
        events: sessionDetail.events || [],
        traces,
      });
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载对话详情失败');
    } finally {
      setDetailLoading(false);
    }
  };

  /** 重新加载当前打开的详情弹窗数据。 */
  const reloadCurrentDetail = async () => {
    const sessionId = String(detail?.session?.id || detail?.session?.session_id || '');
    if (!sessionId) return;
    const row = rows.find((item) => item.id === sessionId);
    if (row) await openDetail(row);
  };

  /**
   * 对分析失败的反馈重新提交后台分析。
   * @param feedbackId - 反馈记录 ID
   */
  const reanalyzeFeedback = async (feedbackId: string) => {
    setReanalyzingId(feedbackId);
    try {
      await api.post(`/api/enterprise/feedback/${feedbackId}/reanalyze?tenant_id=${TENANT_ID}`);
      notify.success('已重新提交后台分析');
      await reloadCurrentDetail();
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '重新分析失败');
    } finally {
      setReanalyzingId(null);
    }
  };

  // 桌面端表格列定义
  const columns: DataTableColumn<ConversationLogRow>[] = [
    {
      key: 'title',
      title: '对话任务',
      width: 200,
      className: 'whitespace-normal text-[#18181a]',
      render: (row) => (
        <span className="line-clamp-1 wrap-break-word">
          {row.title || row.summary || row.last_agent_question || row.id}
        </span>
      ),
    },
    {
      key: 'agent',
      title: '数字员工',
      width: 180,
      render: (row) => <span className="block truncate" title={agentLabel(row)}>{agentLabel(row)}</span>,
    },
    {
      key: 'status',
      title: '状态',
      width: 120,
      render: (row) => (
        <div className="flex flex-wrap gap-[4px]">
          {row.downFeedback && <StatusBadge tone="red">差评</StatusBadge>}
          {row.upFeedback && <StatusBadge tone="green">好评</StatusBadge>}
          {!row.upFeedback && !row.downFeedback && <StatusBadge tone="blue">未评价</StatusBadge>}
        </div>
      ),
    },
    {
      key: 'attribution',
      title: '问题归因',
      width: 130,
      render: (row) => (
        <span>
          {row.downFeedback
            ? row.downFeedback.primary_bucket_label || row.downFeedback.primary_bucket || '待分析'
            : '暂无缺口'}
        </span>
      ),
    },
    {
      key: 'latest',
      title: '最近内容',
      className: 'whitespace-normal',
      render: (row) => (
        <span className="line-clamp-1 wrap-break-word">
          {row.downFeedback?.latest_message ||
            row.upFeedback?.latest_message ||
            row.summary ||
            row.last_agent_question ||
            '-'}
        </span>
      ),
    },
    {
      key: 'updated',
      title: '时间',
      width: 170,
      render: (row) => formatDateTime(row.updated_at),
    },
    {
      key: 'actions',
      title: '操作',
      width: 90,
      render: (row) => (
        <UIButton
          variant="link"
          disabled={detailLoading}
          onClick={() => void openDetail(row)}
          className="h-auto p-0 text-[12px] font-normal text-[#1a71ff] hover:text-[#4a8dff] hover:no-underline disabled:text-[#c0c6d4]"
        >
          查看
        </UIButton>
      ),
    },
  ];

  // 移动端卡片渲染
  const renderMobileCard = (row: ConversationLogRow) => (
    <article className={MOBILE_CARD_CLASS} key={row.id}>
      <div className="flex min-w-0 items-start justify-between gap-[10px]">
        <strong className="min-w-0 wrap-break-word text-[14px] font-semibold text-[#18181a]">
          {row.title || row.summary || row.last_agent_question || row.id}
        </strong>
        <div className="flex shrink-0 flex-wrap justify-end gap-[4px]">
          {row.downFeedback && <StatusBadge tone="red">差评</StatusBadge>}
          {row.upFeedback && <StatusBadge tone="green">好评</StatusBadge>}
          {!row.upFeedback && !row.downFeedback && <StatusBadge tone="blue">未评价</StatusBadge>}
        </div>
      </div>
      <p className="mt-[8px] line-clamp-2 text-[12px] leading-[1.55] text-[#858b9c]">
        {row.downFeedback?.latest_message ||
          row.upFeedback?.latest_message ||
          row.summary ||
          row.last_agent_question ||
          '-'}
      </p>
      <div className="mt-[10px] flex items-center justify-between gap-[10px] text-[12px] text-[#858b9c]">
        <span className="truncate" title={agentLabel(row)}>{agentLabel(row)}</span>
        <span className="shrink-0">{formatDateTime(row.updated_at)}</span>
      </div>
      <div className="mt-[10px] flex justify-end">
        <UIButton
          variant="link"
          disabled={detailLoading}
          onClick={() => void openDetail(row)}
          className="h-auto p-0 text-[12px] font-normal text-[#1a71ff] hover:text-[#4a8dff] hover:no-underline disabled:text-[#c0c6d4]"
        >
          查看
        </UIButton>
      </div>
    </article>
  );

  return (
    <>
      <section
        aria-busy={loading}
        className="relative mt-[-2px] flex w-full min-w-0 max-w-full flex-col gap-[24px] overflow-hidden rounded-[18px] bg-white p-[14px] shadow-[0_20px_42px_rgba(21,26,38,0.045)] min-[521px]:p-[18px]"
      >
        {/* 区域标题 */}
        <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <IconCalendar className="size-[14px] shrink-0" />
          <span className="text-[14px] font-normal leading-none">对话记录</span>
        </div>

        {/* 反馈统计卡片 */}
        <div className="flex flex-wrap items-stretch gap-[20px]" aria-label="对话反馈统计">
          <StatCard value={rows.length} label="对话" />
          <StatCard value={summary?.total_feedback ?? 0} label="反馈" />
          <StatCard value={summary?.up_count ?? 0} label="好评" tone="green" />
          <StatCard value={summary?.down_count ?? 0} label="差评" tone="red" />
        </div>

        {/* 反馈归因汇总 */}
        {summary && (summary.summary || summary.bucket_counts.length > 0) && (
          <div className="flex flex-col gap-[12px] rounded-[14px] border border-[#eef0f4] bg-[#fafbfc] px-[20px] py-[16px]">
            {summary.summary && (
              <p className="wrap-break-word text-[13px] leading-[1.7] text-[#464c5e]">
                {summary.summary}
              </p>
            )}
            {summary.bucket_counts.length > 0 && (
              <div className="flex flex-wrap gap-[6px]">
                {summary.bucket_counts.map((item) => (
                  <StatusBadge key={item.bucket} tone={bucketTone(item.bucket)}>
                    {item.label} {item.count}
                  </StatusBadge>
                ))}
              </div>
            )}
          </div>
        )}

        {/* 筛选标签栏 */}
        <div className="overflow-x-auto">
          <UnderlineTabs
            aria-label="对话日志筛选"
            variant="line"
            value={filter}
            onChange={setFilter}
            items={FILTER_TABS}
          />
        </div>

        {/* 移动端卡片列表 */}
        <div className="grid gap-[10px] md:hidden">
          {filteredRows.length ? (
            pagination.pagedItems.map(renderMobileCard)
          ) : (
            <div className="py-[40px] text-center text-[13px] text-[#858b9c]">暂无对话日志</div>
          )}
        </div>

        {/* 桌面端数据表格 */}
        <div className="hidden md:block">
          <DataTable
            aria-label="对话日志"
            columns={columns}
            data={pagination.pagedItems}
            rowKey={(row) => row.id}
            loading={loading}
            emptyText="暂无对话日志"
          />
        </div>

        {/* 分页器 */}
        {filteredRows.length > 0 && (
          <Paginator
            aria-label="对话日志分页"
            className="mt-0 mb-[6px]"
            page={pagination.page}
            pageCount={pagination.pageCount}
            onChange={pagination.setPage}
          />
        )}
      </section>

      {/* 对话详情弹窗 */}
      <FeedbackDetailDialog
        detail={detail}
        agentLabelFromId={agentLabelFromId}
        onClose={() => setDetail(null)}
        onReanalyze={reanalyzeFeedback}
        reanalyzingId={reanalyzingId}
      />
    </>
  );
}

/**
 * 对话详情弹窗组件。
 * 展示会话元信息、消息流（含执行 trace）、反馈统计和归因分析。
 *
 * @param detail - 对话详情数据，为 null 时弹窗关闭
 * @param agentLabelFromId - 员工 ID 到展示名称的转换函数
 * @param onClose - 关闭弹窗回调
 * @param onReanalyze - 重新分析回调
 * @param reanalyzingId - 正在重新分析的反馈 ID
 * @returns 详情弹窗 JSX
 */
function FeedbackDetailDialog({
  detail,
  agentLabelFromId,
  onClose,
  onReanalyze,
  reanalyzingId,
}: {
  detail: ConversationDetail | null;
  agentLabelFromId: (agentId?: string | null) => string;
  onClose: () => void;
  onReanalyze: (feedbackId: string) => void;
  reanalyzingId: string | null;
}) {
  return (
    <Dialog open={Boolean(detail)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        className="flex max-h-[calc(100dvh-4rem)] w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[900px]"
      >
        <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <Clock className="size-[14px] shrink-0" />
          <DialogTitle className="text-[14px] font-normal leading-none text-[#757f9c]">
            对话日志详情
          </DialogTitle>
        </div>

        {detail && (
          <div className="flex min-h-0 flex-1 flex-col gap-[16px] overflow-y-auto px-[12px]">
            {/* 会话元信息网格 */}
            <div className="grid grid-cols-2 gap-[10px] max-[520px]:grid-cols-1">
              <DetailField label="任务 ID">
                {String(detail.session.session_id || detail.session.id || '-')}
              </DetailField>
              <DetailField label="数字员工">{agentLabelFromId(String(detail.session.agent_id || ''))}</DetailField>
              <DetailField label="用户">{displayUser(detail.session)}</DetailField>
              <DetailField label="状态">{String(detail.session.status || '-')}</DetailField>
              <DetailField label="反馈" className="col-span-2 max-[520px]:col-span-1">
                <div className="flex flex-wrap gap-[6px]">
                  <StatusBadge tone="green">
                    好评 {detail.feedback.filter((item) => item.rating === 'up').length}
                  </StatusBadge>
                  <StatusBadge tone="red">
                    差评 {detail.feedback.filter((item) => item.rating === 'down').length}
                  </StatusBadge>
                  {/* 展示差评的归因标签 */}
                  {detail.feedback
                    .filter((item) => item.rating === 'down')
                    .map((item) => item.analysis as FeedbackAnalysisRead | undefined)
                    .filter(Boolean)
                    .map((analysis, index) => (
                      <StatusBadge
                        key={`${analysis?.bucket || 'unknown'}_${index}`}
                        tone={bucketTone(analysis?.bucket)}
                      >
                        {analysis?.bucket_label || analysis?.bucket || '待分析'}
                      </StatusBadge>
                    ))}
                </div>
              </DetailField>
            </div>

            {/* 消息流 */}
            <div className="feedback-conversation">
              {conversationItems(detail).map(({ message: item, trace }) => (
                <FeedbackMessage
                  key={item.id}
                  item={item}
                  trace={trace}
                  onReanalyze={onReanalyze}
                  reanalyzing={Boolean(item.feedback_id && item.feedback_id === reanalyzingId)}
                />
              ))}
              {/* 无消息但有 trace 时，仅展示 trace */}
              {detail.messages.length === 0 && detail.traces.length > 0
                ? detail.traces.map((trace) => (
                    <div key={trace.turn_id} className="feedback-message-row assistant">
                      <div className="feedback-message-bubble trace-only">
                        <FeedbackTraceBlock trace={trace} />
                      </div>
                    </div>
                  ))
                : null}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

/**
 * 单条反馈消息渲染组件。
 * 展示消息角色、时间、反馈评级、归因分析，以及关联的执行 trace。
 * 差评消息且分析失败时，展示"重新分析"按钮。
 *
 * @param item - 消息数据
 * @param trace - 关联的执行 trace（可选）
 * @param onReanalyze - 重新分析回调
 * @param reanalyzing - 当前消息是否正在重新分析中
 * @returns 消息气泡 JSX
 */
function FeedbackMessage({
  item,
  trace,
  onReanalyze,
  reanalyzing,
}: {
  item: FeedbackMessageRead;
  trace?: TurnTraceRead;
  onReanalyze: (feedbackId: string) => void;
  reanalyzing: boolean;
}) {
  const isUser = item.role === 'user';
  const isAssistant = item.role === 'assistant';
  const analysisFailed = item.feedback_analysis?.status === 'failed';
  return (
    <div className={`feedback-message-row ${isUser ? 'user' : 'assistant'}`}>
      <div className="feedback-message-bubble">
        {/* 消息元信息：角色、时间、评级徽章、归因徽章 */}
        <div className="feedback-message-meta">
          <span>{isUser ? '用户' : isAssistant ? '员工' : item.role}</span>
          <span>{formatDateTime(item.created_at)}</span>
          {item.feedback_rating === 'down' && <StatusBadge tone="red">差评</StatusBadge>}
          {item.feedback_rating === 'up' && <StatusBadge tone="green">好评</StatusBadge>}
          {item.feedback_analysis &&
            (analysisFailed ? (
              <StatusBadge tone="red">分析失败</StatusBadge>
            ) : (
              <StatusBadge tone={bucketTone(item.feedback_analysis.bucket)}>
                {item.feedback_analysis.bucket_label || item.feedback_analysis.bucket || '待分析'}
              </StatusBadge>
            ))}
        </div>
        {/* 执行 trace 块 */}
        {trace && <FeedbackTraceBlock trace={trace} />}
        <p className="feedback-message-content">{item.content}</p>
        {/* 差评分析详情盒 */}
        {item.feedback_analysis && item.feedback_rating === 'down' && (
          <div className="feedback-analysis-box">
            <div>
              <strong>状态：</strong>
              {analysisStatusLabel(item.feedback_analysis.status)}
              {item.feedback_analysis.status !== 'failed' &&
                typeof item.feedback_analysis.confidence === 'number' && (
                  <span> · 置信度 {(item.feedback_analysis.confidence * 100).toFixed(0)}%</span>
                )}
            </div>
            {item.feedback_analysis.summary && (
              <div>
                <strong>改进项：</strong>
                {item.feedback_analysis.summary}
              </div>
            )}
            {item.feedback_analysis.reason && (
              <div>
                <strong>原因：</strong>
                {item.feedback_analysis.reason}
              </div>
            )}
            {/* 分析失败时显示重新分析按钮 */}
            {item.feedback_analysis.status === 'failed' && item.feedback_id && (
              <UIButton
                variant="outline"
                disabled={reanalyzing}
                onClick={() => onReanalyze(item.feedback_id as string)}
                className="mt-[8px] h-[30px] gap-[4px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[14px] text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:text-[#18181a]"
              >
                <RefreshCw className={cn('size-3.5', reanalyzing && 'animate-spin')} />
                重新分析
              </UIButton>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * 将消息列表与执行 trace 关联。
 * trace 通过用户消息 ID 或 turn ID 关联到对应的 AI 回复。
 *
 * @param detail - 对话详情数据
 * @returns 消息与 trace 的关联数组
 */
function conversationItems(
  detail: ConversationDetail,
): Array<{ message: FeedbackMessageRead; trace?: TurnTraceRead }> {
  // 按 user_message_id 和 turn_id 建立 trace 索引
  const tracesByUserMessage = new Map<string, TurnTraceRead>();
  const tracesByTurn = new Map<string, TurnTraceRead>();
  detail.traces.forEach((trace) => {
    if (trace.user_message_id) tracesByUserMessage.set(trace.user_message_id, trace);
    tracesByTurn.set(trace.turn_id, trace);
  });

  // 追踪当前用户消息 ID，用于关联后续的 AI 回复
  let currentUserMessageId = '';
  return detail.messages.map((messageItem) => {
    if (messageItem.role === 'user') {
      currentUserMessageId = messageItem.id;
      return { message: messageItem };
    }
    // AI 回复通过当前用户消息 ID 或 turn ID 关联 trace
    const trace =
      messageItem.role === 'assistant'
        ? tracesByUserMessage.get(currentUserMessageId) || tracesByTurn.get(currentUserMessageId)
        : undefined;
    return { message: messageItem, trace };
  });
}

/**
 * 执行 trace 可折叠块组件。
 * 展示员工回复背后的执行流程：思考、技能调用、工具调用、知识检索等步骤。
 *
 * @param trace - 单轮 trace 数据
 * @returns trace 块 JSX（无有效行时返回 null）
 */
function FeedbackTraceBlock({ trace }: { trace: TurnTraceRead }) {
  const lines = traceDetails(trace.lines);
  if (lines.length === 0) return null;
  return (
    <div className="feedback-trace-block">
      <div className="feedback-trace-header">
        <Workflow className="size-[14px]" />
        <span>执行记录</span>
        <span>{trace.completed_at ? '已完成' : '执行中'}</span>
      </div>
      <div className="feedback-trace-lines">
        {lines.map((line) => (
          <div key={line.id} className={`feedback-trace-line ${line.kind} ${line.state}`}>
            <span className="feedback-trace-icon">{traceLineIcon(line.kind)}</span>
            <span className="feedback-trace-content">
              <span className="feedback-trace-text">{line.text}</span>
              {line.detail && <span className="feedback-trace-detail">{line.detail}</span>}
              {line.code && (
                <details className="feedback-trace-code">
                  <summary>查看代码</summary>
                  <pre>{line.code}</pre>
                </details>
              )}
              {line.output && (
                <details className="feedback-trace-code">
                  <summary>{line.outputTitle || '查看输出'}</summary>
                  <pre>{line.output}</pre>
                </details>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * 过滤 trace 行，隐藏无需展示的占位行。
 * 隐藏成功的"思考"行，以及无实质内容的占位文本行。
 *
 * @param lines - 原始 trace 行数组
 * @returns 过滤后的 trace 行数组
 */
function traceDetails(lines: TraceLineRead[]): TraceLineRead[] {
  const hiddenPlaceholders = new Set(['正在思考', '已完成思考', '正在执行', '执行记录']);
  return lines.filter((line) => {
    if (line.kind === 'thinking' && line.state !== 'failed') return false;
    if (hiddenPlaceholders.has(line.text) && !line.detail && !line.code && !line.output) return false;
    return true;
  });
}

/**
 * 根据 trace 行类型返回对应的图标。
 * @param kind - trace 行类型
 * @returns 图标元素
 */
function traceLineIcon(kind: TraceLineRead['kind']) {
  if (kind === 'skill') return <GitBranch className="size-[13px]" />;
  if (kind === 'tool') return <Wrench className="size-[13px]" />;
  if (kind === 'knowledge') return <FileSearch className="size-[13px]" />;
  return <Workflow className="size-[13px]" />;
}

/**
 * 从会话对象中提取用户展示名称。
 * @param session - 会话对象
 * @returns 用户展示名称
 */
function displayUser(session: Record<string, unknown>): string {
  return String(session.display_name || session.username || session.user_id || '-');
}

/**
 * 根据归因桶类型返回对应的徽章色调。
 * @param bucket - 归因桶标识
 * @returns 徽章色调
 */
function bucketTone(bucket?: string): BadgeTone {
  if (bucket === 'model_issue') return 'red';
  if (bucket === 'skill_issue') return 'orange';
  if (bucket === 'tool_or_system_issue') return 'blue';
  if (bucket === 'positive_or_resolved') return 'green';
  if (bucket === 'needs_model_analysis') return 'blue';
  return 'gray';
}

/**
 * 将分析状态枚举转换为中文标签。
 * @param status - 分析状态
 * @returns 中文状态标签
 */
function analysisStatusLabel(status?: string): string {
  if (status === 'pending') return '等待分析';
  if (status === 'analyzed') return '已完成';
  if (status === 'failed') return '分析失败';
  if (status === 'needs_model') return '未配置模型';
  return status || '未知';
}

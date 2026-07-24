/**
 * @file 仪表盘主页组件
 * @description
 * 企业端数字员工仪表盘的核心页面，根据当前选中的数字员工渲染不同的视图：
 *
 * 1. 未加载完成时：显示空白占位，避免员工档案出现前闪现空状态
 * 2. 无员工（非管理员）：显示"还没有数字员工"引导卡片
 * 3. 选中"全局概览"员工（is_overall）：显示开放广场统计看板（SOP/技能/知识库/工具数量、调用量、反馈等）
 * 4. 选中具体员工：显示员工 Hero 区（头像、名称、角色、在线状态、统计指标）+ 标签页切换
 *    （工作记录 / 定时任务 / 记忆 / 对话日志）
 *
 * 页面通过 localStorage 持久化当前选中的员工范围（agent scope），并通过
 * 自定义事件 'ultrarag-enterprise-agent-scope-change' 跨组件同步范围变更。
 */

import { useEffect, useState } from 'react';
import type { ComponentType, ReactNode, SVGProps } from 'react';
import { useNavigate } from 'react-router-dom';
import { Badge, Button as UiButton, Tabs, TabsList, TabsTrigger, notify } from '@/components/ui';
import { EnterpriseRoute } from '../../enums/routes';
import IconChat from '../../assets/icons/chat.svg?react';
import IconEdit from '../../assets/icons/edit.svg?react';
import IconAccount from '../../assets/icons/sys-accounts.svg?react';
import IconProfileFile from '../../assets/icons/profile-file.svg?react';
import IconProfileAlarm from '../../assets/icons/profile-alarm.svg?react';
import IconProfileHistory from '../../assets/icons/profile-history.svg?react';
import IconProfileCalendar from '../../assets/icons/profile-calendar.svg?react';
import { api, TENANT_ID } from '../../api/client';
import type { EnterpriseAuthUser } from '../../auth';
import AppHeader from '../../components/AppHeader';
import EmployeeAvatar from '../../components/EmployeeAvatar';
import EmployeeAvatarEditor from '../../components/EmployeeAvatarEditor';
import EmployeeProfileEditor from '../../components/EmployeeProfileEditor';
import StaffdeckIcon from '../../components/StaffdeckIcon';
import ScheduledTasksTab from './ScheduledTasksTab';
import MemoriesTab from './MemoriesTab';
import ConversationLogsTab from './ConversationLogsTab';
import WorkRecordTab from './WorkRecordTab';
import type { ReplyStats } from './WorkRecordTab';
import {
  agentResourceCount,
  canManageEmployeeAgent,
  canSelectCurrentEmployeeAgent,
  employeeCreatorName,
  employeeDisplayName,
  employeeProfile,
  preferredEmployeeAgent,
  staffdeckDisplayText,
} from '../../employee';
import type {
  AgentProfileRead,
  AgentWorkRecordEventRead,
  AgentWorkRecordRead,
  EnterpriseChatSessionRead,
  FeedbackSummaryRead,
  GeneralSkillRead,
  KnowledgeBaseRead,
  ModelConfigRead,
  ScheduledTaskRead,
  SkillRead,
  ToolRead,
} from '../../types';

/** localStorage 中存储当前选中员工范围的键名。 */
const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';

/**
 * 仪表盘主页组件。
 *
 * 数据加载流程：
 * 1. 首次挂载时并行请求所有资源（员工列表、技能、知识库、模型、工具、会话、反馈、定时任务）
 * 2. 根据权限过滤出当前用户可见的员工列表
 * 3. 若未选中员工或选中的已不可见，自动选择偏好员工
 * 4. 选中具体员工后，异步加载该员工的工作记录（回复统计 + 活动事件）
 *
 * @param currentUser - 当前登录用户信息
 * @param isAdmin - 是否为管理员（影响员工过滤逻辑和默认选择）
 * @param profileTab - 当前激活的标签页
 * @param onLogout - 登出回调
 * @returns 仪表盘页面 JSX
 */
export default function DashboardPage({
  currentUser,
  isAdmin = false,
  profileTab = 'work',
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  isAdmin?: boolean;
  profileTab?: ProfileTabKey;
  onLogout?: () => void;
}) {
  const navigate = useNavigate();
  // 各类资源的状态
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [skills, setSkills] = useState<SkillRead[]>([]);
  const [generalSkills, setGeneralSkills] = useState<GeneralSkillRead[]>([]);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseRead[]>([]);
  const [models, setModels] = useState<ModelConfigRead[]>([]);
  const [tools, setTools] = useState<ToolRead[]>([]);
  const [sessions, setSessions] = useState<EnterpriseChatSessionRead[]>([]);
  const [feedbackSummary, setFeedbackSummary] = useState<FeedbackSummaryRead | null>(null);
  const [scheduledTasks, setScheduledTasks] = useState<ScheduledTaskRead[]>([]);
  const [replyStats, setReplyStats] = useState<ReplyStats>({ total: 0, today: 0, byDay: {} });
  const [activityEvents, setActivityEvents] = useState<AgentWorkRecordEventRead[]>([]);
  // 当前选中的员工 ID（从 localStorage 初始化）
  const [agentId, setAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
  // 编辑器弹窗状态
  const [avatarEditorOpen, setAvatarEditorOpen] = useState(false);
  const [profileEditorOpen, setProfileEditorOpen] = useState(false);
  const [loaded, setLoaded] = useState(false);

  // 监听跨组件的员工范围变更事件（其他组件切换员工时会派发此事件）
  useEffect(() => {
    const onScopeChange = (event: Event) => {
      setAgentId((event as CustomEvent<{ agentId?: string }>).detail?.agentId || window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

  // 并行加载所有资源数据
  useEffect(() => {
    Promise.all([
      api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`),
      api.get<SkillRead[]>(`/api/enterprise/skills?tenant_id=${TENANT_ID}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ''}`),
      api.get<GeneralSkillRead[]>(`/api/enterprise/general-skills?tenant_id=${TENANT_ID}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ''}`),
      api.get<KnowledgeBaseRead[]>(`/api/enterprise/knowledge-bases?tenant_id=${TENANT_ID}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ''}`),
      api.get<ModelConfigRead[]>(`/api/enterprise/model-configs?tenant_id=${TENANT_ID}`),
      api.get<ToolRead[]>(`/api/enterprise/tools?tenant_id=${TENANT_ID}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ''}`),
      api.get<EnterpriseChatSessionRead[]>(`/api/enterprise/sessions?tenant_id=${TENANT_ID}`),
      api.get<FeedbackSummaryRead>(`/api/enterprise/feedback/summary?tenant_id=${TENANT_ID}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ''}`),
      api.get<ScheduledTaskRead[]>(`/api/enterprise/scheduled-tasks?tenant_id=${TENANT_ID}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ''}`),
    ])
      .then(([agentRows, skillRows, generalSkillRows, kbRows, modelRows, toolRows, sessionRows, feedbackRows, taskRows]) => {
        // 根据权限过滤出当前用户可选的员工
        const visibleAgents = agentRows.filter((item) => canSelectCurrentEmployeeAgent(item, currentUser, {
          activeOnly: true,
        }));
        setAgents(visibleAgents);
        setSkills(skillRows);
        setGeneralSkills(generalSkillRows);
        setKnowledgeBases(kbRows);
        setModels(modelRows);
        setTools(toolRows);
        setSessions(sessionRows);
        setFeedbackSummary(feedbackRows);
        // 过滤掉已归档的定时任务
        setScheduledTasks(taskRows.filter((item) => item.status !== 'archived'));
        // 若未选中员工或选中的已不可见，自动选择偏好员工
        if (!agentId || !visibleAgents.some((item) => item.id === agentId)) {
          const manageableAgents = visibleAgents.filter((item) => canManageEmployeeAgent(item, currentUser));
          const next = isAdmin
            ? preferredEmployeeAgent(visibleAgents)?.id || ''
            : preferredEmployeeAgent(manageableAgents)?.id
              || preferredEmployeeAgent(visibleAgents)?.id
              || '';
          if (next) {
            window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, next);
            window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: next } }));
            setAgentId(next);
          }
        }
      })
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载数字员工档案失败'))
      .finally(() => setLoaded(true));
  }, [agentId, currentUser, isAdmin]);

  // 确定当前选中的员工：优先精确匹配 ID，其次取第一个非全局员工
  const selectedAgent = agents.find((item) => item.id === agentId)
    || agents.find((item) => !item.is_overall)
    || null;
  // 全局员工显示所有会话，具体员工只显示该员工的会话
  const employeeSessions = selectedAgent?.is_overall
    ? sessions
    : sessions.filter((item) => item.agent_id === selectedAgent?.id);

  // 异步加载当前员工的工作记录（回复统计 + 活动事件）
  useEffect(() => {
    let cancelled = false;
    /**
     * 加载当前选中员工的工作记录数据。
     * 从后端获取回复统计（总数、今日数、按日分布）和活动事件列表。
     * 全局员工不加载工作记录。
     */
    async function loadWorkRecord() {
      // 全局员工不需要工作记录
      if (!selectedAgent || selectedAgent.is_overall) {
        setReplyStats({ total: 0, today: 0, byDay: {} });
        setActivityEvents([]);
        return;
      }
      try {
        const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai';
        const workRecord = await api.get<AgentWorkRecordRead>(
          `/api/enterprise/agents/${encodeURIComponent(selectedAgent.id)}/work-record?tenant_id=${TENANT_ID}&timezone=${encodeURIComponent(timezone)}`,
        );
        if (cancelled) return;
        setReplyStats({
          total: workRecord.reply_stats.total,
          today: workRecord.reply_stats.today,
          byDay: workRecord.reply_stats.by_day,
        });
        setActivityEvents(workRecord.events);
      } catch (error) {
        if (cancelled) return;
        setReplyStats({ total: 0, today: 0, byDay: {} });
        setActivityEvents([]);
        notify.error(error instanceof Error ? error.message : '加载员工工作记录失败');
      }
    }
    void loadWorkRecord();
    return () => {
      cancelled = true;
    };
  }, [selectedAgent?.id, selectedAgent?.is_overall]);

  // 计算各类统计指标
  const defaultModel = models.find((item) => item.is_default);
  const totalCalls = skills.reduce((sum, item) => sum + (item.total_call_count || item.call_count || 0), 0);
  const positiveFeedback = skills.reduce((sum, item) => sum + (item.total_positive_feedback_count || 0), 0);
  const negativeFeedback = skills.reduce((sum, item) => sum + (item.total_negative_feedback_count || 0), 0);
  // 过滤掉空的默认知识库
  const visibleKnowledgeBases = knowledgeBases.filter((item) => !isEmptyDefaultKnowledgeBase(item));

  // 避免在员工 API 返回前闪现开放广场/空状态
  if (!loaded && agents.length === 0) {
    return <div className="page dashboard-page" />;
  }

  // 无员工且非管理员：显示引导卡片
  if (!selectedAgent && !isAdmin) {
    return (
      <div className="page dashboard-page">
        <div className="empty-workspace-card p-[24px]">
          <h3 className="m-0 text-[20px] font-semibold text-foreground">还没有数字员工</h3>
          <p className="mt-[8px] text-[14px] text-muted-foreground">
            点击左下角「新建数字员工」开始创建，或前往员工广场选择已发布的员工。
          </p>
          <div className="mt-[16px] flex gap-[8px]">
            <UiButton onClick={() => navigate('/enterprise/agents')}>查看我的数字员工</UiButton>
            <UiButton variant="outline" onClick={() => navigate('/enterprise/feedback')}>查看对话日志</UiButton>
          </div>
        </div>
      </div>
    );
  }

  // 全局概览或未选中具体员工：显示开放广场统计看板
  if (!selectedAgent || selectedAgent.is_overall) {
    return (
      <div className="page dashboard-page">
        <div className="page-title">
          <h3>开放广场</h3>
        </div>
        <section className="employee-hero org-hero">
          <div>
            <span className="section-kicker">开放广场</span>
            <h2 className="ui-typography">开放广场</h2>
            <p className="ui-typography">
              汇集所有可共享的 SOP、知识库、技能和工具，新建数字员工时可以从这里复制配置作为起点。
            </p>
          </div>
          <div className="employee-hero-metrics">
            <MetricTile label="员工" value={agents.filter((item) => !item.is_overall).length} />
            <MetricTile label="对话" value={sessions.length} />
            <MetricTile label="反馈" value={feedbackSummary?.total_feedback || 0} />
          </div>
        </section>
        <div className="org-dashboard-grid">
          <DashboardStat title="SOP" value={skills.length} icon={<StaffdeckIcon name="filter" />} />
          <DashboardStat title="技能" value={generalSkills.length} icon={<StaffdeckIcon name="spark" />} />
          <DashboardStat title="知识库" value={visibleKnowledgeBases.length} icon={<StaffdeckIcon name="file" />} />
          <DashboardStat title="可用工具" value={tools.filter((item) => item.enabled).length} icon={<StaffdeckIcon name="tool" />} />
          <DashboardStat title="SOP 调用" value={totalCalls} icon={<StaffdeckIcon name="chat" />} />
          <DashboardStat title="好评" value={positiveFeedback || feedbackSummary?.up_count || 0} icon={<StaffdeckIcon name="chat" />} />
          <DashboardStat title="差评" value={negativeFeedback || feedbackSummary?.down_count || 0} icon={<StaffdeckIcon name="chat" />} />
          <div className="org-dashboard-card">
            <div className="ui-card-body p-[24px]">
              <span className="org-dashboard-icon"><StaffdeckIcon name="model" /></span>
              <span className="text-[13px] text-muted-foreground">默认模型</span>
              <span className="text-[15px] text-foreground">{defaultModel ? `${defaultModel.name} / ${defaultModel.model}` : '未配置'}</span>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // —— 以下为选中具体员工的详情视图 ——
  const employee = employeeProfile(selectedAgent);
  const employeeCreator = employeeCreatorName(selectedAgent);
  const canEditSelectedAgent = canManageEmployeeAgent(selectedAgent, currentUser);
  // 过滤出活跃状态的各类资源
  const activeSkills = skills.filter((item) => item.status === 'published' && item.branch_status !== 'inactive');
  const activeGeneralSkills = generalSkills.filter((item) => item.status === 'published');
  const activeKnowledge = visibleKnowledgeBases.filter((item) => item.status === 'active');
  const activeTools = tools.filter((item) => item.enabled);
  const selectedKnowledgeCount = visibleKnowledgeBases.length;
  const selectedGeneralSkillCount = agentResourceCount(selectedAgent, 'general_skill');
  const selectedSkillCount = agentResourceCount(selectedAgent, 'skill');
  const employeeScheduledTasks = scheduledTasks.filter((item) => item.agent_id === selectedAgent.id && item.status !== 'archived');
  const activeScheduledTasks = employeeScheduledTasks.filter((item) => item.status === 'active');
  // 计算好评率/差评率
  const totalFeedback = positiveFeedback + negativeFeedback;
  const positiveRate = totalFeedback ? Math.round((positiveFeedback / totalFeedback) * 100) : 0;
  const negativeRate = totalFeedback ? Math.round((negativeFeedback / totalFeedback) * 100) : 0;
  // 提取系统提示词摘要
  const systemPromptSummary = typeof selectedAgent.metadata?.system_prompt_summary === 'string'
    ? selectedAgent.metadata.system_prompt_summary
    : '';
  const systemSummary = compactSummary(
    staffdeckDisplayText(selectedAgent.persona_prompt || systemPromptSummary || selectedAgent.description || `${employee.roleName}，负责接收任务、调用知识库、执行 SOP 并沉淀对话质量反馈。`),
    132,
  );

  const heroActionButtonClass = 'inline-flex items-center justify-center gap-[4px] py-[8px] px-[12px] rounded-[14px] border-[0.5px] border-[#e3e7f1] bg-white text-[12px] font-normal text-[#858b9c] shadow-[0px_6px_6px_rgba(0,0,0,0.05)] hover:bg-[#f6f6f6] hover:text-[#858b9c]';
  const heroAvatar = (
    <EmployeeAvatar
      agent={selectedAgent}
      width={136}
      height={160}
      radius={0}
      fit="contain"
      objectPosition="center bottom"
      style={{ background: 'transparent', border: 'none', boxShadow: 'none', overflow: 'visible' }}
    />
  );

  return (
    <div className="min-h-full w-full min-w-0 max-w-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
        left={(
          <div className="flex flex-wrap items-center gap-x-9 gap-y-6 pt-1 pl-1">
            {/* 头像区域：有权限时可点击更换头像 */}
            <div className="flex shrink-0 flex-col items-center">
              {canEditSelectedAgent ? (
                <button
                  type="button"
                  onClick={() => setAvatarEditorOpen(true)}
                  aria-label="更换头像"
                  className="group relative block cursor-pointer border-0 bg-transparent p-0"
                >
                  {heroAvatar}
                  <span className="pointer-events-none absolute inset-x-0 bottom-0 flex items-center justify-center gap-1 bg-black/45 py-1 text-[11px] text-white opacity-0 transition-opacity group-hover:opacity-100">
                    <IconAccount className="size-3" />
                    更换头像
                  </span>
                </button>
              ) : (
                heroAvatar
              )}
              <div className="flex items-center gap-4">
                <UiButton
                  variant="outline"
                  className={heroActionButtonClass}
                  onClick={() => { window.location.href = '/workspace/chat'; }}
                >
                  <IconChat className="size-[14px]" />
                  去对话
                </UiButton>
                {canEditSelectedAgent && (
                  <UiButton
                    variant="outline"
                    className={heroActionButtonClass}
                    onClick={() => setProfileEditorOpen(true)}
                  >
                    <IconEdit className="size-[14px]" />
                    编辑资料
                  </UiButton>
                )}
              </div>
            </div>

            {/* 员工信息区：名称、角色、在线状态、创建者、工作风格、摘要、指标 */}
            <div className="flex min-w-[280px] flex-1 flex-col gap-2">
              <div className="flex items-end gap-2">
                <h2 className="m-0 text-[22px] leading-none font-semibold text-[#18181a]">
                  {employeeDisplayName(selectedAgent)}
                </h2>
                <span className="text-[13px] leading-none text-[#757f9c]">{employee.roleName || employeeDisplayName(selectedAgent)}</span>
              </div>

              <div className="flex flex-wrap items-center gap-4">
                {/* 在线状态指示器 */}
                <span className="inline-flex items-center gap-1.5 rounded-full bg-[#f6f6f6] px-2.5 py-0.5">
                  <span
                    className="size-1.5 rounded-full ring-[1.5px] ring-white"
                    style={{ background: selectedAgent.status === 'active' ? '#22c55e' : '#c4c9d4' }}
                  />
                  <span className="text-[12px] text-[#757f9c]">
                    {selectedAgent.status === 'active' ? '在线' : '下线'}
                  </span>
                </span>
                <span className="text-[12px] text-[#757f9c]">创建者：{employeeCreator}</span>
                <span className="text-[12px] text-[#757f9c]">入职时间：{employee.onboardedAt}</span>
                <div className="flex flex-wrap items-center gap-3">
                  {employee.workStyles.slice(0, 3).map((item) => (
                    <Badge
                      key={item}
                      variant="outline"
                      className="h-auto rounded-[10px] border-[0.5px] border-[#e3e7f1] px-4 py-1 text-[12px] font-normal text-[#757f9c]"
                    >
                      {item}
                    </Badge>
                  ))}
                </div>
              </div>

              <p className="m-0 line-clamp-2 max-w-[720px] text-[14px] leading-[22px] text-[#757f9c]">
                {systemSummary}
              </p>

              {/* 关键指标行：资料 / 技能 / SOP / 定时任务 */}
              <div className="flex w-full max-w-[514px] gap-3">
                <HeroMetric value={selectedKnowledgeCount} label="资料" />
                <HeroMetric value={selectedGeneralSkillCount} label="技能" />
                <HeroMetric value={selectedSkillCount} label="SOP" />
                <HeroMetric value={activeScheduledTasks.length} label="定时任务" />
              </div>
            </div>
          </div>
        )}
      />
      {/* 标签页导航 */}
      <EmployeeProfileTabs activeKey={profileTab} />
      {/* 根据当前标签页渲染对应内容 */}
      {profileTab === 'work' && (
        <WorkRecordTab
          selectedAgent={selectedAgent}
          activeKnowledge={activeKnowledge}
          activeGeneralSkills={activeGeneralSkills}
          activeSkills={activeSkills}
          activeTools={activeTools}
          activeScheduledTasks={activeScheduledTasks}
          employeeSessions={employeeSessions}
          replyStats={replyStats}
          activityEvents={activityEvents}
          positiveRate={positiveRate}
          negativeRate={negativeRate}
        />
      )}
      {profileTab === 'scheduled' && <ScheduledTasksTab />}
      {profileTab === 'memories' && <MemoriesTab />}
      {profileTab === 'logs' && <ConversationLogsTab />}
      {/* 头像编辑器 */}
      <EmployeeAvatarEditor
        agent={selectedAgent}
        open={avatarEditorOpen}
        onClose={() => setAvatarEditorOpen(false)}
        onSaved={(saved) => setAgents((current) => current.map((item) => (item.id === saved.id ? saved : item)))}
      />
      {/* 资料编辑器 */}
      <EmployeeProfileEditor
        agent={selectedAgent}
        open={profileEditorOpen}
        currentUser={currentUser}
        onClose={() => setProfileEditorOpen(false)}
        onSaved={(saved) => setAgents((current) => current.map((item) => (item.id === saved.id ? saved : item)))}
      />
    </div>
  );
}

/**
 * 开放广场看板中的统计卡片。
 *
 * @param title - 统计项标题
 * @param value - 统计数值
 * @param icon - 图标元素
 * @returns 统计卡片 JSX
 */
function DashboardStat({ title, value, icon }: { title: string; value: number; icon: ReactNode }) {
  return (
    <div className="org-dashboard-card">
      <div className="ui-card-body p-[24px]">
        <span className="org-dashboard-icon">{icon}</span>
        <span className="text-[13px] text-muted-foreground">{title}</span>
        <strong>{value}</strong>
      </div>
    </div>
  );
}

/**
 * 判断知识库是否为可忽略的空默认知识库。
 * 满足以下任一条件时返回 true：
 * 1. 通过文档上传创建但无源文档且无运行时知识内容
 * 2. 名称为"默认知识库"且文档数/桶数/片段数均为 0
 *
 * @param item - 知识库数据
 * @returns 是否为空默认知识库
 */
function isEmptyDefaultKnowledgeBase(item: KnowledgeBaseRead): boolean {
  const hasRuntimeKnowledge = item.document_count > 0 || item.bucket_count > 0 || item.chunk_count > 0;
  if (!hasRuntimeKnowledge && item.metadata?.created_from_document_upload && !item.metadata?.source_document_id) {
    return true;
  }
  return (
    item.name === '默认知识库'
    && item.document_count === 0
    && item.bucket_count === 0
    && item.chunk_count === 0
  );
}

/**
 * 开放广场概览区的指标磁贴。
 *
 * @param label - 指标标签
 * @param value - 指标数值
 * @returns 指标磁贴 JSX
 */
function MetricTile({ label, value }: { label: string; value: number }) {
  return (
    <div className="employee-metric-tile">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

/** 个人档案标签页的键类型。 */
type ProfileTabKey = 'work' | 'scheduled' | 'memories' | 'logs';

/** 标签页配置：键名、显示文案、图标组件、对应路由。 */
const PROFILE_TABS: {
  key: ProfileTabKey;
  label: string;
  Icon: ComponentType<SVGProps<SVGSVGElement>>;
  route: EnterpriseRoute;
}[] = [
  { key: 'work', label: '工作记录', Icon: IconProfileFile, route: EnterpriseRoute.Dashboard },
  { key: 'scheduled', label: '定时任务', Icon: IconProfileAlarm, route: EnterpriseRoute.ScheduledTasks },
  { key: 'memories', label: '记忆', Icon: IconProfileHistory, route: EnterpriseRoute.Memories },
  { key: 'logs', label: '对话日志', Icon: IconProfileCalendar, route: EnterpriseRoute.Feedback },
];

/**
 * 个人档案标签页导航栏。
 * 点击标签时通过路由跳转切换页面。
 *
 * @param activeKey - 当前激活的标签页键
 * @returns 标签页导航 JSX
 */
function EmployeeProfileTabs({ activeKey = 'work' }: { activeKey?: ProfileTabKey }) {
  const navigate = useNavigate();
  return (
    <Tabs
      value={activeKey}
      onValueChange={(value) => {
        const tab = PROFILE_TABS.find((item) => item.key === value);
        if (tab && value !== activeKey) navigate(tab.route);
      }}
      className="flex w-full flex-col items-center"
    >
      <TabsList
        aria-label="个人档案分区"
        className="h-[35px]! w-[504px] max-w-full gap-2 rounded-none bg-transparent p-0"
      >
        {PROFILE_TABS.map(({ key, label, Icon }) => (
          <TabsTrigger
            key={key}
            value={key}
            className="h-[35px] flex-1 gap-[7px] rounded-t-lg rounded-b-none border-0 text-[14px] font-bold text-[#8b94aa] hover:text-[#202226] data-[state=active]:bg-white data-[state=active]:text-[#202226] data-[state=active]:shadow-[0_-12px_28px_rgba(21,26,38,0.04)] in-data-[theme=dark]:text-[#8f98aa] in-data-[theme=dark]:hover:text-[#f0f2f6] in-data-[theme=dark]:data-[state=active]:bg-[#202126] in-data-[theme=dark]:data-[state=active]:text-[#c5ccd8] in-data-[theme=dark]:data-[state=active]:shadow-none"
          >
            <Icon />
            {label}
          </TabsTrigger>
        ))}
      </TabsList>
    </Tabs>
  );
}

/**
 * Hero 区的指标小磁贴。
 *
 * @param label - 指标标签
 * @param value - 指标数值
 * @returns 指标磁贴 JSX
 */
function HeroMetric({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex flex-1 items-end gap-1 rounded-[10px] bg-[#f6f6f6] px-5 py-2">
      <strong className="text-[14px] leading-none font-medium text-[#18181a]">{value}</strong>
      <span className="text-[12px] leading-none text-[#464c5e]">{label}</span>
    </div>
  );
}

/**
 * 将文本压缩为指定最大长度的摘要。
 * 先将连续空白合并为单个空格并去除首尾空白，超长时截断并添加省略号。
 *
 * @param value - 原始文本
 * @param maxLength - 最大字符数
 * @returns 压缩后的摘要文本
 */
function compactSummary(value: string, maxLength: number): string {
  const compact = value.replace(/\s+/g, ' ').trim();
  return compact.length > maxLength ? `${compact.slice(0, maxLength)}...` : compact;
}

/**
 * @file 工作记录标签页组件
 * @description
 * 仪表盘"工作记录"标签页，展示数字员工的工作概览与活动轨迹。
 *
 * 核心功能模块：
 * 1. 顶部指标栏：今日对话、累计对话、好评率、差评率（可点击跳转对话日志）
 * 2. 活动时间线：支持日/周/月三种视图，展示对话、定时任务、SOP、工具、知识、技能
 *    六类活动的时间分布，带有 HoverCard 悬浮详情和日期选择器
 * 3. 成长记录：横向时间线展示新增的 SOP、技能、工具等里程碑事件
 * 4. 能力卡片网格：知识库、技能、SOP、工具、定时任务、对话日志六个能力入口卡片
 */

import { Fragment, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';

import { HoverCard, HoverCardContent, HoverCardTrigger } from '../../components/ui/hover-card';
import { Popover, PopoverContent, PopoverTrigger } from '../../components/ui/popover';

import IconGrowthArrow from '../../assets/icons/growth-arrow.svg?react';
import IconCardArrow from '../../assets/icons/card-arrow.svg?react';
import IconCapFolder from '../../assets/icons/cap-folder.svg?react';
import IconCapMagicWand from '../../assets/icons/cap-magicwand.svg?react';
import IconCapClipboard from '../../assets/icons/cap-clipboard.svg?react';
import IconCapBriefcase from '../../assets/icons/cap-briefcase.svg?react';
import IconProfileAlarm from '../../assets/icons/profile-alarm.svg?react';
import IconProfileCalendar from '../../assets/icons/profile-calendar.svg?react';
import capabilityLogs from '../../assets/staffdeck/capabilityLogs.png';
import capabilityTasks from '../../assets/staffdeck/capabilityTasks.png';
import capabilityTools from '../../assets/staffdeck/capabilityTools.png';
import StaffdeckIcon from '../../components/StaffdeckIcon';
import { staffdeckDisplayText } from '../../employee';
import type {
  AgentProfileRead,
  AgentWorkRecordEventRead,
  EnterpriseChatSessionRead,
  GeneralSkillRead,
  KnowledgeBaseRead,
  ScheduledTaskRead,
  SkillRead,
  ToolRead,
} from '../../types';

/** 回复统计数据结构。 */
export type ReplyStats = {
  /** 累计对话总数。 */
  total: number;
  /** 今日对话数。 */
  today: number;
  /** 按天统计的对话数映射。 */
  byDay: Record<string, number>;
};

/** 时间线模式：日 / 周 / 月。 */
const TIMELINE_MODES = [
  { key: 'day', label: 'Day' },
  { key: 'week', label: 'Week' },
  { key: 'month', label: 'Month' },
] as const;
type TimelineMode = (typeof TIMELINE_MODES)[number]['key'];
/** 星期缩写数组（周日到周六）。 */
const TIMELINE_WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

/** 成长记录事件数据结构。 */
type GrowthEvent = {
  id: string;
  kind: string;
  title: string;
  description: string;
  timestamp: string;
  icon: ReactNode;
  tone: string;
};

/** 成长记录时间戳来源类型（支持多种时间字段）。 */
type GrowthTimestampSource = {
  created_at?: string;
  updated_at?: string;
  metadata?: Record<string, unknown>;
};

/** 工作记录标签页的属性类型。 */
export type WorkRecordTabProps = {
  /** 当前选中的数字员工。 */
  selectedAgent: AgentProfileRead;
  /** 活跃的知识库列表。 */
  activeKnowledge: KnowledgeBaseRead[];
  /** 活跃的技能列表。 */
  activeGeneralSkills: GeneralSkillRead[];
  /** 活跃的 SOP 列表。 */
  activeSkills: SkillRead[];
  /** 活跃的工具列表。 */
  activeTools: ToolRead[];
  /** 活跃的定时任务列表。 */
  activeScheduledTasks: ScheduledTaskRead[];
  /** 员工对话会话列表。 */
  employeeSessions: EnterpriseChatSessionRead[];
  /** 回复统计数据。 */
  replyStats: ReplyStats;
  /** 活动事件列表。 */
  activityEvents: AgentWorkRecordEventRead[];
  /** 好评率（百分比）。 */
  positiveRate: number;
  /** 差评率（百分比）。 */
  negativeRate: number;
};

// 能力卡片样式类名常量
const capabilityCardClass = 'group relative flex h-[230px] w-full min-w-0 appearance-none flex-col items-stretch gap-[6px] overflow-hidden rounded-[20px] border px-[24px] py-[20px] text-left transition-[transform,box-shadow] duration-[180ms] ease-[ease] hover:-translate-y-[2px]';
const capabilityLightCardClass = 'border-[#f6f6f6] bg-white shadow-[0_4px_10px_rgba(0,0,0,0.05)] hover:shadow-[0_12px_26px_rgba(0,0,0,0.08)]';
const capabilityDarkCardClass = 'border-[#29282d] bg-[#29282d] text-white shadow-none hover:shadow-[0_12px_26px_rgba(0,0,0,0.28)]';
const capabilityArrowClass = 'pointer-events-none absolute top-[13px] right-[8px] size-[20px] text-[#858b9c] group-data-[tone=dark]:text-[#c7ccd6]';
const capabilityGlyphClass = 'size-[14px] shrink-0 text-[#858b9c] group-data-[tone=dark]:text-white';
const capabilityNameClass = 'min-w-0 truncate text-[14px] font-normal text-[#858b9c] group-data-[tone=dark]:text-white';
const capabilityBarClass = 'block h-[4px] w-full overflow-hidden rounded-[90px] bg-[#e9e9e9] group-data-[tone=dark]:bg-[#6a6a6a]';
const capabilityBarFillClass = 'block h-full w-[20px] rounded-[90px] bg-[#282931] group-data-[tone=dark]:bg-[#e9e9e9]';
const capabilityDescClass = 'line-clamp-5 min-w-0 overflow-hidden text-[10px] leading-[16px] font-normal text-[#757f9c] [overflow-wrap:anywhere] group-data-[tone=dark]:line-clamp-2 group-data-[tone=dark]:text-[#f6f6f6]';

/**
 * 工作记录标签页主组件。
 *
 * @param props - 组件属性，参见 {@link WorkRecordTabProps}
 * @returns 工作记录标签页 JSX
 */
export default function WorkRecordTab({
  selectedAgent,
  activeKnowledge,
  activeGeneralSkills,
  activeSkills,
  activeTools,
  activeScheduledTasks,
  employeeSessions,
  replyStats,
  activityEvents,
  positiveRate,
  negativeRate,
}: WorkRecordTabProps) {
  const navigate = useNavigate();
  const goToLogs = () => navigate(`/enterprise/feedback?agent_id=${encodeURIComponent(selectedAgent.id)}`);

  // 能力卡片配置数组
  const capabilityCards = [
    {
      route: '/enterprise/knowledge',
      title: '知识库',
      tone: 'knowledge',
      count: activeKnowledge.length,
      body: activeKnowledge.slice(0, 3).map((item) => staffdeckDisplayText(item.name)).join(' / ') || '暂无知识库',
      icon: <IconCapFolder className={capabilityGlyphClass} />,
      dark: false,
    },
    {
      route: '/enterprise/general-skills',
      title: '技能',
      tone: 'skill',
      count: activeGeneralSkills.length,
      body: activeGeneralSkills.slice(0, 3).map((item) => staffdeckDisplayText(item.name)).join(' / ') || '暂无启用技能',
      icon: <IconCapMagicWand className={capabilityGlyphClass} />,
      dark: false,
    },
    {
      route: '/enterprise/skills',
      title: 'SOP',
      tone: 'sop',
      count: activeSkills.length,
      body: activeSkills.slice(0, 3).map((item) => staffdeckDisplayText(item.name)).join(' / ') || '暂无启用 SOP',
      icon: <IconCapClipboard className={capabilityGlyphClass} />,
      dark: false,
    },
    {
      route: '/enterprise/tools',
      title: '工具',
      tone: 'tools',
      count: activeTools.length,
      body: activeTools.slice(0, 3).map((item) => staffdeckDisplayText(item.display_name || item.name)).join(' / ') || '暂无启用工具',
      icon: <IconCapBriefcase className={capabilityGlyphClass} />,
      dark: true,
      illustration: capabilityTools,
    },
    {
      route: '/enterprise/scheduled-tasks',
      title: '定时任务',
      tone: 'tasks',
      count: activeScheduledTasks.length,
      body: activeScheduledTasks.slice(0, 2).map((item) => staffdeckDisplayText(item.title)).join(' / ') || '暂无启用定时任务',
      icon: <IconProfileAlarm className={capabilityGlyphClass} />,
      dark: true,
      illustration: capabilityTasks,
    },
    {
      route: `/enterprise/feedback?agent_id=${encodeURIComponent(selectedAgent.id)}`,
      title: '对话日志',
      tone: 'logs',
      count: replyStats.total,
      body: staffdeckDisplayText(employeeSessions[0]?.summary || employeeSessions[0]?.last_agent_question || '暂无对话任务'),
      icon: <IconProfileCalendar className={capabilityGlyphClass} />,
      dark: true,
      illustration: capabilityLogs,
    },
  ];

  // 计算成长记录时间线
  const growthItems = growthTimeline(activeSkills, activeGeneralSkills, activeTools);

  return (
    <section className="relative flex w-full min-w-0 max-w-full mt-[-2px] flex-col gap-[24px] overflow-hidden rounded-[18px] shadow-[0_20px_42px_rgba(21,26,38,0.045)] bg-white p-[14px] *:min-w-0 min-[521px]:p-[18px] in-data-[theme=dark]:border-[#343741] in-data-[theme=dark]:bg-[#202126] in-data-[theme=dark]:text-[#f0f2f6]">
      {/* —— 顶部指标栏（可点击跳转对话日志） —— */}
      <div className="flex w-full items-stretch gap-[16px]">
        <ClickableMetric label="今日对话" value={replyStats.today} onClick={goToLogs} />
        <ClickableMetric label="累计对话" value={replyStats.total} onClick={goToLogs} />
        <ClickableMetric label="好评率" value={positiveRate} suffix="%" tone="positive" onClick={goToLogs} />
        <ClickableMetric label="差评率" value={negativeRate} suffix="%" tone="negative" onClick={goToLogs} />
      </div>
      {/* 活动时间线 */}
      <ActivityTimeline events={activityEvents} />
      {/* —— 成长记录时间线 —— */}
      <div className="flex w-full min-w-0 max-w-full flex-col gap-[10px] mt-[20px]">
        <div className="inline-flex items-center gap-[6px] self-start text-[14px] capitalize leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
          <IconGrowthArrow className="size-[14px] shrink-0" />
          成长记录
        </div>
        {growthItems.length ? (
          <div className="relative w-full min-w-0 max-w-full overflow-x-auto">
            <div className="grid grid-flow-col auto-cols-[minmax(160px,1fr)] gap-[20px] pb-[20px]">
              {growthItems.map((item) => (
                <div className="relative flex flex-col items-center gap-[8px]" key={item.id}>
                  {/* 时间线横线 */}
                  <span className="pointer-events-none absolute left-[-10px] right-[-10px] top-[28px] z-0 h-px bg-[#e3e7f1] in-data-[theme=dark]:bg-[#363a45]" />
                  <p className="m-0 text-center text-[12px] font-medium leading-[16px] text-[#18181a] in-data-[theme=dark]:text-[#f0f2f6]">
                    {formatMonthDay(item.timestamp)}
                  </p>
                  {/* 时间线节点 */}
                  <span className="relative z-10 size-[8px] shrink-0 rounded-full bg-[#18181a] in-data-[theme=dark]:bg-[#f0f2f6]" />
                  {/* 节点信息卡片 */}
                  <div className="relative flex w-[136px] flex-col gap-[4px] rounded-[14px] bg-[#f6f6f6] px-[16px] py-[10px] in-data-[theme=dark]:bg-[#2b2d33]">
                    <span className="absolute top-[-8px] left-1/2 size-0 -translate-x-1/2 border-x-6 border-b-8 border-x-transparent border-b-[#f6f6f6] in-data-[theme=dark]:border-b-[#2b2d33]" />
                    <span className="truncate text-[10px] leading-none text-[#757f9c]">{item.kind}</span>
                    <span className="truncate text-[12px] leading-none text-[#464c5e] in-data-[theme=dark]:text-[#c9cede]">
                      {staffdeckDisplayText(item.title)}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div className="employee-memory-empty">暂无成长轨迹</div>
        )}
      </div>
      {/* —— 能力卡片网格 —— */}
      <div className="w-full min-w-0 max-w-full overflow-x-auto">
        <div className="grid grid-flow-col auto-cols-[minmax(160px,1fr)] gap-[clamp(18px,2.22vw,32px)]">
        {capabilityCards.map((item) => (
          <button
            type="button"
            key={item.title}
            className={`${capabilityCardClass} ${item.dark ? capabilityDarkCardClass : capabilityLightCardClass}`}
            data-tone={item.dark ? 'dark' : 'light'}
            onClick={() => navigate(item.route)}
          >
            <IconCardArrow className={capabilityArrowClass} />
            <span className="flex flex-col gap-[12px]">
              <span className="flex min-w-0 items-center gap-[6px] pr-[24px]">
                {item.icon}
                <span className={capabilityNameClass}>{item.title}</span>
              </span>
              <span className="flex flex-col gap-[6px]">
                <strong className="text-[24px] leading-none font-semibold text-[#18181a] group-data-[tone=dark]:text-white">{item.count}</strong>
                <span className={capabilityBarClass}><span className={capabilityBarFillClass} /></span>
              </span>
            </span>
            <span className={capabilityDescClass}>{item.body}</span>
            {item.illustration && (
              <img
                className="pointer-events-none absolute bottom-0 left-1/2 h-[84px] w-[120px] -translate-x-1/2 object-contain object-bottom"
                src={item.illustration}
                alt=""
              />
            )}
          </button>
        ))}
        </div>
      </div>
    </section>
  );
}

/** 指标色调类型。 */
type MetricTone = 'default' | 'positive' | 'negative';

/** 指标背景色调到 Tailwind 类名的映射。 */
const metricToneClass: Record<MetricTone, string> = {
  default:
    'border-[0.5px] border-[#e3e7f1] bg-transparent hover:bg-[#f7f8fa] in-data-[theme=dark]:border-[#343741] in-data-[theme=dark]:hover:bg-white/5',
  positive: 'bg-[#e9f7ef] hover:bg-[#dcf1e5] in-data-[theme=dark]:bg-[#173a29] in-data-[theme=dark]:hover:bg-[#1c452f]',
  negative: 'bg-[#fce7e7] hover:bg-[#f9dada] in-data-[theme=dark]:bg-[#3d1f1f] in-data-[theme=dark]:hover:bg-[#4a2626]',
};

/** 指标数值色调到 Tailwind 类名的映射。 */
const metricValueToneClass: Record<MetricTone, string> = {
  default: 'text-[#18181a] in-data-[theme=dark]:text-[#f0f2f6]',
  positive: 'text-[#2cb360] in-data-[theme=dark]:text-[#4fd189]',
  negative: 'text-[#d20b0b] in-data-[theme=dark]:text-[#f26565]',
};

/**
 * 可点击的指标磁贴组件。
 *
 * @param label - 指标标签
 * @param value - 指标数值
 * @param suffix - 数值后缀（如"%"）
 * @param tone - 色调（default/positive/negative）
 * @param onClick - 点击回调
 * @returns 指标磁贴 JSX
 */
function ClickableMetric({
  label,
  value,
  suffix = '',
  tone = 'default',
  onClick,
}: {
  label: string;
  value: number;
  suffix?: string;
  tone?: MetricTone;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex min-w-px flex-[1_0_0] cursor-pointer flex-col justify-center gap-[4px] rounded-[20px] px-[32px] py-[16px] text-left transition-colors ${metricToneClass[tone]}`}
    >
      <strong className={`text-[18px] font-medium leading-none ${metricValueToneClass[tone]}`}>{value}{suffix}</strong>
      <span className="text-[12px] leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">{label}</span>
    </button>
  );
}

/** 各活动类型的圆点颜色映射，日/周/月视图共享。 */
const ACTIVITY_DOT: Record<string, string> = {
  chat: 'bg-[#4f92ff]',
  task: 'bg-[#ff9138]',
  sop: 'bg-[#2cb360]',
  tool: 'bg-[#9b6dff]',
  knowledge: 'bg-[#12b5c9]',
  skill: 'bg-[#f2589f]',
};

/** 带时间戳的事件类型。 */
type TrackEvent = { time: number; name: string };

/** 时间线轨道配置。 */
type TimelineTrackConfig = {
  key: string;
  label: string;
  unit: string;
  dot: string;
  bar: string;
};

/** 六类活动轨道配置。 */
const TIMELINE_TRACKS: TimelineTrackConfig[] = [
  {
    key: 'chat',
    label: '对话',
    unit: '次对话',
    dot: ACTIVITY_DOT.chat,
    bar: 'bg-[#e8f0ff] in-data-[theme=dark]:bg-[#1d2c47]',
  },
  {
    key: 'task',
    label: '定时任务',
    unit: '个任务',
    dot: ACTIVITY_DOT.task,
    bar: 'bg-[#fff1e3] in-data-[theme=dark]:bg-[#3a2c1a]',
  },
  {
    key: 'sop',
    label: '新增SOP',
    unit: '个 SOP',
    dot: ACTIVITY_DOT.sop,
    bar: 'bg-[#e9f7ef] in-data-[theme=dark]:bg-[#173a29]',
  },
  {
    key: 'tool',
    label: '新增工具',
    unit: '个工具',
    dot: ACTIVITY_DOT.tool,
    bar: 'bg-[#f1ecff] in-data-[theme=dark]:bg-[#2c2544]',
  },
  {
    key: 'knowledge',
    label: '新增知识',
    unit: '个知识',
    dot: ACTIVITY_DOT.knowledge,
    bar: 'bg-[#e2f6f9] in-data-[theme=dark]:bg-[#123037]',
  },
  {
    key: 'skill',
    label: '新增技能',
    unit: '个技能',
    dot: ACTIVITY_DOT.skill,
    bar: 'bg-[#fde8f1] in-data-[theme=dark]:bg-[#3d1e2e]',
  },
];

/** 单日活动项。 */
type DayActivity = { label: string; dot: string; time?: string };

/** 活动时间线组件的属性类型。 */
type ActivityTimelineProps = {
  events: AgentWorkRecordEventRead[];
};

/**
 * 活动时间线组件。
 * 支持日/周/月三种视图模式，可前后翻页和通过日期选择器跳转。
 *
 * 数据处理流程：
 * 1. 将原始事件按轨道类型分组并提取时间戳
 * 2. 日视图：将事件按 2 小时为单位聚合为时间条
 * 3. 周/月视图：将事件按天聚合为日历格子
 *
 * @param events - 活动事件列表
 * @returns 活动时间线 JSX
 */
function ActivityTimeline({ events }: ActivityTimelineProps) {
  const [mode, setMode] = useState<TimelineMode>('day');
  const [anchor, setAnchor] = useState<number>(() => startOfDay(new Date()).getTime());

  // 按轨道类型分组事件，提取时间戳
  const eventsByTrack = useMemo(() => {
    const collect = (entries: Array<{ value?: string; name?: string }>) =>
      entries
        .map((entry) => ({ time: entry.value ? new Date(entry.value).getTime() : Number.NaN, name: entry.name || '' }))
        .filter((entry) => Number.isFinite(entry.time));
    return TIMELINE_TRACKS.reduce<Record<string, TrackEvent[]>>((grouped, track) => {
      grouped[track.key] = collect(
        events
          .filter((item) => item.kind === track.key)
          .map((item) => ({ value: item.timestamp, name: staffdeckDisplayText(item.label) })),
      );
      return grouped;
    }, {});
  }, [events]);

  // 周/月视图的按天活动数据
  const itemsByDay = useMemo(
    () => (mode === 'month' || mode === 'week' ? buildDayActivities(events) : {}),
    [events, mode],
  );

  // 计算当前视图的时间范围和刻度
  const range = useMemo(() => timelineRange(mode, anchor), [mode, anchor]);
  const ticks = useMemo(() => timelineTicks(mode, range), [mode, range]);
  // 过滤出有活动的轨道，并计算时间条
  const activeTracks = useMemo(
    () =>
      TIMELINE_TRACKS.map((track) => ({
        track,
        segments: daySegments(eventsByTrack[track.key] || [], range),
      })).filter((item) => item.segments.length > 0),
    [eventsByTrack, range],
  );

  // 前/后翻页
  const shift = (direction: number) => setAnchor((prev) => shiftAnchor(mode, prev, direction));
  // 切换模式时归一化锚点
  const changeMode = (next: TimelineMode) => {
    setMode(next);
    setAnchor(normalizeAnchor(next, anchor));
  };

  return (
    <div className="flex w-full min-w-0 flex-col gap-[16px]">
      {/* 工具栏：时间范围标签 + 翻页/日期选择 + 模式切换 */}
      <div className="flex h-[36px] flex-wrap items-center justify-between gap-[12px]">
        <div className="flex items-center gap-[6px] text-[14px] text-[#858b9c] in-data-[theme=dark]:text-[#8b93a6]">
          <IconProfileCalendar className="size-[14px] shrink-0" />
          {formatAnchorLabel(mode, range)}
        </div>
        <div className="flex items-center gap-[24px] rounded-[8px] border border-[#e3e7f1] px-[12px] py-[8px] in-data-[theme=dark]:border-[#343741]">
          <button
            type="button"
            onClick={() => shift(-1)}
            className="flex size-[14px] items-center justify-center text-[#464c5e] transition-colors hover:text-[#18181a] in-data-[theme=dark]:text-[#c9cede]"
            aria-label="上一个周期"
          >
            <TimelineChevron direction="left" />
          </button>
          <TimelineDatePicker
            mode={mode}
            anchor={anchor}
            label={formatTimelineRange(mode, range)}
            onPick={setAnchor}
          />
          <button
            type="button"
            onClick={() => shift(1)}
            className="flex size-[14px] items-center justify-center text-[#464c5e] transition-colors hover:text-[#18181a] in-data-[theme=dark]:text-[#c9cede]"
            aria-label="下一个周期"
          >
            <TimelineChevron direction="right" />
          </button>
        </div>
        <div className="flex items-center gap-[12px]">
          {TIMELINE_MODES.map((item) => (
            <button
              type="button"
              key={item.key}
              onClick={() => changeMode(item.key)}
              className={`flex w-[50px] items-center justify-center px-[8px] text-[12px] transition-colors ${
                mode === item.key
                  ? 'font-medium text-[#464c5e] in-data-[theme=dark]:text-[#f0f2f6]'
                  : 'text-[#757f9c] hover:text-[#464c5e] in-data-[theme=dark]:text-[#8b93a6]'
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      {/* 根据模式渲染不同视图 */}
      {mode === 'month' ? (
        <MonthCalendar anchor={anchor} itemsByDay={itemsByDay} />
      ) : mode === 'week' ? (
        <WeekCalendar anchor={anchor} itemsByDay={itemsByDay} />
      ) : activeTracks.length === 0 ? (
        <TimelineEmptyState text="当日暂无活动记录" />
      ) : (
        /* 日视图：多轨道时间条 */
        <div className="flex min-h-[178px] w-full flex-col gap-[16px]">
          <div className="relative flex flex-1 w-full flex-col justify-center overflow-hidden rounded-[20px] px-[12px] py-[16px]">
            {/* 网格背景线 */}
            <div className="pointer-events-none absolute inset-x-[12px] inset-y-[8px] flex justify-between">
              {ticks.map((_, index) => (
                <span key={`grid-${index}`} className="w-px bg-[#eef1f7] in-data-[theme=dark]:bg-[#2c2f38]" />
              ))}
            </div>
            {/* 轨道时间条 */}
            <div className="relative z-10 flex min-h-[94px] flex-col gap-[8px]">
              {activeTracks.map(({ track, segments }) => (
                <div key={track.key} className="relative h-[26px] w-full">
                  {segments.map((segment, segmentIndex) => {
                    const label = trackBarLabel(track, segment);
                    return (
                      <HoverCard key={segmentIndex} openDelay={120} closeDelay={80}>
                        <HoverCardTrigger asChild>
                          <div
                            className={`absolute top-0 flex h-[26px] min-w-0 cursor-default items-center gap-[6px] overflow-hidden rounded-[8px] px-[8px] py-[4px] ${track.bar}`}
                            style={{ left: `${segment.left}%`, width: `${segment.width}%` }}
                          >
                            <span className={`size-[6px] shrink-0 rounded-full ${track.dot}`} />
                            <span className="truncate text-[10px] leading-none capitalize text-[#464c5e] in-data-[theme=dark]:text-[#f0f2f6]">
                              {label}
                            </span>
                          </div>
                        </HoverCardTrigger>
                        <HoverCardContent align="start" sideOffset={6} className="w-auto max-w-[300px] p-[10px]">
                          <div className="mb-[8px] flex items-center gap-[6px]">
                            <span className={`size-[6px] shrink-0 rounded-full ${track.dot}`} />
                            <span className="text-[12px] font-medium text-[#18181a] in-data-[theme=dark]:text-[#f0f2f6]">
                              {track.label}
                            </span>
                            <span className="text-[11px] text-[#858b9c]">
                              共{segment.count}
                              {track.unit}
                            </span>
                          </div>
                          <div className="flex flex-col gap-[4px]">
                            {segment.events.slice(0, 12).map((event, eventIndex) => (
                              <div
                                key={`${track.key}-${event.time}-${eventIndex}`}
                                className="flex items-start gap-[8px] text-[11px] leading-[15px]"
                              >
                                <span className="shrink-0 tabular-nums text-[#858b9c]">
                                  {formatHm(new Date(event.time))}
                                </span>
                                <span className="flex-1 break-words text-[#464c5e] in-data-[theme=dark]:text-[#c9cede]">
                                  {event.name || track.label}
                                </span>
                              </div>
                            ))}
                            {segment.events.length > 12 && (
                              <div className="text-[11px] text-[#858b9c]">…等{segment.events.length}项</div>
                            )}
                          </div>
                        </HoverCardContent>
                      </HoverCard>
                    );
                  })}
                </div>
              ))}
            </div>
          </div>

          {/* 时间刻度行 */}
          <div className="flex h-[16px] w-full items-center justify-between px-[12px] text-[12px] leading-none text-[#858b9c] in-data-[theme=dark]:text-[#8b93a6]">
            {ticks.map((tick, index) => (
              <span key={`tick-${index}`} className="relative w-0">
                <span className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 whitespace-nowrap">
                  {tick}
                </span>
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * 时间线空状态组件。
 *
 * @param text - 提示文案
 * @returns 空状态 JSX
 */
function TimelineEmptyState({ text }: { text: string }) {
  return (
    <div className="flex min-h-[178px] w-full flex-col items-center justify-center gap-[10px] rounded-[20px] border border-dashed border-[#e3e7f1] in-data-[theme=dark]:border-[#343741]">
      <IconProfileCalendar className="size-[24px] text-[#c0c5d2] in-data-[theme=dark]:text-[#5b606d]" />
      <span className="text-[13px] leading-none text-[#858b9c] in-data-[theme=dark]:text-[#8b93a6]">
        {text}
      </span>
    </div>
  );
}

/**
 * 周视图日历组件。
 * 横向排列 7 天，每天显示活动列表，超过 4 项可展开/收起。
 *
 * @param anchor - 锚点时间戳
 * @param itemsByDay - 按天索引的活动数据
 * @returns 周视图日历 JSX
 */
function WeekCalendar({
  anchor,
  itemsByDay,
}: {
  anchor: number;
  itemsByDay: Record<string, DayActivity[]>;
}) {
  const weekStart = startOfWeek(new Date(anchor));
  const days = Array.from({ length: 7 }, (_, index) => {
    const date = new Date(weekStart);
    date.setDate(date.getDate() + index);
    return date;
  });
  const todayKey = dateKey(new Date());
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const toggle = (key: string) => setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));

  const hasActivity = days.some((date) => (itemsByDay[dateKey(date)] || []).length > 0);
  if (!hasActivity) {
    return <TimelineEmptyState text="本周暂无活动记录" />;
  }

  return (
    <div className="flex min-h-[178px] w-full min-w-0 items-stretch rounded-[20px]">
      {days.map((date, index) => {
        const key = dateKey(date);
        const isToday = key === todayKey;
        const items = itemsByDay[key] || [];
        const isExpanded = Boolean(expanded[key]);
        // 展开或 ≤4 项时全部显示，否则只显示前 3 项
        const visible = isExpanded || items.length <= 4 ? items : items.slice(0, 3);
        const overflow = items.length - visible.length;

        return (
          <Fragment key={key}>
            {index > 0 && (
              <span className="my-[12px] w-px shrink-0 self-stretch bg-[#eef1f7] in-data-[theme=dark]:bg-[#2c2f38]" />
            )}
            <div
              className={`flex min-w-px flex-1 flex-col gap-[12px] rounded-[18px] px-[12px] py-[10px] ${
                isToday ? 'bg-[#e8f0ff] in-data-[theme=dark]:bg-[#1d2c47]' : ''
              }`}
            >
              <span className="text-[14px] leading-none text-[#858b9c] in-data-[theme=dark]:text-[#8b93a6]">
                {date.getDate()}
              </span>
              <div className="flex w-full flex-col gap-[2px]">
                {visible.map((item, itemIndex) => (
                  <HoverCard key={`${key}-${itemIndex}`} openDelay={120} closeDelay={80}>
                    <HoverCardTrigger asChild>
                      <div className="flex cursor-default items-center gap-[6px] rounded-[8px] p-[4px] transition-colors hover:bg-[#f6f6f6] in-data-[theme=dark]:hover:bg-[#2b2d33]">
                        <span className={`size-[6px] shrink-0 rounded-full ${item.dot}`} />
                        <span className="truncate text-[10px] leading-none capitalize text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
                          {item.label}
                        </span>
                      </div>
                    </HoverCardTrigger>
                    <HoverCardContent align="start" sideOffset={6} className="w-auto max-w-[300px] p-[10px]">
                      <div className="flex items-start gap-[8px]">
                        <span className={`mt-[4px] size-[6px] shrink-0 rounded-full ${item.dot}`} />
                        <span className="flex-1 break-words text-[12px] leading-[17px] text-[#464c5e] in-data-[theme=dark]:text-[#c9cede]">
                          {item.time ? (
                            <span className="mr-[6px] tabular-nums text-[#858b9c]">{item.time}</span>
                          ) : null}
                          {item.label}
                        </span>
                      </div>
                    </HoverCardContent>
                  </HoverCard>
                ))}
                {/* 展开/收起按钮 */}
                {overflow > 0 && (
                  <button
                    type="button"
                    onClick={() => toggle(key)}
                    className="flex items-center gap-[6px] rounded-[8px] p-[4px] text-left transition-colors hover:bg-[#f6f6f6] in-data-[theme=dark]:hover:bg-[#2b2d33]"
                  >
                    <span className="truncate text-[10px] leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
                      还有{overflow}项
                    </span>
                  </button>
                )}
                {isExpanded && items.length > 4 && (
                  <button
                    type="button"
                    onClick={() => toggle(key)}
                    className="flex items-center gap-[6px] rounded-[8px] p-[4px] text-left transition-colors hover:bg-[#f6f6f6] in-data-[theme=dark]:hover:bg-[#2b2d33]"
                  >
                    <span className="truncate text-[10px] leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
                      收起
                    </span>
                  </button>
                )}
              </div>
            </div>
          </Fragment>
        );
      })}
    </div>
  );
}

/**
 * 月视图日历组件。
 * 标准月历布局，每天格子显示活动列表，超过 4 项可展开/收起。
 *
 * @param anchor - 锚点时间戳
 * @param itemsByDay - 按天索引的活动数据
 * @returns 月视图日历 JSX
 */
function MonthCalendar({
  anchor,
  itemsByDay,
}: {
  anchor: number;
  itemsByDay: Record<string, DayActivity[]>;
}) {
  const weeks = monthCalendarWeeks(anchor);
  const month = new Date(anchor).getMonth();
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const toggle = (key: string) => setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));
  return (
    <div className="w-full overflow-hidden rounded-[20px] border border-[#eef1f7] in-data-[theme=dark]:border-[#2c2f38]">
      {/* 星期表头 */}
      <div className="grid grid-cols-7">
        {['周日', '周一', '周二', '周三', '周四', '周五', '周六'].map((day) => (
          <div
            key={day}
            className="px-[12px] py-[8px] text-[12px] leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]"
          >
            {day}
          </div>
        ))}
      </div>
      {weeks.map((week) => (
        <div
          key={dateKey(week[0])}
          className="grid grid-cols-7 border-t border-[#eef1f7] in-data-[theme=dark]:border-[#2c2f38]"
        >
          {week.map((date) => {
            const key = dateKey(date);
            const items = itemsByDay[key] || [];
            const isExpanded = Boolean(expanded[key]);
            const visible = isExpanded || items.length <= 4 ? items : items.slice(0, 3);
            const overflow = items.length - visible.length;
            const inMonth = date.getMonth() === month;
            // 每月 1 号显示月份前缀
            const dayLabel = date.getDate() === 1 ? `${date.getMonth() + 1}月1日` : `${date.getDate()}`;
            return (
              <div
                key={key}
                className={`flex min-h-[136px] flex-col gap-[8px] px-[12px] py-[10px] ${inMonth ? '' : 'opacity-45'}`}
              >
                <span className="text-[14px] leading-none text-[#858b9c] in-data-[theme=dark]:text-[#8b93a6]">
                  {dayLabel}
                </span>
                <div className="flex flex-col gap-[2px]">
                  {visible.map((item, index) => (
                    <HoverCard key={`${key}-${index}`} openDelay={120} closeDelay={80}>
                      <HoverCardTrigger asChild>
                        <div className="flex cursor-default items-center gap-[6px] rounded-[8px] p-[4px] transition-colors hover:bg-[#f6f6f6] in-data-[theme=dark]:hover:bg-[#2b2d33]">
                          <span className={`size-[6px] shrink-0 rounded-full ${item.dot}`} />
                          <span className="truncate text-[10px] leading-none capitalize text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
                            {item.label}
                          </span>
                        </div>
                      </HoverCardTrigger>
                      <HoverCardContent align="start" sideOffset={6} className="w-auto max-w-[300px] p-[10px]">
                        <div className="flex items-start gap-[8px]">
                          <span className={`mt-[4px] size-[6px] shrink-0 rounded-full ${item.dot}`} />
                          <span className="flex-1 break-words text-[12px] leading-[17px] text-[#464c5e] in-data-[theme=dark]:text-[#c9cede]">
                            {item.time ? (
                              <span className="mr-[6px] tabular-nums text-[#858b9c]">{item.time}</span>
                            ) : null}
                            {item.label}
                          </span>
                        </div>
                      </HoverCardContent>
                    </HoverCard>
                  ))}
                  {overflow > 0 && (
                    <button
                      type="button"
                      onClick={() => toggle(key)}
                      className="flex items-center gap-[6px] rounded-[8px] p-[4px] text-left transition-colors hover:bg-[#f6f6f6] in-data-[theme=dark]:hover:bg-[#2b2d33]"
                    >
                      <span className="truncate text-[10px] leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
                        还有{overflow}项
                      </span>
                    </button>
                  )}
                  {isExpanded && items.length > 4 && (
                    <button
                      type="button"
                      onClick={() => toggle(key)}
                      className="flex items-center gap-[6px] rounded-[8px] p-[4px] text-left transition-colors hover:bg-[#f6f6f6] in-data-[theme=dark]:hover:bg-[#2b2d33]"
                    >
                      <span className="truncate text-[10px] leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
                        收起
                      </span>
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}

/**
 * 时间线导航箭头 SVG 组件。
 * @param direction - 方向：left 或 right
 * @returns 箭头 SVG
 */
function TimelineChevron({ direction }: { direction: 'left' | 'right' }) {
  return (
    <svg viewBox="0 0 14 14" fill="none" className="size-[14px]" aria-hidden>
      <path
        d={direction === 'left' ? 'M9 3L5 7l4 4' : 'M5 3l4 4-4 4'}
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * 将日期归零到当天的 00:00:00。
 * @param date - 原始日期
 * @returns 当天起始日期
 */
function startOfDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

/**
 * 计算日期所在周的起始日（周日）。
 * @param date - 原始日期
 * @returns 本周起始日期
 */
function startOfWeek(date: Date): Date {
  const start = startOfDay(date);
  start.setDate(start.getDate() - start.getDay());
  return start;
}

/**
 * 根据模式和时间锚点计算时间范围。
 * @param mode - 时间线模式
 * @param anchor - 锚点时间戳
 * @returns 包含 start 和 end 的范围对象
 */
function timelineRange(mode: TimelineMode, anchor: number): { start: number; end: number } {
  const base = new Date(anchor);
  if (mode === 'day') {
    const start = startOfDay(base);
    return { start: start.getTime(), end: start.getTime() + 24 * 60 * 60 * 1000 };
  }
  if (mode === 'week') {
    const start = startOfWeek(base);
    return { start: start.getTime(), end: start.getTime() + 7 * 24 * 60 * 60 * 1000 };
  }
  // month 模式：从当月 1 号到下月 1 号
  const start = new Date(base.getFullYear(), base.getMonth(), 1);
  const end = new Date(base.getFullYear(), base.getMonth() + 1, 1);
  return { start: start.getTime(), end: end.getTime() };
}

/**
 * 将锚点归一化到当前模式的起始位置。
 * @param mode - 时间线模式
 * @param anchor - 原始锚点
 * @returns 归一化后的锚点
 */
function normalizeAnchor(mode: TimelineMode, anchor: number): number {
  return timelineRange(mode, anchor).start;
}

/**
 * 按方向翻页，移动锚点到上一个或下一个周期。
 * @param mode - 时间线模式
 * @param anchor - 当前锚点
 * @param direction - 移动方向（-1 前退，+1 后进）
 * @returns 移动后的锚点
 */
function shiftAnchor(mode: TimelineMode, anchor: number, direction: number): number {
  const base = new Date(anchor);
  if (mode === 'day') base.setDate(base.getDate() + direction);
  else if (mode === 'week') base.setDate(base.getDate() + direction * 7);
  else base.setMonth(base.getMonth() + direction);
  return normalizeAnchor(mode, base.getTime());
}

/**
 * 生成日视图中时间条的标签文本。
 * @param track - 轨道配置
 * @param bar - 时间条数据（计数和名称列表）
 * @returns 标签文本
 */
function trackBarLabel(
  track: TimelineTrackConfig,
  bar: { count: number; names: string[] },
): string {
  if (track.key === 'chat') return `对话${bar.count}条`;
  if (!bar.names.length) return track.label;
  const suffix = bar.names.length > 1 ? ` 等${bar.names.length}项` : '';
  return `${track.label} ${bar.names[0]}${suffix}`;
}

/** 日视图中时间条的合并数据结构。 */
type DaySegment = { left: number; width: number; count: number; names: string[]; events: TrackEvent[] };

// 日视图的时间网格常量：一天 24 小时，按 2 小时一格
const DAY_HOURS = 24;
const HOUR_MS = 60 * 60 * 1000;

/**
 * 日视图：将事件按 2 小时为单位聚合为时间条段。
 *
 * 算法说明：
 * - 每个事件向下取整到所在 2 小时格的起始（如 6:26→6点格）
 * - 格的结束为起始+2（如 6→6-8 格）
 * - 相邻或接触的同轨道格会合并为一个更宽的时间条
 *
 * @param events - 轨道事件列表
 * @param range - 时间范围
 * @returns 合并后的时间条段数组
 */
function daySegments(events: TrackEvent[], range: { start: number; end: number }): DaySegment[] {
  const inRange = events
    .filter((event) => event.time >= range.start && event.time < range.end)
    .sort((a, b) => a.time - b.time);
  if (!inRange.length) return [];

  // 按格子合并事件
  const merged: Array<{ start: number; end: number; events: TrackEvent[] }> = [];
  for (const event of inRange) {
    const hour = (event.time - range.start) / HOUR_MS;
    const start = Math.max(0, Math.floor(hour / 2) * 2);
    const end = Math.min(DAY_HOURS, start + 2);
    const last = merged[merged.length - 1];
    if (last && start <= last.end) {
      // 与上一段相邻或重叠，合并
      last.end = Math.max(last.end, end);
      last.events.push(event);
    } else {
      merged.push({ start, end, events: [event] });
    }
  }

  // 转换为百分比位置
  return merged.map((block) => ({
    left: (block.start / DAY_HOURS) * 100,
    width: ((block.end - block.start) / DAY_HOURS) * 100,
    count: block.events.length,
    names: Array.from(new Set(block.events.map((event) => event.name).filter(Boolean))),
    events: block.events,
  }));
}

/**
 * 根据模式和时间范围生成时间刻度标签。
 * @param mode - 时间线模式
 * @param range - 时间范围
 * @returns 刻度标签数组
 */
function timelineTicks(mode: TimelineMode, range: { start: number; end: number }): string[] {
  if (mode === 'day') {
    // 日视图：每 2 小时一个刻度（0, 2, 4, ... 24）
    return Array.from({ length: 13 }, (_, index) => formatHour(index * 2));
  }
  if (mode === 'week') {
    // 周视图：7 天
    return Array.from({ length: 7 }, (_, index) => {
      const date = new Date(range.start + index * 24 * 60 * 60 * 1000);
      return `${TIMELINE_WEEKDAYS[date.getDay()]} ${date.getDate()}`;
    });
  }
  // 月视图：按天均匀取约 8 个刻度
  const start = new Date(range.start);
  const daysInMonth = new Date(start.getFullYear(), start.getMonth() + 1, 0).getDate();
  const step = Math.max(2, Math.round(daysInMonth / 8));
  const ticks: string[] = [];
  for (let day = 1; day <= daysInMonth; day += step) {
    ticks.push(`${start.getMonth() + 1}/${day}`);
  }
  return ticks;
}

/**
 * 将小时数格式化为 12 小时制标签。
 * @param hour - 小时数（0-24）
 * @returns 格式化后的标签（如"12AM"、"2PM"）
 */
function formatHour(hour: number): string {
  if (hour === 0 || hour === 24) return '12AM';
  if (hour === 12) return '12PM';
  return hour < 12 ? `${hour}AM` : `${hour - 12}PM`;
}

/**
 * 将日期格式化为 YYYY/MM/DD 字符串。
 * @param date - 日期对象
 * @returns 格式化后的日期字符串
 */
function formatTimelineDate(date: Date): string {
  const month = `${date.getMonth() + 1}`.padStart(2, '0');
  const day = `${date.getDate()}`.padStart(2, '0');
  return `${date.getFullYear()}/${month}/${day}`;
}

/**
 * 根据模式和时间范围生成日期选择器显示标签。
 * @param mode - 时间线模式
 * @param range - 时间范围
 * @returns 显示标签
 */
function formatTimelineRange(mode: TimelineMode, range: { start: number; end: number }): string {
  const start = new Date(range.start);
  if (mode === 'day') return formatTimelineDate(start);
  const last = new Date(range.end - 24 * 60 * 60 * 1000);
  const short = (date: Date) => `${date.getMonth() + 1}/${date.getDate()}`;
  if (mode === 'week') return `${short(start)} - ${short(last)}`;
  return `${start.getFullYear()}/${`${start.getMonth() + 1}`.padStart(2, '0')}`;
}

/**
 * 时间线日期选择器组件（Popover 弹出日历）。
 * 支持日/周/月三种模式的选择，以及快捷选项（今天、昨天、本周、上周等）。
 *
 * @param mode - 当前模式
 * @param anchor - 当前锚点
 * @param label - 触发按钮显示的标签
 * @param onPick - 选择日期后的回调
 * @returns 日期选择器 JSX
 */
function TimelineDatePicker({
  mode,
  anchor,
  label,
  onPick,
}: {
  mode: TimelineMode;
  anchor: number;
  label: string;
  onPick: (ms: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const [viewDate, setViewDate] = useState(() => new Date(anchor));

  const handleOpenChange = (next: boolean) => {
    // 打开时同步视图日期到当前锚点
    if (next) setViewDate(new Date(anchor));
    setOpen(next);
  };
  const commit = (date: Date) => {
    onPick(normalizeAnchor(mode, date.getTime()));
    setOpen(false);
  };

  const selected = new Date(anchor);
  const selectedWeekStart = startOfWeek(selected).getTime();
  const shiftView = (deltaMonth: number, deltaYear: number) =>
    setViewDate((prev) => new Date(prev.getFullYear() + deltaYear, prev.getMonth() + deltaMonth, 1));

  const now = new Date();
  // 根据模式生成快捷选项
  const shortcuts: { label: string; date: Date }[] =
    mode === 'day'
      ? [
          { label: '今天', date: now },
          { label: '昨天', date: new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1) },
        ]
      : mode === 'week'
        ? [
            { label: '本周', date: now },
            { label: '上周', date: new Date(now.getFullYear(), now.getMonth(), now.getDate() - 7) },
          ]
        : [
            { label: '本月', date: now },
            { label: '上月', date: new Date(now.getFullYear(), now.getMonth() - 1, 1) },
          ];

  return (
    <Popover open={open} onOpenChange={handleOpenChange}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="text-[12px] whitespace-nowrap text-[#464c5e] transition-colors hover:text-[#18181a] in-data-[theme=dark]:text-[#c9cede]"
        >
          {label}
        </button>
      </PopoverTrigger>
      <PopoverContent align="center" className="w-auto p-[12px]">
        {/* 月份/年份翻页 */}
        <div className="mb-[8px] flex items-center justify-between">
          <button
            type="button"
            onClick={() => shiftView(mode === 'month' ? 0 : -1, mode === 'month' ? -1 : 0)}
            className="flex size-[24px] items-center justify-center rounded-[6px] text-[#464c5e] transition-colors hover:bg-[#f6f6f6] in-data-[theme=dark]:text-[#c9cede] in-data-[theme=dark]:hover:bg-[#2b2d33]"
            aria-label="上一页"
          >
            <TimelineChevron direction="left" />
          </button>
          <span className="text-[13px] font-medium text-[#18181a] in-data-[theme=dark]:text-[#f0f2f6]">
            {mode === 'month'
              ? `${viewDate.getFullYear()}年`
              : `${viewDate.getFullYear()}年${viewDate.getMonth() + 1}月`}
          </span>
          <button
            type="button"
            onClick={() => shiftView(mode === 'month' ? 0 : 1, mode === 'month' ? 1 : 0)}
            className="flex size-[24px] items-center justify-center rounded-[6px] text-[#464c5e] transition-colors hover:bg-[#f6f6f6] in-data-[theme=dark]:text-[#c9cede] in-data-[theme=dark]:hover:bg-[#2b2d33]"
            aria-label="下一页"
          >
            <TimelineChevron direction="right" />
          </button>
        </div>

        {/* 月模式：12 个月选择网格 */}
        {mode === 'month' ? (
          <div className="grid grid-cols-3 gap-[6px]">
            {Array.from({ length: 12 }, (_, index) => {
              const isSelected = selected.getFullYear() === viewDate.getFullYear() && selected.getMonth() === index;
              return (
                <button
                  key={index}
                  type="button"
                  onClick={() => commit(new Date(viewDate.getFullYear(), index, 1))}
                  className={`flex h-[36px] w-[64px] items-center justify-center rounded-[8px] text-[12px] transition-colors ${
                    isSelected
                      ? 'bg-[#4f92ff] text-white'
                      : 'text-[#464c5e] hover:bg-[#f6f6f6] in-data-[theme=dark]:text-[#c9cede] in-data-[theme=dark]:hover:bg-[#2b2d33]'
                  }`}
                >
                  {index + 1}月
                </button>
              );
            })}
          </div>
        ) : (
          /* 日/周模式：日历网格 */
          <>
            <div className="grid grid-cols-7">
              {['日', '一', '二', '三', '四', '五', '六'].map((day) => (
                <span
                  key={day}
                  className="flex size-[32px] items-center justify-center text-[11px] text-[#a7adbd] in-data-[theme=dark]:text-[#6b7080]"
                >
                  {day}
                </span>
              ))}
            </div>
            {monthCalendarWeeks(viewDate.getTime()).map((week) => (
              <div key={dateKey(week[0])} className="grid grid-cols-7">
                {week.map((date, dayIndex) => {
                  const inMonth = date.getMonth() === viewDate.getMonth();
                  const isSelected = mode === 'day' && isSameDay(date, selected);
                  const inWeek = mode === 'week' && startOfWeek(date).getTime() === selectedWeekStart;
                  // 周选中时首尾天有圆角
                  const bandRounding =
                    dayIndex === 0 ? 'rounded-l-[8px]' : dayIndex === 6 ? 'rounded-r-[8px]' : '';
                  const tone = isSelected
                    ? 'rounded-[8px] bg-[#4f92ff] font-medium text-white'
                    : inWeek
                      ? `${bandRounding} bg-[#e8f0ff] text-[#18181a] in-data-[theme=dark]:bg-[#1d2c47] in-data-[theme=dark]:text-[#f0f2f6]`
                      : inMonth
                        ? 'rounded-[8px] text-[#464c5e] hover:bg-[#f6f6f6] in-data-[theme=dark]:text-[#c9cede] in-data-[theme=dark]:hover:bg-[#2b2d33]'
                        : 'rounded-[8px] text-[#c0c5d2] hover:bg-[#f6f6f6] in-data-[theme=dark]:text-[#5b606d]';
                  return (
                    <button
                      key={dateKey(date)}
                      type="button"
                      onClick={() => commit(date)}
                      className={`flex size-[32px] items-center justify-center text-[12px] transition-colors ${tone}`}
                    >
                      {date.getDate()}
                    </button>
                  );
                })}
              </div>
            ))}
          </>
        )}

        {/* 快捷选项 */}
        <div className="mt-[8px] flex items-center gap-[6px] border-t border-[#eef1f7] pt-[8px] in-data-[theme=dark]:border-[#2c2f38]">
          {shortcuts.map((shortcut) => (
            <button
              key={shortcut.label}
              type="button"
              onClick={() => commit(shortcut.date)}
              className="rounded-[6px] px-[10px] py-[4px] text-[12px] text-[#464c5e] transition-colors hover:bg-[#f6f6f6] in-data-[theme=dark]:text-[#c9cede] in-data-[theme=dark]:hover:bg-[#2b2d33]"
            >
              {shortcut.label}
            </button>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}

/**
 * 判断两个日期是否为同一天。
 * @param a - 日期 a
 * @param b - 日期 b
 * @returns 是否同一天
 */
function isSameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

/**
 * 生成锚点标签（用于工具栏左侧显示）。
 * @param mode - 时间线模式
 * @param range - 时间范围
 * @returns 标签字符串
 */
function formatAnchorLabel(mode: TimelineMode, range: { start: number; end: number }): string {
  const start = new Date(range.start);
  if (mode === 'month') return `${start.getFullYear()}/${`${start.getMonth() + 1}`.padStart(2, '0')}`;
  return formatTimelineDate(start);
}

/**
 * 将时间戳格式化为 HH:MM 字符串。
 * @param date - 日期对象
 * @returns 格式化后的时间字符串
 */
function formatHm(date: Date): string {
  return `${`${date.getHours()}`.padStart(2, '0')}:${`${date.getMinutes()}`.padStart(2, '0')}`;
}

/**
 * 生成月历的周数组（包含前后月的填充天，形成完整的周→天网格）。
 * 以周日为每周起始，覆盖整个月份。
 *
 * @param anchor - 锚点时间戳
 * @returns 周数组（每周 7 天）
 */
function monthCalendarWeeks(anchor: number): Date[][] {
  const base = new Date(anchor);
  const monthStart = new Date(base.getFullYear(), base.getMonth(), 1);
  const monthEnd = new Date(base.getFullYear(), base.getMonth() + 1, 0);
  // 从本月第一天所在周的周日开始
  const gridStart = startOfWeek(monthStart);
  const weeks: Date[][] = [];
  const cursor = new Date(gridStart);
  while (cursor <= monthEnd || cursor.getDay() !== 0) {
    const week: Date[] = [];
    for (let day = 0; day < 7; day += 1) {
      week.push(new Date(cursor));
      cursor.setDate(cursor.getDate() + 1);
    }
    weeks.push(week);
    // 最多 6 周
    if (weeks.length > 6) break;
  }
  return weeks;
}

/**
 * 将活动事件按天聚合，用于周/月视图。
 * 对话事件按天汇总计数，其他事件保留逐条记录并添加类型前缀。
 *
 * @param events - 活动事件列表
 * @returns 按天索引的活动数据
 */
function buildDayActivities(events: AgentWorkRecordEventRead[]): Record<string, DayActivity[]> {
  const map: Record<string, DayActivity[]> = {};
  const push = (event: AgentWorkRecordEventRead, label: string) => {
    const date = new Date(event.timestamp);
    if (Number.isNaN(date.getTime())) return;
    const key = dateKey(date);
    (map[key] ||= []).push({ label, dot: ACTIVITY_DOT[event.kind], time: formatHm(date) });
  };

  // 对话事件按天汇总计数，放在每天列表最前
  const chatByDay: Record<string, number> = {};
  events.filter((item) => item.kind === 'chat').forEach((item) => {
    const date = new Date(item.timestamp);
    if (Number.isNaN(date.getTime())) return;
    const key = dateKey(date);
    chatByDay[key] = (chatByDay[key] || 0) + 1;
  });
  Object.entries(chatByDay).forEach(([dayKey, count]) => {
    (map[dayKey] ||= []).unshift({ label: `对话${count}条`, dot: ACTIVITY_DOT.chat });
  });

  // 非对话事件逐条添加，并加上类型前缀
  events.filter((item) => item.kind !== 'chat').forEach((item) => {
    const prefix = item.kind === 'sop'
      ? '新增SOP '
      : item.kind === 'tool'
        ? '新增工具 '
        : item.kind === 'knowledge'
          ? '新增知识 '
          : item.kind === 'skill'
            ? '新增技能 '
            : '';
    push(item, `${prefix}${staffdeckDisplayText(item.label)}`);
  });

  return map;
}

/**
 * 将日期格式化为 YYYY-MM-DD 键字符串。
 * @param date - 日期对象
 * @returns 日期键字符串
 */
export function dateKey(date: Date): string {
  const year = date.getFullYear();
  const month = `${date.getMonth() + 1}`.padStart(2, '0');
  const day = `${date.getDate()}`.padStart(2, '0');
  return `${year}-${month}-${day}`;
}

/**
 * 生成成长记录时间线事件。
 * 从 SOP、技能、工具三类资源中提取成长里程碑事件。
 *
 * @param sops - SOP 列表
 * @param generalSkills - 技能列表
 * @param tools - 工具列表
 * @returns 按时间排序的成长事件数组
 */
function growthTimeline(
  sops: SkillRead[],
  generalSkills: GeneralSkillRead[],
  tools: ToolRead[],
): GrowthEvent[] {
  const events: GrowthEvent[] = [];

  // SOP 事件：区分新增和进化（版本升级）
  sops.forEach((item) => {
    const evolved = Boolean(item.branch_head_version && item.branch_head_version !== item.branch_base_version);
    events.push({
      id: `sop-${item.id}`,
      kind: evolved ? 'SOP 进化' : '新增 SOP',
      title: item.name,
      description: evolved
        ? `本地版本从 ${item.branch_base_version || item.version} 进化到 ${item.branch_head_version || item.version}`
        : `新增 ${item.version} 版业务流程`,
      timestamp: stableGrowthTimestamp(item),
      icon: <StaffdeckIcon name="filter" />,
      tone: 'mint',
    });
  });

  // 技能事件：区分新增和升级（有实质性更新）
  generalSkills.forEach((item) => {
    const upgraded = isMeaningfullyUpdated(item.created_at, item.updated_at);
    events.push({
      id: `general-${item.id}`,
      kind: upgraded ? '技能升级' : '新增技能',
      title: item.name,
      description: upgraded ? '技能说明、权限或运行配置有更新' : `新增 ${item.slug} 通用能力`,
      timestamp: stableGrowthTimestamp(item),
      icon: <StaffdeckIcon name="spark" />,
      tone: 'teal',
    });
  });

  // 工具事件：统一为新增
  tools.forEach((item) => {
    events.push({
      id: `tool-${item.id}`,
      kind: '新增工具',
      title: item.display_name || item.name,
      description: `${item.bucket || '工具'} · ${item.tool_type.toUpperCase()} 调用能力`,
      timestamp: stableGrowthTimestamp(item),
      icon: <StaffdeckIcon name="tool" />,
      tone: 'green',
    });
  });

  // 过滤掉无时间戳的事件，并按时间升序排列
  return events
    .filter((item) => Boolean(item.timestamp))
    .sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());
}

/**
 * 从资源对象中提取稳定的成长时间戳。
 * 优先使用 metadata 中的专用时间字段（learned_at、assigned_at 等），最后回退到 created_at。
 *
 * @param item - 包含时间字段的资源对象
 * @returns 时间戳字符串（可能为空）
 */
function stableGrowthTimestamp(item: GrowthTimestampSource): string {
  const metadata = item.metadata || {};
  const candidates = [
    metadata.learned_at,
    metadata.assigned_at,
    metadata.installed_at,
    metadata.imported_at,
    metadata.created_at,
    item.created_at,
  ];
  return candidates.find((value): value is string => typeof value === 'string' && Boolean(value.trim())) || '';
}

/**
 * 判断资源是否有实质性更新（创建时间和更新时间差异超过 1 分钟）。
 * 用于区分"新增"和"升级"两种成长事件。
 *
 * @param createdAt - 创建时间
 * @param updatedAt - 更新时间
 * @returns 是否有实质性更新
 */
function isMeaningfullyUpdated(createdAt?: string, updatedAt?: string): boolean {
  if (!createdAt || !updatedAt) return false;
  return Math.abs(new Date(updatedAt).getTime() - new Date(createdAt).getTime()) > 60 * 1000;
}

/**
 * 将时间戳格式化为"月.日"格式（如"7.23"）。
 * @param value - 时间戳字符串
 * @returns 格式化后的日期标签
 */
function formatMonthDay(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '-';
  return `${date.getMonth() + 1}.${date.getDate()}`;
}

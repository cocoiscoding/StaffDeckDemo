/**
 * @file scheduled-tasks/shared.ts
 * @description 定时任务模块的共享类型定义与工具函数集合。
 *
 * 本文件集中维护定时任务（ScheduledTask）相关的：
 * - 表单数据结构（TaskFormValues）及初始值
 * - 列表筛选类型、筛选谓词及筛选标签配置
 * - 状态徽章的配色与文案映射
 * - 调度计划（schedule）的构建、解析与格式化逻辑
 * - 前后端时间格式互转工具
 *
 * 这些常量与纯函数被编辑器页面、列表页面及各子组件复用，
 * 是整个定时任务功能的数据契约层。
 */

import type { UnderlineTabItem } from '@/components/ui';
import { formatClientDateTime, parseBackendDateTime } from '@/lib/timezone';
import type { ScheduledTaskRead, ScheduledTaskRunRead } from '../../types';

/** localStorage 键名，用于持久化当前选中的数字员工（Agent）作用域 */
export const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';
/** 任务列表默认每页条数 */
export const TASK_PAGE_SIZE = 10;

/** 星期选项配置，value 与 JavaScript Date.getDay() 的 0-6 对齐（0=周一） */
export const WEEKDAY_OPTIONS = [
  { label: '周一', value: 0 },
  { label: '周二', value: 1 },
  { label: '周三', value: 2 },
  { label: '周四', value: 3 },
  { label: '周五', value: 4 },
  { label: '周六', value: 5 },
  { label: '周日', value: 6 },
];

/**
 * 定时任务编辑器表单的值类型。
 * 该结构是对后端 ScheduledTaskRead 的扁平化表示，方便表单组件直接绑定。
 */
export type TaskFormValues = {
  title: string;
  prompt: string;
  description?: string;
  /** 调度类型：一次性 / 每天 / 每周 / 每月 */
  schedule_type: 'once' | 'daily' | 'weekly' | 'monthly';
  /** 每天/每周/每月 的执行时间（HH:mm） */
  time: string;
  /** 一次性任务的精确执行时刻（datetime-local 格式） */
  run_at: string;
  /** 每周调度时选中的星期（0-6，对应周一到周日） */
  weekdays: number[];
  /** 每月调度时的日期（1-31） */
  day_of_month: number;
  /** 任务状态：启用或暂停 */
  status: 'active' | 'paused';
  /** 最大运行次数，不填表示无限制 */
  max_runs?: number;
};

/** 新建任务时表单的初始默认值 */
export const INITIAL_VALUES: TaskFormValues = {
  title: '',
  prompt: '',
  description: '',
  schedule_type: 'daily',
  time: '09:00',
  run_at: '',
  weekdays: [0],
  day_of_month: 1,
  status: 'active',
  max_runs: undefined,
};

/** 任务列表可用筛选维度 */
export type TaskListFilter = 'all' | 'pending' | 'completed' | 'paused';
/** 执行记录列表可用筛选维度 */
export type RunListFilter = 'all' | 'pending' | 'completed' | 'failed';

/** 任务列表筛选标签栏配置 */
export const TASK_FILTER_TABS: UnderlineTabItem<TaskListFilter>[] = [
  { label: '全部', value: 'all' },
  { label: '待完成', value: 'pending' },
  { label: '已完成', value: 'completed' },
  { label: '已暂停', value: 'paused' },
];
/** 执行记录列表筛选标签栏配置 */
export const RUN_FILTER_TABS: UnderlineTabItem<RunListFilter>[] = [
  { label: '全部', value: 'all' },
  { label: '待完成', value: 'pending' },
  { label: '已完成', value: 'completed' },
  { label: '失败/跳过', value: 'failed' },
];

/** 各筛选维度对应的行级判定谓词，返回 true 表示该行命中筛选 */
const TASK_FILTERS: Record<TaskListFilter, (row: ScheduledTaskRead) => boolean> = {
  all: () => true,
  pending: (row) => row.status === 'active',
  paused: (row) => row.status === 'paused',
  completed: (row) => row.status === 'completed',
};
const RUN_FILTERS: Record<RunListFilter, (row: ScheduledTaskRunRead) => boolean> = {
  all: () => true,
  pending: (row) => row.status === 'queued' || row.status === 'running',
  failed: (row) => row.status === 'failed' || row.status === 'skipped',
  completed: (row) => row.status === 'succeeded',
};

/**
 * 判断某条任务是否命中指定的列表筛选条件。
 * @param row 任务数据行
 * @param filter 当前激活的筛选维度
 * @returns 命中返回 true
 */
export function matchesTaskFilter(row: ScheduledTaskRead, filter: TaskListFilter): boolean {
  return TASK_FILTERS[filter](row);
}

/**
 * 判断某条执行记录是否命中指定的列表筛选条件。
 * @param row 执行记录数据行
 * @param filter 当前激活的筛选维度
 * @returns 命中返回 true
 */
export function matchesRunFilter(row: ScheduledTaskRunRead, filter: RunListFilter): boolean {
  return RUN_FILTERS[filter](row);
}

/** 状态徽章可选的色调 */
export type BadgeTone = 'blue' | 'orange' | 'green' | 'red' | 'gray';
/** 各色调对应的 Tailwind 样式类（背景色 + 文字色） */
export const BADGE_TONE_CLASS: Record<BadgeTone, string> = {
  blue: 'bg-[#e8f0ff] text-[#1a71ff]',
  orange: 'bg-[#fff2e5] text-[#ff7f00]',
  green: 'bg-[#e9f7ef] text-[#2cb360]',
  red: 'bg-[#fce7e7] text-[#d20b0b]',
  gray: 'bg-[#f2f3f7] text-[#858b9c]',
};
/** 任务状态 → 徽章色调 + 中文文案 */
export const TASK_STATUS_BADGE: Record<string, { tone: BadgeTone; text: string }> = {
  active: { tone: 'blue', text: '启用' },
  paused: { tone: 'orange', text: '暂停' },
  completed: { tone: 'green', text: '已完成' },
  archived: { tone: 'gray', text: '已删除' },
};
/** 执行记录状态 → 徽章色调 + 中文文案 */
export const RUN_STATUS_BADGE: Record<string, { tone: BadgeTone; text: string }> = {
  succeeded: { tone: 'green', text: '成功' },
  failed: { tone: 'red', text: '失败' },
  running: { tone: 'blue', text: '执行中' },
  skipped: { tone: 'gray', text: '已跳过' },
};

/** 合法的调度类型集合，用于校验后端返回值 */
const SCHEDULE_TYPES = new Set<TaskFormValues['schedule_type']>(['once', 'daily', 'weekly', 'monthly']);
/** 各调度类型 → 后端 schedule 对象的构建策略 */
const SCHEDULE_BUILDERS: Record<
  TaskFormValues['schedule_type'],
  (values: TaskFormValues) => Record<string, unknown>
> = {
  once: (values) => ({ run_at: values.run_at }),
  weekly: (values) => ({
    time: values.time || '09:00',
    weekdays: values.weekdays?.length ? values.weekdays : [0],
  }),
  monthly: (values) => ({
    time: values.time || '09:00',
    day_of_month: values.day_of_month || 1,
  }),
  daily: (values) => ({ time: values.time || '09:00' }),
};
/** 各调度类型 → 人类可读文案的格式化策略 */
const SCHEDULE_FORMATTERS: Record<
  TaskFormValues['schedule_type'],
  (row: ScheduledTaskRead, schedule: Record<string, unknown>) => string
> = {
  once: (row, schedule) => `一次性 · ${formatTime(String(schedule.run_at || row.next_run_at || ''))}`,
  weekly: (_row, schedule) => {
    // 将数字星期数组映射为中文标签并用顿号连接
    const days = Array.isArray(schedule.weekdays)
      ? schedule.weekdays
          .map((item) => WEEKDAY_OPTIONS[Number(item)]?.label)
          .filter(Boolean)
          .join('、')
      : '周一';
    return `每周 ${days} ${schedule.time || '09:00'}`;
  },
  monthly: (_row, schedule) => `每月 ${schedule.day_of_month || 1} 号 ${schedule.time || '09:00'}`,
  daily: (_row, schedule) => `每天 ${schedule.time || '09:00'}`,
};

/**
 * 根据表单值构建提交给后端的 schedule 对象。
 * 内部按 schedule_type 分发到对应的构建策略。
 * @param values 表单当前值
 * @returns 后端所需的 schedule 结构（字段因类型而异）
 */
export function buildSchedule(values: TaskFormValues): Record<string, unknown> {
  return SCHEDULE_BUILDERS[values.schedule_type](values);
}

/**
 * 将后端返回的任务数据行转换为表单可用的初始值。
 * 在编辑场景加载任务详情时调用。
 * @param row 后端任务对象
 * @returns 表单初始值
 */
export function taskToFormValues(row: ScheduledTaskRead): TaskFormValues {
  const schedule = row.schedule || {};
  return {
    title: row.title,
    prompt: row.prompt,
    description: row.description || '',
    schedule_type: normalizeScheduleType(row.schedule_type),
    time: String(schedule.time || '09:00'),
    // 后端时间为 UTC，需转为浏览器本地 datetime-local 格式
    run_at: toDatetimeLocal(String(schedule.run_at || row.next_run_at || '')),
    weekdays: Array.isArray(schedule.weekdays) ? schedule.weekdays.map((item) => Number(item)) : [0],
    day_of_month: Number(schedule.day_of_month || 1),
    status: row.status === 'active' ? 'active' : 'paused',
    max_runs: row.max_runs,
  };
}

/**
 * 将字符串安全地归一化为合法的调度类型。
 * 非法或未知值回退为 'daily'。
 * @param value 后端返回的 schedule_type 字符串
 * @returns 合法的调度类型
 */
export function normalizeScheduleType(value: string): TaskFormValues['schedule_type'] {
  const scheduleType = value as TaskFormValues['schedule_type'];
  return SCHEDULE_TYPES.has(scheduleType) ? scheduleType : 'daily';
}

/**
 * 将后端返回的时间字符串转换为 <input type="datetime-local"> 所需的本地格式（yyyy-MM-ddTHH:mm）。
 * @param value ISO/后端时间字符串
 * @returns 本地 datetime-local 字符串，无法解析时返回空串
 */
export function toDatetimeLocal(value: string): string {
  if (!value) return '';
  const date = parseBackendDateTime(value);
  if (Number.isNaN(date.getTime())) return '';
  // 利用时区偏移将 UTC 时间调整为本地时间后再截取
  const offset = date.getTimezoneOffset();
  const local = new Date(date.getTime() - offset * 60000);
  return local.toISOString().slice(0, 16);
}

/**
 * 将任务的调度计划格式化为人类可读的中文文案（如"每天 09:00"）。
 * @param row 任务数据行
 * @returns 格式化后的调度描述
 */
export function formatSchedule(row: ScheduledTaskRead): string {
  const schedule = row.schedule || {};
  return SCHEDULE_FORMATTERS[normalizeScheduleType(row.schedule_type)](row, schedule);
}

/**
 * 将时间字符串格式化为客户端本地展示文案。
 * @param value 时间字符串
 * @returns 格式化后的时间，空值时返回"暂无"
 */
export function formatTime(value?: string): string {
  return formatClientDateTime(value, '暂无');
}

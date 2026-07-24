/**
 * @file scheduled-tasks/ScheduledTaskEditorPage.tsx
 * @description 定时任务编辑器页面，支持新建与编辑两种模式。
 *
 * 页面分为两大区块：
 * 1. 任务说明：任务名称、执行 Prompt、内部备注
 * 2. 唤醒计划：启用状态、调度类型（每天/每周/每月/一次性）、执行时间、最大运行次数
 *
 * 数据流转：
 * - 新建模式：使用 INITIAL_VALUES 初始化空表单，提交时 POST 创建
 * - 编辑模式：通过 URL 参数 taskId 从后端 GET 任务详情并回填，提交时 PUT 更新
 * - 表单值通过 buildSchedule() 转换为后端所需的 schedule 结构
 *
 * 该文件导出两个薄包装组件 ScheduledTaskNewPage / ScheduledTaskEditPage，
 * 分别对应路由中的"新建"与"编辑"入口。
 */

import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { notify } from '@/components/ui/app-toast';
import { getClientTimeZone } from '@/lib/timezone';

import AppHeader from '@/components/AppHeader';
import { Button } from '@/components/ui/button';
import {
  Checkbox,
  Input,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
  Textarea,
} from '@/components/ui';
import { cn } from '@/lib/utils';

import { api, TENANT_ID } from '../../api/client';
import IconArrowRight from '../../assets/icons/arrow-right.svg?react';
import IconAlarm from '../../assets/icons/profile-alarm.svg?react';
import type { EnterpriseAuthUser } from '../../auth';
import type { ScheduledTaskRead } from '../../types';
import {
  ENTERPRISE_AGENT_STORAGE_KEY,
  INITIAL_VALUES,
  WEEKDAY_OPTIONS,
  buildSchedule,
  taskToFormValues,
  type TaskFormValues,
} from './shared';

/** 编辑器页面的公共属性：当前登录用户与登出回调 */
export type ScheduledTaskPageProps = {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
};

/**
 * 定时任务"新建"页面入口。
 * 以 mode="new" 渲染编辑器。
 * @param props 公共页面属性
 * @returns 编辑器组件
 */
export function ScheduledTaskNewPage(props: ScheduledTaskPageProps = {}) {
  return <ScheduledTaskEditorPage mode="new" {...props} />;
}

/**
 * 定时任务"编辑"页面入口。
 * 以 mode="edit" 渲染编辑器，通过 URL 中的 taskId 加载已有任务。
 * @param props 公共页面属性
 * @returns 编辑器组件
 */
export function ScheduledTaskEditPage(props: ScheduledTaskPageProps = {}) {
  return <ScheduledTaskEditorPage mode="edit" {...props} />;
}

/** 表单校验错误信息映射，key 为字段名 */
type FormErrors = Partial<Record<'title' | 'prompt' | 'run_at' | 'time' | 'weekdays', string>>;

/** 卡片容器样式 */
const CARD_CLASS =
  'rounded-[14px] border border-[#eceef1] bg-white p-[20px]';
/** 卡片标题样式 */
const CARD_TITLE_CLASS = 'mb-[16px] text-[14px] font-medium text-[#18181a]';
/** 字段标签样式 */
const FIELD_LABEL_CLASS = 'text-[13px] font-medium text-[#18181a]';
/** 字段错误提示样式 */
const FIELD_ERROR_CLASS = 'text-[12px] leading-none text-[#d20b0b]';

/**
 * 定时任务编辑器核心组件。
 *
 * 根据模式加载或初始化表单数据，提供表单校验、保存提交等能力。
 * 同时监听全局的员工作用域切换事件，确保提交时使用正确的 agentId。
 *
 * @param mode 编辑模式：'new' 新建 | 'edit' 编辑
 * @param currentUser 当前登录用户信息
 * @param onLogout 登出回调
 * @returns 编辑器页面 JSX
 */
function ScheduledTaskEditorPage({
  mode,
  currentUser,
  onLogout,
}: { mode: 'new' | 'edit' } & ScheduledTaskPageProps) {
  // 表单当前值
  const [values, setValues] = useState<TaskFormValues>(INITIAL_VALUES);
  // 表单校验错误
  const [errors, setErrors] = useState<FormErrors>({});
  // 是否正在加载已有任务（仅编辑模式）
  const [loading, setLoading] = useState(false);
  // 是否正在保存提交
  const [saving, setSaving] = useState(false);
  // 当前选中的数字员工 ID，从 localStorage 初始化
  const [agentId, setAgentId] = useState(
    () => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '',
  );
  const navigate = useNavigate();
  const { taskId } = useParams();
  const isEdit = mode === 'edit';
  const scheduleType = values.schedule_type;

  /**
   * 更新表单中某个字段的值。
   * @param key 字段名
   * @param value 新值
   */
  function update<K extends keyof TaskFormValues>(key: K, value: TaskFormValues[K]) {
    setValues((prev) => ({ ...prev, [key]: value }));
  }

  // 监听全局员工作用域切换事件，同步更新本地 agentId
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

  // 根据模式加载数据：新建时重置为初始值，编辑时从后端拉取任务详情
  useEffect(() => {
    if (!isEdit) {
      setValues(INITIAL_VALUES);
      return;
    }
    if (!taskId) return;
    setLoading(true);
    api
      .get<ScheduledTaskRead>(`/api/enterprise/scheduled-tasks/${taskId}?tenant_id=${TENANT_ID}`)
      .then((row) => {
        setAgentId(row.agent_id);
        // 将后端数据转换为表单结构后回填
        setValues(taskToFormValues(row));
      })
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载定时任务失败'))
      .finally(() => setLoading(false));
  }, [isEdit, taskId]);

  /**
   * 校验表单字段，将错误写入 state。
   * @returns 校验是否通过（无错误返回 true）
   */
  function validate(): boolean {
    const nextErrors: FormErrors = {};
    if (!values.title.trim()) nextErrors.title = '请填写任务名称';
    if (!values.prompt.trim()) nextErrors.prompt = '请填写任务描述';
    // 一次性任务需要精确执行时刻，周期任务需要每日时间
    if (values.schedule_type === 'once') {
      if (!values.run_at) nextErrors.run_at = '请选择执行时间';
    } else if (!values.time) {
      nextErrors.time = '请填写执行时间';
    }
    // 每周调度至少选择一个星期
    if (values.schedule_type === 'weekly' && !values.weekdays.length) {
      nextErrors.weekdays = '请选择星期';
    }
    setErrors(nextErrors);
    return Object.keys(nextErrors).length === 0;
  }

  /**
   * 保存定时任务：校验通过后构建 payload 并提交到后端。
   * 新建使用 POST，编辑使用 PUT。成功后更新表单或跳转。
   */
  async function save() {
    if (!validate()) return;
    if (!agentId) {
      notify.error('请先选择员工');
      return;
    }
    // 组装提交给后端的完整 payload
    const payload = {
      tenant_id: TENANT_ID,
      agent_id: agentId,
      title: values.title.trim(),
      prompt: values.prompt.trim(),
      description: values.description?.trim() || undefined,
      schedule_type: values.schedule_type,
      // 将扁平表单值转换为后端期望的 schedule 嵌套结构
      schedule: buildSchedule(values),
      timezone: getClientTimeZone(),
      status: values.status,
      // 并发策略：禁止并发（上一轮未结束则跳过本次）
      concurrency_policy: 'forbid',
      // 错过策略：合并（错过的执行合并为一次）
      misfire_policy: 'coalesce',
      max_runs: values.max_runs || undefined,
    };
    setSaving(true);
    try {
      const saved =
        isEdit && taskId
          ? await api.put<ScheduledTaskRead>(`/api/enterprise/scheduled-tasks/${taskId}`, payload)
          : await api.post<ScheduledTaskRead>('/api/enterprise/scheduled-tasks', payload);
      notify.success('定时任务已保存');
      if (!isEdit) {
        // 新建成功后跳转到编辑页（替换历史，避免回退重复创建）
        navigate(`/enterprise/scheduled-tasks/${saved.id}/edit`, { replace: true });
      } else {
        // 编辑成功后用最新数据刷新表单
        setValues(taskToFormValues(saved));
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存定时任务失败');
    } finally {
      setSaving(false);
    }
  }

  /**
   * 切换某一天的选中状态（每周调度场景）。
   * @param day 星期值（0-6）
   * @param checked 是否选中
   */
  function toggleWeekday(day: number, checked: boolean) {
    setValues((prev) => {
      const next = checked
        ? [...prev.weekdays, day]
        : prev.weekdays.filter((item) => item !== day);
      // 保持星期数组有序
      return { ...prev, weekdays: next.sort((a, b) => a - b) };
    });
  }

  return (
    <div
      className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]"
      aria-busy={loading || saving}
    >
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
        title={isEdit ? '编辑定时任务' : '新建空白定时任务'}
        description="保存后到点会拉起一个新的执行记录，并交给当前员工按 SOP、技能、资料和工具执行。"
      />
      {/* 顶部操作按钮：返回 + 保存 */}
      <div className="flex justify-end gap-[16px] mt-[20px] mb-[16px]">
        <Button
          variant="outline"
          onClick={() => navigate('/enterprise/scheduled-tasks')}
          className="h-8 gap-1 rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-5 text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]"
        >
          <IconArrowRight className="size-3.5 rotate-180" />
          返回定时任务
        </Button>
        <Button
          onClick={() => void save()}
          disabled={saving}
          className="h-8 gap-1 rounded-[10px] bg-[#18181a] px-5 text-[12px] font-normal text-white hover:bg-[#303030]"
        >
          保存
        </Button>
      </div>

      {/* 双栏布局：左任务说明，右唤醒计划 */}
      <div className="grid grid-cols-1 items-start gap-[20px] lg:grid-cols-2">
        {/* ===== 任务说明区块 ===== */}
        <section className={CARD_CLASS}>
          <h3 className={CARD_TITLE_CLASS}>任务说明</h3>
          <div className="flex flex-col gap-[16px]">
            {/* 任务名称 */}
            <div className="flex flex-col gap-[6px]">
              <Label htmlFor="task-title" className={FIELD_LABEL_CLASS}>
                任务名称
              </Label>
              <div className="relative">
                <IconAlarm className="pointer-events-none absolute left-[10px] top-1/2 size-[14px] -translate-y-1/2 text-[#858b9c]" />
                <Input
                  id="task-title"
                  className={cn('pl-[30px]', errors.title && 'border-destructive')}
                  maxLength={80}
                  placeholder="例如：每日交付质量复盘"
                  value={values.title}
                  onChange={(event) => update('title', event.target.value)}
                />
              </div>
              {errors.title && <p className={FIELD_ERROR_CLASS}>{errors.title}</p>}
            </div>

            {/* 任务 Prompt：每次执行时交给数字员工的指令 */}
            <div className="flex flex-col gap-[6px]">
              <Label htmlFor="task-prompt" className={FIELD_LABEL_CLASS}>
                每次执行时交给员工的任务
              </Label>
              <Textarea
                id="task-prompt"
                rows={7}
                maxLength={10000}
                className={cn(errors.prompt && 'border-destructive')}
                placeholder="描述员工每次执行时需要做什么，可以包含拆解要求、输出格式和注意事项。"
                value={values.prompt}
                onChange={(event) => update('prompt', event.target.value)}
              />
              <div className="flex items-center justify-between">
                {errors.prompt ? (
                  <p className={FIELD_ERROR_CLASS}>{errors.prompt}</p>
                ) : (
                  <span />
                )}
                {/* 字数统计 */}
                <span className="text-[12px] leading-none text-[#858b9c]">
                  {values.prompt.length}/10000
                </span>
              </div>
            </div>

            {/* 内部备注（可选） */}
            <div className="flex flex-col gap-[6px]">
              <Label htmlFor="task-description" className={FIELD_LABEL_CLASS}>
                内部备注
              </Label>
              <Textarea
                id="task-description"
                rows={3}
                placeholder="可选，用于说明这个定时任务的来源和目的"
                value={values.description || ''}
                onChange={(event) => update('description', event.target.value)}
              />
            </div>
          </div>
        </section>

        {/* ===== 唤醒计划区块 ===== */}
        <section className={CARD_CLASS}>
          <h3 className={CARD_TITLE_CLASS}>唤醒计划</h3>
          <div className="flex flex-col gap-[16px]">
            {/* 启用/暂停开关 */}
            <div className="flex items-center justify-between">
              <Label htmlFor="task-status" className={FIELD_LABEL_CLASS}>
                启用状态
              </Label>
              <div className="flex items-center gap-[8px]">
                <Switch
                  id="task-status"
                  checked={values.status !== 'paused'}
                  onCheckedChange={(checked) => update('status', checked ? 'active' : 'paused')}
                />
                <span className="text-[13px] text-[#858b9c]">
                  {values.status !== 'paused' ? '启用' : '暂停'}
                </span>
              </div>
            </div>

            {/* 调度类型选择 */}
            <div className="flex flex-col gap-[6px]">
              <Label className={FIELD_LABEL_CLASS}>调度类型</Label>
              <Select
                value={values.schedule_type}
                onValueChange={(value) =>
                  update('schedule_type', value as TaskFormValues['schedule_type'])
                }
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="daily">每天</SelectItem>
                  <SelectItem value="weekly">每周</SelectItem>
                  <SelectItem value="monthly">每月</SelectItem>
                  <SelectItem value="once">一次性</SelectItem>
                </SelectContent>
              </Select>
            </div>

            {/* 根据调度类型渲染不同的时间选择器 */}
            {scheduleType === 'once' ? (
              // 一次性任务：选择精确的日期时间
              <div className="flex flex-col gap-[6px]">
                <Label htmlFor="task-run-at" className={FIELD_LABEL_CLASS}>
                  执行时间
                </Label>
                <Input
                  id="task-run-at"
                  type="datetime-local"
                  className={cn(errors.run_at && 'border-destructive')}
                  value={values.run_at}
                  onChange={(event) => update('run_at', event.target.value)}
                />
                {errors.run_at && <p className={FIELD_ERROR_CLASS}>{errors.run_at}</p>}
              </div>
            ) : (
              // 周期任务：选择每日时间
              <div className="flex flex-col gap-[6px]">
                <Label htmlFor="task-time" className={FIELD_LABEL_CLASS}>
                  执行时间
                </Label>
                <Input
                  id="task-time"
                  type="time"
                  className={cn(errors.time && 'border-destructive')}
                  value={values.time}
                  onChange={(event) => update('time', event.target.value)}
                />
                {errors.time && <p className={FIELD_ERROR_CLASS}>{errors.time}</p>}
              </div>
            )}

            {/* 每周调度：星期多选 */}
            {scheduleType === 'weekly' && (
              <div className="flex flex-col gap-[8px]">
                <Label className={FIELD_LABEL_CLASS}>执行日期</Label>
                <div className="flex flex-wrap gap-x-[16px] gap-y-[10px]">
                  {WEEKDAY_OPTIONS.map((option) => (
                    <label
                      key={option.value}
                      className="flex cursor-pointer items-center gap-[6px] text-[13px] text-[#18181a]"
                    >
                      <Checkbox
                        checked={values.weekdays.includes(option.value)}
                        onCheckedChange={(checked) =>
                          toggleWeekday(option.value, checked === true)
                        }
                      />
                      {option.label}
                    </label>
                  ))}
                </div>
                {errors.weekdays && <p className={FIELD_ERROR_CLASS}>{errors.weekdays}</p>}
              </div>
            )}

            {/* 每月调度：选择日期号 */}
            {scheduleType === 'monthly' && (
              <div className="flex flex-col gap-[6px]">
                <Label htmlFor="task-day" className={FIELD_LABEL_CLASS}>
                  每月几号
                </Label>
                <Input
                  id="task-day"
                  type="number"
                  min={1}
                  max={31}
                  className="w-[120px]"
                  value={values.day_of_month}
                  onChange={(event) => update('day_of_month', Number(event.target.value) || 1)}
                />
              </div>
            )}

            {/* 最大运行次数（可选） */}
            <div className="flex flex-col gap-[6px]">
              <Label htmlFor="task-max-runs" className={FIELD_LABEL_CLASS}>
                最大运行次数
              </Label>
              <Input
                id="task-max-runs"
                type="number"
                min={1}
                placeholder="不填为无限制"
                value={values.max_runs ?? ''}
                onChange={(event) =>
                  update('max_runs', event.target.value ? Number(event.target.value) : undefined)
                }
              />
            </div>

            {/* 并发策略说明 */}
            <div className="rounded-[12px] border border-[#eef0f4] bg-[#fafbfc] px-[14px] py-[12px] text-[13px] leading-[1.6] text-[#858b9c]">
              默认使用 forbid 并发策略：上一轮未结束时跳过本次唤醒，避免同一员工重复处理同一批任务。
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}

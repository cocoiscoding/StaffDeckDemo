/**
 * @file pages/PersonaPage.tsx
 * @description 数字员工人设（Persona）与 UI 配置管理页面。
 *
 * 该页面管理两部分配置：
 * 1. 人设配置：员工的名称、描述和系统提示词（system_prompt）
 *    - 支持全局人设（overall）和单个员工人设两种模式
 *    - 根据当前选中的员工作用域自动加载对应的人设数据
 * 2. UI 配置：对话界面的展示开关和运行参数
 *    - 思维链/技能/工具 Trace 的显隐
 *    - 反思轮数、Agent Loop 最大动作数等运行参数
 *
 * 人设数据通过 /api/enterprise/persona 接口交互，
 * UI 配置通过 /api/enterprise/ui-config 接口交互。
 */

import { SaveOutlined, UserOutlined } from '../icons';
import { useEffect, useState, type ReactNode } from 'react';
import {
  Button as UIButton,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  Switch,
  Textarea,
  notify,
} from '@/components/ui';
import { api, TENANT_ID } from '../api/client';
import type { AgentProfileRead, PersonaRead, UIConfigRead } from '../types';

/** localStorage 键名：当前选中的数字员工作用域 */
const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';

/** 人设表单数据结构 */
type PersonaForm = {
  agent_name: string;
  agent_description: string;
  system_prompt: string;
};

/** UI 配置表单数据结构 */
type UiConfigForm = {
  show_thinking_trace: boolean;
  show_skill_trace: boolean;
  show_tool_trace: boolean;
  reflection_max_rounds: string;
  agent_loop_max_actions: string;
};

/** 人设表单空白初始值 */
const BLANK_PERSONA: PersonaForm = { agent_name: '', agent_description: '', system_prompt: '' };
/** UI 配置默认值 */
const DEFAULT_UI_CONFIG: UiConfigForm = {
  show_thinking_trace: true,
  show_skill_trace: true,
  show_tool_trace: true,
  reflection_max_rounds: '1',
  agent_loop_max_actions: '6',
};

/**
 * 将 ISO 时间字符串格式化为仅日期（yyyy-MM-dd）。
 * 自动补全时区后缀以保证正确解析。
 * @param value ISO 时间字符串
 * @returns 日期部分字符串，解析失败时截取前 10 位
 */
function formatDateOnly(value: string): string {
  // 补全时区后缀（Z 或 +HH:MM），避免本地时区偏移
  const normalized = /(?:z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) {
    return value.slice(0, 10);
  }
  return date.toISOString().slice(0, 10);
}

/**
 * 人设管理页面主组件。
 *
 * 管理人设表单和 UI 配置表单两块数据。根据当前选中的员工
 * 自动加载对应人设（全局或员工级），支持监听员工作用域切换。
 * @returns 渲染好的人设管理页面
 */
export default function PersonaPage() {
  // 人设表单当前值
  const [form, setForm] = useState<PersonaForm>(BLANK_PERSONA);
  // UI 配置表单当前值
  const [uiForm, setUiForm] = useState<UiConfigForm>(DEFAULT_UI_CONFIG);
  // 人设保存中状态
  const [loading, setLoading] = useState(false);
  // UI 配置保存中状态
  const [uiLoading, setUiLoading] = useState(false);
  // 人设最后更新时间
  const [updatedAt, setUpdatedAt] = useState('');
  // UI 配置最后更新时间
  const [uiUpdatedAt, setUiUpdatedAt] = useState('');
  // 全部员工列表（用于作用域选择）
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  // 当前选中的员工 ID（从 localStorage 初始化）
  const [selectedAgentId, setSelectedAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
  // 当前选中的员工对象
  const selectedAgent = agents.find((agent) => agent.id === selectedAgentId) || null;
  // 是否为全局人设模式（未选员工或选了 is_overall 的员工）
  const isOverallPersona = !selectedAgent || selectedAgent.is_overall;

  /** 局部更新人设表单字段 */
  const updatePersona = (patch: Partial<PersonaForm>) => setForm((prev) => ({ ...prev, ...patch }));
  /** 局部更新 UI 配置表单字段 */
  const updateUiConfig = (patch: Partial<UiConfigForm>) => setUiForm((prev) => ({ ...prev, ...patch }));

  useEffect(() => {
    void loadPersonaScope();
    api
      .get<UIConfigRead>(`/api/enterprise/ui-config?tenant_id=${TENANT_ID}`)
      .then((row) => {
        setUiForm({
          show_thinking_trace: row.show_thinking_trace,
          show_skill_trace: row.show_skill_trace,
          show_tool_trace: row.show_tool_trace,
          reflection_max_rounds: String(row.reflection_max_rounds),
          agent_loop_max_actions: String(row.agent_loop_max_actions),
        });
        setUiUpdatedAt(row.updated_at);
      })
      .catch((error) => notify.error(error.message));
  }, []);

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      const agentId = (event as CustomEvent<{ agentId?: string }>).detail?.agentId || '';
      if (agentId) setSelectedAgentId(agentId);
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

  useEffect(() => {
    const agent = agents.find((item) => item.id === selectedAgentId);
    if (agent) {
      if (agent.is_overall) {
        api
          .get<PersonaRead>(`/api/enterprise/persona?tenant_id=${TENANT_ID}`)
          .then((row) => {
            setForm({
              agent_name: agent.name,
              agent_description: agent.description || '',
              system_prompt: agent.persona_prompt || row.system_prompt,
            });
            setUpdatedAt(agent.updated_at || row.updated_at);
          })
          .catch((error) => notify.error(error.message));
        return;
      }
      setForm({
        agent_name: agent.name,
        agent_description: agent.description || '',
        system_prompt: agent.persona_prompt || '',
      });
      setUpdatedAt(agent.updated_at);
      return;
    }
    api
      .get<PersonaRead>(`/api/enterprise/persona?tenant_id=${TENANT_ID}`)
      .then((row) => {
        setForm((prev) => ({ ...prev, system_prompt: row.system_prompt }));
        setUpdatedAt(row.updated_at);
      })
      .catch((error) => notify.error(error.message));
  }, [agents, selectedAgentId]);

  /**
   * 加载员工作用域列表。
   * 获取全部员工后，校验当前选中的作用域是否有效；
   * 无效时自动回退到全局员工或第一个员工。
   */
  async function loadPersonaScope() {
    try {
      const rows = await api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`);
      setAgents(rows);
      setSelectedAgentId((current) => {
        const stored = window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY);
        const candidate = current || stored || '';
        if (candidate && rows.some((agent) => agent.id === candidate)) return candidate;
        return rows.find((agent) => agent.is_overall)?.id || rows[0]?.id || '';
      });
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载员工域失败');
    }
  }

  async function save() {
    if (!form.system_prompt.trim() || (selectedAgent && !form.agent_name.trim())) {
      notify.error('请填写必填项');
      return;
    }
    setLoading(true);
    try {
      if (selectedAgent) {
        const row = await api.put<AgentProfileRead>(`/api/enterprise/agents/${selectedAgent.id}`, {
          tenant_id: TENANT_ID,
          name: form.agent_name,
          description: form.agent_description,
          persona_prompt: form.system_prompt,
          status: selectedAgent.status,
        });
        setAgents((prev) => prev.map((item) => (item.id === row.id ? { ...row, resources: item.resources } : item)));
        setUpdatedAt(row.updated_at);
        if (row.is_overall) {
          await api.put<PersonaRead>('/api/enterprise/persona', {
            tenant_id: TENANT_ID,
            system_prompt: form.system_prompt,
          });
        }
        window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: row.id } }));
        notify.success('岗位人设已保存');
      } else {
        const row = await api.put<PersonaRead>('/api/enterprise/persona', {
          tenant_id: TENANT_ID,
          system_prompt: form.system_prompt,
        });
        setUpdatedAt(row.updated_at);
        notify.success('组织默认岗位人设已保存');
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存失败');
    } finally {
      setLoading(false);
    }
  }

  async function saveUiConfig() {
    const reflectionMaxRounds = Number(uiForm.reflection_max_rounds);
    const agentLoopMaxActions = Number(uiForm.agent_loop_max_actions);
    if (Number.isNaN(reflectionMaxRounds) || Number.isNaN(agentLoopMaxActions)) {
      notify.error('反思轮数与单轮最大动作数必须是数字');
      return;
    }
    setUiLoading(true);
    try {
      const row = await api.put<UIConfigRead>('/api/enterprise/ui-config', {
        tenant_id: TENANT_ID,
        show_thinking_trace: uiForm.show_thinking_trace,
        show_skill_trace: uiForm.show_skill_trace,
        show_tool_trace: uiForm.show_tool_trace,
        reflection_max_rounds: reflectionMaxRounds,
        agent_loop_max_actions: agentLoopMaxActions,
      });
      setUiUpdatedAt(row.updated_at);
      notify.success('展示设置已保存');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存失败');
    } finally {
      setUiLoading(false);
    }
  }

  return (
    <>
      <div className="page-title">
        <div>
          <h3>岗位人设</h3>
        </div>
        <UIButton disabled={loading} onClick={() => void save()}>
          <SaveOutlined />
          保存
        </UIButton>
      </div>
      <Card className="editor-card">
        <CardHeader>
          <CardTitle className="flex items-center gap-[6px]"><UserOutlined /> 岗位人设</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-[14px]">
          <LabeledField label="名称">
            <Input value={form.agent_name} placeholder="数字员工姓名" onChange={(event) => updatePersona({ agent_name: event.target.value })} />
          </LabeledField>
          <LabeledField label="描述">
            <Textarea rows={2} value={form.agent_description} placeholder="员工岗位描述" onChange={(event) => updatePersona({ agent_description: event.target.value })} />
          </LabeledField>
          <LabeledField label="岗位 Prompt">
            <Textarea
              className="persona-editor"
              rows={12}
              value={form.system_prompt}
              placeholder={isOverallPersona ? '输入组织默认岗位人设' : '输入仅当前员工可见的岗位人设'}
              onChange={(event) => updatePersona({ system_prompt: event.target.value })}
            />
          </LabeledField>
          {updatedAt && <span className="text-[12px] text-muted-foreground">最后更新：{formatDateOnly(updatedAt)}</span>}
        </CardContent>
      </Card>
      <Card className="editor-card settings-card">
        <CardHeader>
          <CardTitle>执行记录与展示设置</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-[16px]">
          <SwitchRow label="展示思考状态" checked={uiForm.show_thinking_trace} onChange={(next) => updateUiConfig({ show_thinking_trace: next })} />
          <SwitchRow label="展示执行技能" checked={uiForm.show_skill_trace} onChange={(next) => updateUiConfig({ show_skill_trace: next })} />
          <SwitchRow label="展示工具调用" checked={uiForm.show_tool_trace} onChange={(next) => updateUiConfig({ show_tool_trace: next })} />
          <LabeledField label="反思轮数" hint="设为 0 时关闭反思；每轮允许模型检查当前技能和工具结果，并决定是否重试其他技能或工具。">
            <Input
              type="number"
              min={0}
              max={5}
              step={1}
              value={uiForm.reflection_max_rounds}
              onChange={(event) => updateUiConfig({ reflection_max_rounds: event.target.value })}
            />
          </LabeledField>
          <LabeledField label="单轮最大动作数" hint="控制一次用户输入内员工可连续决策和调用工具的最大次数，用于避免无限循环。">
            <Input
              type="number"
              min={1}
              max={20}
              step={1}
              value={uiForm.agent_loop_max_actions}
              onChange={(event) => updateUiConfig({ agent_loop_max_actions: event.target.value })}
            />
          </LabeledField>
          <UIButton className="self-start" disabled={uiLoading} onClick={() => void saveUiConfig()}>
            <SaveOutlined />
            保存设置
          </UIButton>
          {uiUpdatedAt && <span className="text-[12px] text-muted-foreground">最后更新：{formatDateOnly(uiUpdatedAt)}</span>}
        </CardContent>
      </Card>
    </>
  );
}

function LabeledField({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-[6px]">
      <span className="text-[12px] font-medium text-[#464c5e]">{label}</span>
      {hint && <span className="text-[11px] leading-[16px] text-muted-foreground">{hint}</span>}
      {children}
    </label>
  );
}

/**
 * 带标签的开关行组件，用于展示开关控件及其标签。
 * @param label - 开关标签文案
 * @param checked - 当前开关状态
 * @param onChange - 开关状态变更回调
 * @returns 开关行 JSX
 */
function SwitchRow({ label, checked, onChange }: { label: string; checked: boolean; onChange: (next: boolean) => void }) {
  return (
    <label className="flex items-center justify-between gap-[16px]">
      <span className="text-[12px] font-medium text-[#464c5e]">{label}</span>
      <Switch checked={checked} onCheckedChange={onChange} />
    </label>
  );
}

/**
 * @file 数字员工工具函数模块。
 *
 * 提供数字员工（Agent）相关的类型定义、预设配置、权限判定和元数据解析等工具函数。
 * 是前端管理数字员工的核心业务逻辑层。
 *
 * 核心概念：
 * - **数字员工（Employee / Agent）**：系统中的 AI 员工实体，有角色（研发/行政/财务等）、
 *   头像、工作风格、专业标签等属性。
 * - **员工模板（Template）**：预置的岗位模板，创建员工时可快速套用。
 * - **头像预设（Avatar Preset）**：预置的岗位头像，不同岗位对应不同的插画和色调。
 * - **权限体系**：区分管理员（admin）和普通成员（member），以及广场员工、自有员工、
 *   默认员工等不同可见性规则。
 * - **元数据（Metadata）**：员工的扩展属性存储在 `metadata` 字段中，包括角色键、头像、
 *   所有者、创建者等信息。
 */

import type { AgentProfileRead, AgentResourceBindingRead, AgentResourceType } from './types';
import {
  isEmployeeOwnedBy,
  isEnterpriseAdmin,
  isGalleryEmployee,
  type EnterpriseAuthUser,
} from './auth';

// 各岗位预设头像图片资源
import avatarAfterSales from './assets/staffdeck/staffdeck-avatar-after-sales.png';
import avatarCommerce from './assets/staffdeck/staffdeck-avatar-commerce.png';
import avatarDefault from './assets/staffdeck/staffdeck-avatar-default.png';
import avatarKnowledge from './assets/staffdeck/staffdeck-avatar-knowledge.png';
import avatarOps from './assets/staffdeck/staffdeck-avatar-ops.png';
import avatarOverall from './assets/staffdeck/staffdeck-avatar-overall.png';
import avatarQuality from './assets/staffdeck/staffdeck-avatar-quality.png';
import avatarService from './assets/staffdeck/staffdeck-avatar-service.png';

/**
 * 数字员工档案信息。
 * 从 Agent metadata 中解析出的结构化展示数据。
 */
export type EmployeeProfile = {
  /** 角色键（对应模板 key） */
  roleKey: string;
  /** 角色名称 */
  roleName: string;
  /** 头像文字（用于文字头像展示） */
  avatarText: string;
  /** 头像色调 */
  avatarTone: string;
  /** 头像类型：预设或上传 */
  avatarKind: 'preset' | 'upload';
  /** 头像预设 key */
  avatarPreset: string;
  /** 头像图片 URL（上传时使用） */
  avatarImage: string;
  /** 入职日期 */
  onboardedAt: string;
  /** 工作风格列表 */
  workStyles: string[];
  /** 专业标签列表 */
  expertiseTags: string[];
  /** 工作模式列表 */
  workModes: string[];
};

/**
 * 头像预设配置。
 */
export type EmployeeAvatarPreset = {
  /** 预设唯一 key */
  key: string;
  /** 预设标签 */
  label: string;
  /** 文字头像字符 */
  text: string;
  /** 色调 */
  tone: string;
};

/**
 * 数字员工模板。
 * 预置的岗位配置模板，创建员工时可直接套用。
 */
export type EmployeeTemplate = {
  /** 模板唯一 key */
  key: string;
  /** 角色名称 */
  roleName: string;
  /** 文字头像字符 */
  avatarText: string;
  /** 头像色调 */
  avatarTone: string;
  /** 关联的头像预设 key */
  avatarPreset: string;
  /** 岗位描述 */
  description: string;
  /** 工作风格列表 */
  workStyles: string[];
  /** 专业标签列表 */
  expertiseTags: string[];
  /** 工作模式列表 */
  workModes: string[];
};

/** 数字员工的简化类型（用于权限判定等场景，减少类型耦合） */
type EmployeeAgentLike = {
  id?: string;
  name?: string;
  /** 是否为开放广场（公共默认员工） */
  is_overall?: boolean;
  metadata?: Record<string, unknown>;
};

/**
 * 头像预设列表。每个预设对应一个岗位类别，含文字头像和色调。
 */
export const EMPLOYEE_AVATAR_PRESETS: EmployeeAvatarPreset[] = [
  { key: 'service-orbit', label: '研发员工', text: '研', tone: 'teal' },
  { key: 'after-sales-seal', label: '行政员工', text: '行', tone: 'copper' },
  { key: 'knowledge-node', label: '知识运营员工', text: '知', tone: 'olive' },
  { key: 'commerce-compass', label: '财务员工', text: '财', tone: 'blue' },
  { key: 'ops-grid', label: '人事员工', text: '人', tone: 'ink' },
  { key: 'quality-star', label: '法务员工', text: '法', tone: 'gold' },
];

/** 默认头像预设 key */
export const DEFAULT_AVATAR_PRESET = 'service-orbit';

/** 预设头像 key 到图片资源的映射 */
const PRESET_AVATAR_IMAGES: Record<string, string> = {
  'service-orbit': avatarService,
  'after-sales-seal': avatarAfterSales,
  'knowledge-node': avatarKnowledge,
  'commerce-compass': avatarCommerce,
  'ops-grid': avatarOps,
  'quality-star': avatarQuality,
  overall: avatarOverall,
};

/** 头像来源所需的最小字段集 */
type AvatarSource = Pick<EmployeeProfile, 'avatarKind' | 'avatarImage' | 'avatarPreset'>;

/**
 * 判断员工头像是否为用户上传的自定义图片。
 *
 * @param profile 头像来源信息
 * @returns 头像类型为 upload 且有图片 URL 时返回 `true`
 */
export function isUploadedAvatar(profile: AvatarSource): boolean {
  return profile.avatarKind === 'upload' && Boolean(profile.avatarImage);
}

/**
 * 解析员工头像图片 URL（上传图片或预设插画）。
 *
 * @param profile 头像来源信息
 * @returns 头像图片 URL；上传图片优先，否则返回预设插画，最终回退到默认头像
 */
export function employeeAvatarImage(profile: AvatarSource): string {
  if (isUploadedAvatar(profile)) return profile.avatarImage;
  return PRESET_AVATAR_IMAGES[profile.avatarPreset || DEFAULT_AVATAR_PRESET] || avatarDefault;
}

/**
 * 数字员工模板列表。
 * 每个模板预置了一个岗位的完整配置（角色、头像、工作风格、专业标签等）。
 */
export const EMPLOYEE_TEMPLATES: EmployeeTemplate[] = [
  {
    key: 'service-specialist',
    roleName: '研发',
    avatarText: '研',
    avatarTone: 'teal',
    avatarPreset: 'service-orbit',
    description: '负责研发资料查询、代码任务拆解、SOP 执行和交付记录沉淀。',
    workStyles: ['目标明确', '证据优先', '动作可追溯'],
    expertiseTags: ['研发协作', '代码检索', 'SOP 执行'],
    workModes: ['理解需求', '检索资料', '推进执行'],
  },
  {
    key: 'after-sales',
    roleName: '行政',
    avatarText: '行',
    avatarTone: 'copper',
    avatarPreset: 'after-sales-seal',
    description: '负责会议纪要、资料归档、跨部门事务跟进和结果同步。',
    workStyles: ['流程推进', '及时追问', '留痕复盘'],
    expertiseTags: ['资料归档', '会议纪要', '事务跟进'],
    workModes: ['确认事项', '拆解步骤', '同步结果'],
  },
  {
    key: 'knowledge-operator',
    roleName: '知识运营',
    avatarText: '知',
    avatarTone: 'olive',
    avatarPreset: 'knowledge-node',
    description: '负责知识库检索、资料结构化归档、信息核对和答案沉淀。',
    workStyles: ['证据优先', '结构清晰', '持续沉淀'],
    expertiseTags: ['知识检索', '资料归档', '信息结构化'],
    workModes: ['查资料', '做归档', '给答案'],
  },
  {
    key: 'commerce-guide',
    roleName: '财务',
    avatarText: '财',
    avatarTone: 'blue',
    avatarPreset: 'commerce-compass',
    description: '负责报销核对、预算口径、财务资料检索和风险提示。',
    workStyles: ['证据优先', '口径统一', '风险克制'],
    expertiseTags: ['报销核对', '预算口径', '数据复盘'],
    workModes: ['查规则', '核凭证', '给结论'],
  },
];

/**
 * 直接返回传入的文本值（占位函数，预留国际化扩展点）。
 *
 * @param value 原始文本
 * @returns 原始文本
 */
export function staffdeckDisplayText(value: string): string {
  return value;
}

/**
 * 判断是否为默认数字员工（非开放广场，且 metadata 中标记为默认员工）。
 *
 * @param agent 数字员工对象
 * @returns 是默认员工时返回 `true`
 */
export function isDefaultEmployeeAgent(agent?: EmployeeAgentLike | null): boolean {
  if (!agent || agent.is_overall) return false;
  const metadata = agent.metadata || {};
  return metadata.is_default_employee === true;
}

/**
 * 从员工列表中选择优先推荐的员工。
 *
 * 选择优先级：默认员工 > 第一个非开放广场员工。
 *
 * @param agents 员工列表
 * @returns 优先推荐的员工，无匹配时返回 `undefined`
 */
export function preferredEmployeeAgent<T extends EmployeeAgentLike>(agents: T[]): T | undefined {
  return agents.find(isDefaultEmployeeAgent) || agents.find((item) => !item.is_overall);
}

/**
 * 员工可见性筛选选项。
 */
export type EmployeeVisibilityOptions = {
  /** 仅保留活跃状态的员工 */
  activeOnly?: boolean;
  /** 排除指定 ID 的员工 */
  excludeAgentId?: string;
  /** 是否包含默认员工 */
  includeDefault?: boolean;
  /** 是否包含开放广场（仅管理员有效） */
  includeOverall?: boolean;
};

/**
 * 判断当前用户是否可以访问（看到）指定数字员工。
 *
 * 可见性规则：
 * - 管理员：可见所有员工（可选包含开放广场）；
 * - 普通成员：不可见开放广场；可见默认员工、自己创建的员工、广场发布的员工。
 *
 * @param agent   数字员工
 * @param user    当前用户
 * @param options 筛选选项
 * @returns 可见时返回 `true`
 */
export function canAccessEmployeeAgent(
  agent: AgentProfileRead,
  user?: EnterpriseAuthUser | null,
  options: EmployeeVisibilityOptions = {},
): boolean {
  if (options.excludeAgentId && agent.id === options.excludeAgentId) return false;
  if (options.activeOnly && agent.status !== 'active') return false;

  const includeOverall = options.includeOverall ?? false;
  // 管理员可见所有员工
  if (isEnterpriseAdmin(user)) return includeOverall || !agent.is_overall;
  // 普通成员不可见开放广场
  if (agent.is_overall) return false;

  const includeDefault = options.includeDefault ?? false;
  return (
    (includeDefault && isDefaultEmployeeAgent(agent))
    || isEmployeeOwnedBy(agent, user)
    || isGalleryEmployee(agent)
  );
}

/**
 * 判断数字员工是否已被当前用户使用过（在对话中使用过）。
 *
 * @param agent 数字员工
 * @returns metadata 中标记了 `used_by_current_user` 或 `chat_used_by_current_user` 时返回 `true`
 */
export function isEmployeeUsedByCurrentUser(agent: AgentProfileRead): boolean {
  const metadata = agent.metadata || {};
  return metadata.used_by_current_user === true || metadata.chat_used_by_current_user === true;
}

/**
 * 判断当前用户是否可以选择指定数字员工作为当前操作对象。
 *
 * 比 `canAccessEmployeeAgent` 更严格：对于广场员工，普通用户只能选择自己使用过的。
 *
 * @param agent   数字员工
 * @param user    当前用户
 * @param options 筛选选项
 * @returns 可选择时返回 `true`
 */
export function canSelectCurrentEmployeeAgent(
  agent: AgentProfileRead,
  user?: EnterpriseAuthUser | null,
  options: EmployeeVisibilityOptions = {},
): boolean {
  if (options.excludeAgentId && agent.id === options.excludeAgentId) return false;
  if (options.activeOnly && agent.status !== 'active') return false;

  const includeOverall = options.includeOverall ?? false;
  if (isEnterpriseAdmin(user)) {
    // 管理员：开放广场受 includeOverall 控制
    if (agent.is_overall) return includeOverall;
    // 管理员对广场员工：仅当已被自己使用过时可选
    if (isGalleryEmployee(agent) && !isEmployeeOwnedBy(agent, user)) {
      return isEmployeeUsedByCurrentUser(agent);
    }
    return true;
  }
  if (agent.is_overall) return false;

  const includeDefault = options.includeDefault ?? false;
  return (
    (includeDefault && isDefaultEmployeeAgent(agent))
    || isEmployeeOwnedBy(agent, user)
    || (isGalleryEmployee(agent) && isEmployeeUsedByCurrentUser(agent))
  );
}

/**
 * 判断当前用户是否可以管理（编辑/删除）指定数字员工。
 *
 * 管理权限规则：
 * - 开放广场：仅管理员可管理；
 * - 其他员工：管理员或创建者可管理。
 *
 * @param agent 数字员工
 * @param user  当前用户
 * @returns 可管理时返回 `true`
 */
export function canManageEmployeeAgent(
  agent: AgentProfileRead,
  user?: EnterpriseAuthUser | null,
): boolean {
  if (agent.is_overall) return isEnterpriseAdmin(user);
  return isEnterpriseAdmin(user) || isEmployeeOwnedBy(agent, user);
}

/**
 * 判断数字员工是否为当前用户创建的（非开放广场）。
 *
 * @param agent 数字员工
 * @param user  当前用户
 * @returns 是当前用户创建的非开放广场员工时返回 `true`
 */
export function isMyEmployeeAgent(
  agent: AgentProfileRead,
  user?: EnterpriseAuthUser | null,
): boolean {
  return !agent.is_overall && isEmployeeOwnedBy(agent, user);
}

/**
 * 筛选出当前用户可见的数字员工列表。
 *
 * @param rows    全部员工列表
 * @param user    当前用户
 * @param options 筛选选项
 * @returns 可见员工列表
 */
export function visibleEmployeeAgents(
  rows: AgentProfileRead[],
  user?: EnterpriseAuthUser | null,
  options: EmployeeVisibilityOptions = {},
): AgentProfileRead[] {
  return rows.filter((agent) => canAccessEmployeeAgent(agent, user, options));
}

/**
 * 筛选出当前用户可选择使用的数字员工列表。
 *
 * @param rows    全部员工列表
 * @param user    当前用户
 * @param options 筛选选项
 * @returns 可选择员工列表
 */
export function currentEmployeeAgents(
  rows: AgentProfileRead[],
  user?: EnterpriseAuthUser | null,
  options: EmployeeVisibilityOptions = {},
): AgentProfileRead[] {
  return rows.filter((agent) => canSelectCurrentEmployeeAgent(agent, user, options));
}

/**
 * 从员工列表中查找开放广场（公共默认员工）。
 *
 * @param rows 员工列表
 * @returns 开放广场员工，不存在时返回 `undefined`
 */
export function openGalleryAgent(rows: AgentProfileRead[]): AgentProfileRead | undefined {
  return rows.find((agent) => agent.is_overall);
}

/**
 * 获取开放广场员工的 ID。
 *
 * @param rows 员工列表
 * @returns 开放广场员工 ID，不存在时返回空串
 */
export function openGalleryAgentId(rows: AgentProfileRead[]): string {
  return openGalleryAgent(rows)?.id || '';
}

/**
 * 构造广场导入来源选项列表。
 * 用于"从广场复制"创建员工时的来源选择器。
 *
 * @param rows   员工列表
 * @param label  选项标签
 * @returns 包含广场员工 ID 的选项数组，无广场员工时返回空数组
 */
export function openGalleryImportSourceOptions(
  rows: AgentProfileRead[],
  label: string,
): Array<{ value: string; label: string }> {
  const agentId = openGalleryAgentId(rows);
  return agentId ? [{ value: agentId, label }] : [];
}

/**
 * 安全地将 unknown 值转换为字符串数组。
 *
 * @param value 原始值
 * @returns 过滤掉空值后的字符串数组；非数组时返回空数组
 */
function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String).filter(Boolean) : [];
}

/**
 * 从 metadata 中安全读取字符串值。
 *
 * @param metadata 元数据对象
 * @param key      字段键
 * @returns 字符串值；不存在或非字符串时返回空串
 */
function stringFromMeta(metadata: Record<string, unknown>, key: string): string {
  const value = metadata[key];
  return typeof value === 'string' ? value : '';
}

/**
 * 从多个候选值中返回第一个非空字符串。
 *
 * @param values 候选值列表
 * @returns 第一个非空 trim 后的字符串；全部为空时返回空串
 */
function firstString(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

/**
 * 从 metadata 中提取创建者名称。
 *
 * 依次尝试多个可能的字段（creator_name、created_by、owner_display_name 等），
 * 返回第一个有效值。
 *
 * @param metadata 元数据对象
 * @param fallback 所有字段都无效时的回退值
 * @returns 创建者名称
 */
export function creatorNameFromMetadata(
  metadata?: Record<string, unknown> | null,
  fallback = '',
): string {
  const meta = metadata || {};
  // 按优先级依次尝试各字段
  const creator = firstString(
    meta.creator_name,
    meta.created_by,
    meta.created_by_display_name,
    meta.created_by_username,
    meta.owner_display_name,
    meta.owner_username,
    meta.gallery_published_by,
    meta.created_by_user_id,
    meta.owner_user_id,
  );
  if (!creator) return fallback;
  const normalized = creator.trim();
  return normalized || fallback;
}

/**
 * 将员工名称与创建者名称组合为展示文本。
 * 格式为 `员工名 @创建者`，创建者为空时仅返回员工名。
 *
 * @param name    员工名称
 * @param creator 创建者名称
 * @returns 组合后的展示文本
 */
export function displayNameWithCreator(name: string, creator?: string): string {
  const cleanName = name.trim() || '未命名';
  const cleanCreator = (creator || '').trim();
  if (!cleanCreator) return cleanName;
  return `${cleanName} @${cleanCreator}`;
}

/**
 * 从数字员工的 metadata 中解析出结构化的员工档案。
 *
 * 解析逻辑：
 * - 根据 metadata 中的 `role_key` 匹配预置模板；
 * - 根据头像配置确定头像类型（上传/预设）和展示样式；
 * - 开放广场员工使用固定的"开放广场"展示样式；
 * - 所有字段均有回退默认值，确保输出完整。
 *
 * @param agent 数字员工对象
 * @returns 结构化的员工档案信息
 */
export function employeeProfile(agent?: AgentProfileRead | null): EmployeeProfile {
  const metadata = agent?.metadata || {};
  // 尝试匹配预置模板
  const template = EMPLOYEE_TEMPLATES.find((item) => item.key === metadata.role_key);
  // 尝试匹配头像预设，回退到模板预设，最终回退到第一个预设
  const preset = EMPLOYEE_AVATAR_PRESETS.find((item) => item.key === metadata.avatar_preset)
    || (template ? EMPLOYEE_AVATAR_PRESETS.find((item) => item.key === template.avatarPreset) : undefined)
    || EMPLOYEE_AVATAR_PRESETS[0];
  const isOverall = Boolean(agent?.is_overall);
  // 判定头像类型：仅当明确为 upload 且有图片 URL 时才用上传头像
  const avatarKind = stringFromMeta(metadata, 'avatar_kind') === 'upload' && stringFromMeta(metadata, 'avatar_image')
    ? 'upload'
    : 'preset';
  return {
    roleKey: stringFromMeta(metadata, 'role_key') || template?.key || '',
    // 开放广场使用固定的角色名
    roleName: isOverall ? '开放广场' : stringFromMeta(metadata, 'role_name') || template?.roleName || '待补充岗位',
    avatarText: isOverall ? '广' : stringFromMeta(metadata, 'avatar_text') || preset.text || template?.avatarText || '员',
    avatarTone: isOverall ? 'overall' : stringFromMeta(metadata, 'avatar_tone') || preset.tone || template?.avatarTone || 'teal',
    avatarKind: isOverall ? 'preset' : avatarKind,
    avatarPreset: isOverall ? 'overall' : stringFromMeta(metadata, 'avatar_preset') || preset.key,
    avatarImage: isOverall ? '' : stringFromMeta(metadata, 'avatar_image'),
    // 入职日期回退到创建日期
    onboardedAt: stringFromMeta(metadata, 'onboarded_at') || agent?.created_at?.slice(0, 10) || '-',
    workStyles: asStringArray(metadata.work_styles),
    expertiseTags: asStringArray(metadata.expertise_tags),
    workModes: asStringArray(metadata.work_modes),
  };
}

/**
 * 获取数字员工的展示名称。
 * 开放广场返回固定名称，其他返回 agent 名称。
 *
 * @param agent 数字员工对象
 * @returns 展示名称
 */
export function employeeDisplayName(agent?: AgentProfileRead | null): string {
  if (!agent) return '数字员工';
  if (agent.is_overall) return '开放广场';
  return agent.name || '数字员工';
}

/**
 * 获取数字员工的创建者名称。
 *
 * @param agent 数字员工对象
 * @returns 创建者名称
 */
export function employeeCreatorName(agent?: AgentProfileRead | null): string {
  return creatorNameFromMetadata(agent?.metadata);
}

/**
 * 获取数字员工的展示名称（含创建者后缀）。
 *
 * @param agent 数字员工对象
 * @returns 形如 `员工名 @创建者` 的展示文本
 */
export function employeeDisplayNameWithCreator(agent?: AgentProfileRead | null): string {
  return displayNameWithCreator(employeeDisplayName(agent), employeeCreatorName(agent));
}

/**
 * 从资源的 metadata 中提取创建者名称。
 *
 * @param resource 资源对象
 * @returns 创建者名称
 */
export function resourceCreatorName(resource?: { metadata?: Record<string, unknown> } | null): string {
  return creatorNameFromMetadata(resource?.metadata);
}

/**
 * 获取资源名称（含创建者后缀）的展示文本。
 *
 * @param name     资源名称
 * @param resource 资源对象
 * @returns 形如 `资源名 @创建者` 的展示文本
 */
export function resourceDisplayNameWithCreator(
  name: string,
  resource?: { metadata?: Record<string, unknown> } | null,
): string {
  return displayNameWithCreator(name, resourceCreatorName(resource));
}

/**
 * 统计指定类型的有效资源数量（排除已删除和已停用的）。
 *
 * @param resources 资源绑定列表
 * @param type      资源类型
 * @returns 有效资源数量
 */
export function resourceCount(resources: AgentResourceBindingRead[] | undefined, type: AgentResourceBindingRead['resource_type']): number {
  return (resources || []).filter((item) => (
    item.resource_type === type
    && item.status !== 'deleted'
    && item.status !== 'inactive'
  )).length;
}

/**
 * 获取聊天侧边栏中可选的数字员工列表：活跃且当前用户可见的员工。
 *
 * @param rows 全部员工列表
 * @param user 当前用户
 * @returns 可选员工列表
 */
export function visibleChatEmployees(
  rows: AgentProfileRead[],
  user?: EnterpriseAuthUser | null,
): AgentProfileRead[] {
  return currentEmployeeAgents(rows, user, { activeOnly: true });
}

/**
 * 统计数字员工绑定的指定类型资源的有效数量。
 *
 * @param agent        数字员工
 * @param resourceType 资源类型
 * @returns 有效资源数量
 */
export function agentResourceCount(agent: AgentProfileRead, resourceType: AgentResourceType): number {
  return (agent.resources || []).filter((resource) => (
    resource.resource_type === resourceType
    && resource.status !== 'deleted'
    && resource.status !== 'inactive'
  )).length;
}

/**
 * 统计活跃状态（status === 'active'）的资源数量。
 *
 * @param resources 资源绑定列表
 * @returns 活跃资源数量
 */
export function activeResourceCount(resources: AgentResourceBindingRead[] | undefined): number {
  return (resources || []).filter((item) => item.status === 'active').length;
}

/**
 * 根据模板 key 生成数字员工的 metadata。
 *
 * 在现有 metadata 基础上叠加模板的预设值（角色、头像、工作风格等）。
 *
 * @param templateKey     模板 key
 * @param currentMetadata 当前已有的 metadata（作为基底）
 * @returns 合并后的完整 metadata
 */
export function employeeMetadataFromTemplate(templateKey: string, currentMetadata: Record<string, unknown> = {}): Record<string, unknown> {
  const template = EMPLOYEE_TEMPLATES.find((item) => item.key === templateKey) || EMPLOYEE_TEMPLATES[0];
  return {
    ...currentMetadata,
    role_key: template.key,
    role_name: template.roleName,
    avatar_text: template.avatarText,
    avatar_tone: template.avatarTone,
    avatar_kind: 'preset',
    avatar_preset: template.avatarPreset,
    // 入职日期：保留已有值或使用当天
    onboarded_at: currentMetadata.onboarded_at || new Date().toISOString().slice(0, 10),
    work_styles: template.workStyles,
    expertise_tags: template.expertiseTags,
    work_modes: template.workModes,
  };
}

/**
 * 生成空白入职的数字员工 metadata。
 *
 * 保留现有 metadata 中已有的值，标记 `blank_onboarding: true`，
 * 各字段使用已有值或合理默认值。
 *
 * @param currentMetadata 当前已有的 metadata（作为基底）
 * @returns 空白入职的 metadata
 */
export function employeeBlankMetadata(currentMetadata: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    ...currentMetadata,
    blank_onboarding: true,
    role_key: stringFromMeta(currentMetadata, 'role_key'),
    role_name: stringFromMeta(currentMetadata, 'role_name') || '待补充职位',
    avatar_text: stringFromMeta(currentMetadata, 'avatar_text') || '员',
    avatar_tone: stringFromMeta(currentMetadata, 'avatar_tone') || 'teal',
    avatar_kind: stringFromMeta(currentMetadata, 'avatar_kind') || 'preset',
    avatar_preset: stringFromMeta(currentMetadata, 'avatar_preset') || EMPLOYEE_AVATAR_PRESETS[0].key,
    onboarded_at: currentMetadata.onboarded_at || new Date().toISOString().slice(0, 10),
    work_styles: asStringArray(currentMetadata.work_styles),
    expertise_tags: asStringArray(currentMetadata.expertise_tags),
    work_modes: asStringArray(currentMetadata.work_modes),
  };
}

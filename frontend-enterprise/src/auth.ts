/**
 * @file 前端会话管理模块。
 *
 * 负责企业版用户的鉴权会话在浏览器 localStorage 中的持久化与读取。
 *
 * 核心概念：
 * - **EnterpriseAuthSession**：会话对象，包含 JWT token 和当前登录用户信息。
 * - **会话生命周期**：登录成功后调用 `setEnterpriseAuthSession` 持久化；
 *   每次发起 API 请求时通过 `getEnterpriseAuthSession` 读取 token 注入鉴权头；
 *   退出登录时调用 `clearEnterpriseAuthSession` 清除。
 * - **权限判定**：提供 `isEnterpriseAdmin`、`isGalleryEmployee`、`isEmployeeOwnedBy` 等辅助函数，
 *   用于前端 UI 层的权限控制（如按钮可见性、路由守卫等）。
 */

/** 企业版鉴权用户信息 */
export type EnterpriseAuthUser = {
  /** 用户唯一 ID */
  id: string;
  /** 所属租户 ID */
  tenant_id: string;
  /** 登录用户名 */
  username: string;
  /** 显示名称（可选，用于 UI 展示） */
  display_name?: string;
  /** 角色：admin（管理员）或 member（普通成员） */
  role: 'admin' | 'member';
};

/** 企业版鉴权会话，包含 token 和用户信息 */
export type EnterpriseAuthSession = {
  /** JWT 访问令牌，用于后续 API 请求的 Bearer 鉴权 */
  token: string;
  /** 当前登录用户信息 */
  user: EnterpriseAuthUser;
};

/** localStorage 中存储会话的键名 */
export const ENTERPRISE_AUTH_STORAGE_KEY = 'ultrarag_auth';

/**
 * 读取当前已持久化的鉴权会话。
 *
 * @returns 会话对象；若不存在或数据格式非法则返回 `null`
 */
export function getEnterpriseAuthSession(): EnterpriseAuthSession | null {
  return readStoredSession(ENTERPRISE_AUTH_STORAGE_KEY);
}

/**
 * 持久化鉴权会话到 localStorage。
 *
 * @param session 待存储的会话对象
 */
export function setEnterpriseAuthSession(session: EnterpriseAuthSession): void {
  window.localStorage.setItem(ENTERPRISE_AUTH_STORAGE_KEY, JSON.stringify(session));
}

/**
 * 清除已持久化的鉴权会话（退出登录时调用）。
 */
export function clearEnterpriseAuthSession(): void {
  window.localStorage.removeItem(ENTERPRISE_AUTH_STORAGE_KEY);
}

/**
 * 从 localStorage 中安全读取并解析会话对象。
 *
 * 读取后校验 token 和 user.id 是否存在，数据损坏时返回 `null` 而非抛错。
 *
 * @param key localStorage 键名
 * @returns 解析后的会话对象，或 `null`
 */
function readStoredSession(key: string): EnterpriseAuthSession | null {
  const raw = window.localStorage.getItem(key);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as EnterpriseAuthSession;
    // 校验必要字段，防止脏数据
    if (!parsed.token || !parsed.user?.id) return null;
    return parsed;
  } catch {
    return null;
  }
}

/**
 * 判断用户是否为管理员。
 *
 * @param user 当前用户，可为空
 * @returns 角色为 `admin` 时返回 `true`
 */
export function isEnterpriseAdmin(user?: EnterpriseAuthUser | null): boolean {
  return user?.role === 'admin';
}

/**
 * 判断数字员工是否已发布到开放广场。
 *
 * 通过检查 agent metadata 中的 `published_to_gallery` 标志位判定。
 *
 * @param agent 数字员工对象，需包含 metadata 字段
 * @returns 已发布到广场时返回 `true`
 */
export function isGalleryEmployee(agent?: { metadata?: Record<string, unknown> } | null): boolean {
  return agent?.metadata?.published_to_gallery === true;
}

/**
 * 判断数字员工是否由指定用户创建（即用户是否为该员工的所有者）。
 *
 * 通过比对 agent metadata 中的 `owner_user_id` 与用户 ID 判定。
 *
 * @param agent 数字员工对象，需包含 metadata 字段
 * @param user  当前用户，可为空
 * @returns 所有者匹配时返回 `true`；用户为空时返回 `false`
 */
export function isEmployeeOwnedBy(
  agent: { metadata?: Record<string, unknown> },
  user?: EnterpriseAuthUser | null,
): boolean {
  if (!user) return false;
  const metadata = agent.metadata || {};
  const ownerUserId = metadata.owner_user_id;
  return ownerUserId === user.id;
}

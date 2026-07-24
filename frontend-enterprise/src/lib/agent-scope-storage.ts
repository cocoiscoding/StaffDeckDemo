/**
 * @file 数字员工（Agent）作用域本地存储模块。
 *
 * 管理当前选中的数字员工 ID 在 localStorage 中的持久化，
 * 以及通过自定义事件在组件间广播作用域切换。
 *
 * 核心概念：
 * - **作用域（Scope）**：用户当前操作的数字员工上下文。切换作用域会影响仪表盘、技能、知识库等页面的数据范围。
 * - **共享存储**：选中的 Agent ID 存储在 localStorage 中，页面刷新后恢复。
 * - **事件广播**：`emitAgentScopeChange` 派发自定义事件，App 层监听后同步 UI 状态。
 */

/** localStorage 中存储当前选中 Agent ID 的键名 */
export const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';
/** 别名，向后兼容 */
export const SELECTED_AGENT_STORAGE_KEY = ENTERPRISE_AGENT_STORAGE_KEY;
/** 会话筛选器存储键名前缀（按用户隔离） */
export const SESSION_FILTER_STORAGE_PREFIX = 'skill_agent_session_filter';

/**
 * 按用户 ID 构造会话筛选器的 localStorage 键名。
 *
 * @param userId 用户 ID，为空时使用 `anonymous`
 * @returns 形如 `skill_agent_session_filter:user_123` 的键名
 */
export function sessionFilterStorageKey(userId: string): string {
  return `${SESSION_FILTER_STORAGE_PREFIX}:${userId || 'anonymous'}`;
}

/**
 * 持久化当前选中的数字员工 ID 到 localStorage。
 *
 * @param agentId 选中的 Agent ID；为空时不执行任何操作
 * @param userId  当前用户 ID（保留参数，当前实现未按用户隔离作用域）
 */
export function persistSharedAgentScope(agentId: string, userId?: string): void {
  void userId;
  if (!agentId) return;
  window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, agentId);
}

/**
 * 清除持久化的数字员工作用域。
 *
 * @param userId 当前用户 ID（保留参数，当前实现未按用户隔离）
 */
export function clearSharedAgentScope(userId?: string): void {
  void userId;
  window.localStorage.removeItem(ENTERPRISE_AGENT_STORAGE_KEY);
}

/**
 * 广播数字员工作用域变更事件。
 *
 * 通过 `window.dispatchEvent` 派发自定义事件，
 * App 层的 `useEffect` 监听该事件后同步选中状态和 UI。
 *
 * @param agentId 新选中的 Agent ID
 */
export function emitAgentScopeChange(agentId: string): void {
  window.dispatchEvent(
    new CustomEvent('ultrarag-enterprise-agent-scope-change', {
      detail: { agentId },
    }),
  );
}

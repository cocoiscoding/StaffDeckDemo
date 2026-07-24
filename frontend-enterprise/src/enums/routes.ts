/**
 * @file 企业版前端路由枚举定义。
 *
 * 集中管理所有页面的 URL 路径，避免在各组件中硬编码字符串路径，减少拼写错误风险。
 * 配合 React Router 的 `<Navigate>`、`useNavigate()` 等使用。
 *
 * 枚举分为两大类路由：
 * - **工作空间路由（/workspace）**：面向终端用户，包括聊天对话和广场浏览。
 * - **企业管理路由（/enterprise）**：面向企业管理员/成员，包括仪表盘、员工管理、
 *   知识库、技能、工具、模型配置、账户管理等后台功能。
 */
export enum EnterpriseRoute {
  /** 工作空间根路径，重定向至 Gallery */
  Workspace = '/workspace',
  /** 聊天对话页面 */
  Chat = '/workspace/chat',
  /** 开放平台页面 */
  Platform = '/enterprise/platform',
  /** 聊天广场（浏览/发起对话） */
  Gallery = '/workspace/gallery',
  /** 数字员工列表页（员工花名册） */
  Agents = '/enterprise/agents',
  /** 仪表盘（员工工作总览） */
  Dashboard = '/enterprise/dashboard',
  /** 定时任务列表页 */
  ScheduledTasks = '/enterprise/scheduled-tasks',
  /** 员工记忆管理页 */
  Memories = '/enterprise/memories',
  /** 对话反馈/日志页 */
  Feedback = '/enterprise/feedback',
  /** 知识库管理页 */
  Knowledge = '/enterprise/knowledge',
  /** 通用技能（Agent Skills）管理页 */
  GeneralSkills = '/enterprise/general-skills',
  /** 领域技能（SOP）管理页 */
  Skills = '/enterprise/skills',
  /** 工具（MCP/HTTP）管理页 */
  Tools = '/enterprise/tools',
  /** 账户管理页（仅管理员） */
  Accounts = '/enterprise/accounts',
  /** 模型配置页（仅管理员） */
  Models = '/enterprise/models',
}

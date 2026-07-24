/**
 * @file 应用根组件（App.tsx）。
 *
 * 该文件是企业版前端应用的核心入口组件，职责包括：
 * - **鉴权守卫**：检查 localStorage 中的会话有效性，无效则跳转登录页；
 * - **路由分发**：将 `/workspace/*`（终端用户聊天）与 `/enterprise/*`（企业管理后台）分发到不同布局；
 * - **数字员工作用域管理**：加载员工列表、管理当前选中的员工、广播作用域切换事件；
 * - **布局框架（Shell）**：提供侧边栏、内容区域、模型配置提示、空状态引导、创建员工弹窗等；
 * - **路由兼容**：处理旧版 `/chat/*` 路径到新路径的 301 重定向。
 *
 * 组件层级（从外到内）：
 * 1. `App`（默认导出）— 顶层鉴权守卫，管理 auth 状态；
 * 2. `AuthedApp` — 鉴权通过后的路由分发组件，处理路径重定向；
 * 3. `Shell` — 企业管理后台的主框架，包含侧边栏和路由出口。
 */

import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { api, isAuthError, TENANT_ID } from "./api/client";
import {
  clearEnterpriseAuthSession,
  getEnterpriseAuthSession,
  isEnterpriseAdmin,
  isGalleryEmployee,
  setEnterpriseAuthSession,
  type EnterpriseAuthSession,
  type EnterpriseAuthUser,
} from "./auth";
import AppSidebar from "./components/AppSidebar";
import OnboardingGuide, { ONBOARDING_SEEN_KEY } from "./components/OnboardingGuide";
import QuickStartGuide, {
  QUICK_START_COMPLETED_EVENT,
  QUICK_START_SEEN_KEY,
} from "./components/QuickStartGuide";
import StaffdeckIcon from "./components/StaffdeckIcon";
import { SidebarProvider } from "@/components/ui/sidebar";
import { EnterpriseRoute } from "./enums/routes";
import {
  employeeBlankMetadata,
  canAccessEmployeeAgent,
  canManageEmployeeAgent,
  canSelectCurrentEmployeeAgent,
  employeeDisplayName,
  employeeDisplayNameWithCreator,
  employeeProfile,
  preferredEmployeeAgent,
} from "./employee";
import AccountsPage from "./pages/AccountsPage";
import AgentsPage from "./pages/AgentsPage";
import ChatPage from "./pages/chat/ChatPage";
import ChatGalleryPage from "./pages/chat/ChatGalleryPage";
import DashboardPage from "./pages/dashboard/DashboardPage";
import EmptyEmployeeState from "./components/EmptyEmployeeState";
import DistillPage from "./pages/DistillPage";
import GeneralSkillsPage, {
  GeneralSkillEditPage,
  GeneralSkillNewPage,
} from "./pages/GeneralSkillsPage";
import KnowledgeManagePage, { KnowledgeAddPage } from "./pages/KnowledgePage";
import LoginPage from "./pages/LoginPage";
import ModelsPage from "./pages/ModelsPage";
import OpenPlatformPage from "./pages/OpenPlatformPage";
import SkillsPage from "./pages/SkillsPage";
import {
  ScheduledTaskEditPage,
  ScheduledTaskNewPage,
} from "./pages/dashboard/ScheduledTasksTab";
import ToolsPage, {
  McpServerEditPage,
  McpServerNewPage,
  ToolEditPage,
  ToolNewPage,
  ToolTestPage,
} from "./pages/ToolsPage";
import { useIsMobile } from "./hooks/use-mobile";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  Input,
  Select as UISelect,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Textarea,
} from "@/components/ui";
import { Button as UIButton } from "@/components/ui/button";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { notify } from "@/components/ui/app-toast";
import {
  emitAgentScopeChange,
  ENTERPRISE_AGENT_STORAGE_KEY,
  persistSharedAgentScope,
} from "@/lib/agent-scope-storage";
import { cn } from "@/lib/utils";
import {
  SELECT_TRIGGER_CLASS,
  DIALOG_CANCEL_BUTTON_CLASS,
  DIALOG_FOOTER_CLASS,
  DIALOG_PRIMARY_BUTTON_CLASS,
} from "@/lib/enterprise-ui";
import type { AgentProfileRead, ModelConfigRead } from "./types";
import { useI18n } from "./i18n";

/** localStorage 中存储侧边栏展开/折叠状态的键名 */
const ENTERPRISE_SIDEBAR_STORAGE_KEY = "ultrarag_enterprise_sidebar_expanded";
/** 模型配置更新自定义事件名，用于跨组件同步模型列表 */
const MODEL_CONFIGS_UPDATED_EVENT = "ultrarag-enterprise-model-configs-updated";

/** 创建数字员工的模式：从广场复制或从空白开始 */
type AgentCreateMode = "copy" | "blank";

/** 创建数字员工弹窗的表单状态 */
type AgentCreateFormState = {
  /** 员工姓名 */
  name: string;
  /** 岗位描述 */
  description: string;
  /** 职位名称 */
  roleName: string;
  /** 创建模式 */
  sourceMode: AgentCreateMode;
  /** 复制来源员工 ID（sourceMode 为 copy 时使用） */
  copyFromAgentId: string;
};

/** 创建员工弹窗的初始空表单状态 */
const EMPTY_AGENT_FORM: AgentCreateFormState = {
  name: "",
  description: "",
  roleName: "",
  sourceMode: "copy",
  copyFromAgentId: "",
};

/**
 * 企业管理后台主框架组件（Shell）。
 *
 * 这是鉴权通过后、企业管理后台的核心布局组件，职责包括：
 * - 渲染侧边栏（AppSidebar）和内容路由区域；
 * - 管理数字员工列表的加载、选中和作用域切换；
 * - 监听模型配置更新、引导完成状态、侧边栏折叠等自定义事件；
 * - 当无可用模型配置时展示提示横幅；
 * - 当无数字员工时展示空状态引导；
 * - 提供"新建数字员工"弹窗（支持从广场复制或空白创建）。
 *
 * @param props.auth     当前鉴权会话
 * @param props.onLogout 退出登录回调
 * @returns Shell 布局 JSX
 */
function Shell({
  auth,
  onLogout,
}: {
  auth: EnterpriseAuthSession;
  onLogout: () => void;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const { t } = useI18n();
  // 全部数字员工列表（含开放广场）
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  // 员工列表是否已加载完成（用于控制空状态展示时机）
  const [agentsLoaded, setAgentsLoaded] = useState(false);
  // 当前选中的数字员工 ID（从 localStorage 恢复）
  const [selectedAgentId, setSelectedAgentId] = useState(
    () => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || "",
  );
  // 侧边栏展开/折叠状态（从 localStorage 恢复，默认展开）
  const [sidebarExpanded, setSidebarExpanded] = useState(() => {
    const stored = window.localStorage.getItem(ENTERPRISE_SIDEBAR_STORAGE_KEY);
    return stored == null ? true : stored === "1";
  });
  // 创建数字员工弹窗的开关和表单状态
  const [agentCreateOpen, setAgentCreateOpen] = useState(false);
  const [agentForm, setAgentForm] =
    useState<AgentCreateFormState>(EMPTY_AGENT_FORM);
  // 模型配置列表（用于检测是否有可用模型）
  const [modelConfigs, setModelConfigs] = useState<ModelConfigRead[]>([]);
  const [modelConfigsLoaded, setModelConfigsLoaded] = useState(false);
  // 新手引导（Onboarding + QuickStart）是否已完成
  const [guidesCompleted, setGuidesCompleted] = useState(() => Boolean(
    window.localStorage.getItem(ONBOARDING_SEEN_KEY)
    && window.localStorage.getItem(QUICK_START_SEEN_KEY),
  ));
  const isMobile = useIsMobile();
  const isAdmin = isEnterpriseAdmin(auth.user);
  const accountRoleLabel = isAdmin ? "管理员" : "";
  // 当前路由是否为技能蒸馏页面（该页面使用持久化布局，不随路由切换卸载）
  const isDistillRoute = location.pathname === "/enterprise/skills/distill";
  const selected =
    location.pathname === "/enterprise"
      ? "/enterprise/dashboard"
      : location.pathname.startsWith("/enterprise/platform")
        ? "/enterprise/platform"
        : location.pathname.startsWith("/enterprise/knowledge")
          ? "/enterprise/knowledge"
          : location.pathname.startsWith("/enterprise/general-skills")
            ? "/enterprise/general-skills"
            : location.pathname.startsWith("/enterprise/tools")
              ? "/enterprise/tools"
              : location.pathname.startsWith("/enterprise/scheduled-tasks")
                ? "/enterprise/scheduled-tasks"
                : isDistillRoute
                  ? "/enterprise/skills"
                  : location.pathname;
  const isAgentRosterRoute = location.pathname.startsWith("/enterprise/agents");
  const [lastDistillSearch, setLastDistillSearch] = useState(() =>
    isDistillRoute ? location.search : "",
  );
  const distillSearch = isDistillRoute ? location.search : lastDistillSearch;
  const distillSearchParams = useMemo(
    () => new URLSearchParams(distillSearch),
    [distillSearch],
  );

  useEffect(() => {
    if (isDistillRoute) {
      setLastDistillSearch(location.search);
    }
  }, [isDistillRoute, location.search]);

  useEffect(() => {
    loadAgents();
  }, []);

  /**
   * 加载模型配置列表。
   * 成功后更新状态；失败时清空列表并标记为未加载。
   */
  const loadModelConfigs = useCallback(() => {
    return api
      .get<ModelConfigRead[]>(`/api/enterprise/model-configs?tenant_id=${TENANT_ID}`)
      .then((items) => {
        setModelConfigs(items);
        setModelConfigsLoaded(true);
      })
      .catch(() => {
        setModelConfigs([]);
        setModelConfigsLoaded(false);
      });
  }, []);

  useEffect(() => {
    void loadModelConfigs();
  }, [loadModelConfigs]);

  // 模型配置加载完成后，监听跨组件的模型配置更新事件
  useEffect(() => {
    const onModelConfigsUpdated = (event: Event) => {
      const rows = (event as CustomEvent<{ models?: ModelConfigRead[] }>).detail?.models;
      if (rows) {
        setModelConfigs(rows);
        setModelConfigsLoaded(true);
      } else {
        void loadModelConfigs();
      }
    };
    window.addEventListener(MODEL_CONFIGS_UPDATED_EVENT, onModelConfigsUpdated);
    return () => window.removeEventListener(MODEL_CONFIGS_UPDATED_EVENT, onModelConfigsUpdated);
  }, [loadModelConfigs]);

  // 监听快速入门完成事件，标记引导已完成
  useEffect(() => {
    const onQuickStartCompleted = () => setGuidesCompleted(true);
    window.addEventListener(QUICK_START_COMPLETED_EVENT, onQuickStartCompleted);
    return () => window.removeEventListener(QUICK_START_COMPLETED_EVENT, onQuickStartCompleted);
  }, []);

  // 移动端自动折叠侧边栏；桌面端恢复用户保存的偏好
  useEffect(() => {
    if (isMobile) {
      setSidebarExpanded(false);
    } else {
      const stored = window.localStorage.getItem(
        ENTERPRISE_SIDEBAR_STORAGE_KEY,
      );
      setSidebarExpanded(stored == null ? true : stored === "1");
    }
  }, [isMobile]);

  // 监听员工列表刷新事件（如其他组件创建/删除员工后通知 Shell 重新加载）
  useEffect(() => {
    const onAgentRefresh = () => {
      void loadAgents();
    };
    window.addEventListener(
      "ultrarag-enterprise-agent-scope-refresh",
      onAgentRefresh,
    );
    return () =>
      window.removeEventListener(
        "ultrarag-enterprise-agent-scope-refresh",
        onAgentRefresh,
      );
  }, []);

  // 监听员工作用域切换事件，同步选中状态（跨组件通信）
  useEffect(() => {
    const onScopeChange = (event: Event) => {
      const nextAgentId =
        (event as CustomEvent<{ agentId?: string }>).detail?.agentId ||
        window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) ||
        "";
      if (nextAgentId) {
        persistSharedAgentScope(nextAgentId, auth.user.id);
        const knownSelectableAgent = agents.some(
          (item) => item.id === nextAgentId && canUseAgentScope(item),
        );
        if (!knownSelectableAgent) void loadAgents(nextAgentId);
      }
      setSelectedAgentId(nextAgentId);
    };
    window.addEventListener(
      "ultrarag-enterprise-agent-scope-change",
      onScopeChange,
    );
    return () =>
      window.removeEventListener(
        "ultrarag-enterprise-agent-scope-change",
        onScopeChange,
      );
  }, [agents, auth.user.id]);

  // 监听"创建数字员工"事件，打开创建弹窗（由空状态引导等组件触发）
  useEffect(() => {
    const onCreateAgent = () => openCreateAgentModal();
    window.addEventListener("ultrarag-enterprise-agent-create", onCreateAgent);
    return () =>
      window.removeEventListener(
        "ultrarag-enterprise-agent-create",
        onCreateAgent,
      );
  }, []);

  /**
   * 加载数字员工列表并确定当前选中的员工。
   *
   * 数据流：
   * 1. 从后端获取全部员工列表；
   * 2. 筛选出当前用户可选择的员工；
   * 3. 优先保留 localStorage 中已选中的员工（若仍可选）；
   * 4. 否则按优先级自动选择一个默认员工（管理员优先选默认/首个，普通用户优先选自有的）；
   * 5. 持久化选中结果并广播作用域变更事件。
   *
   * @param preferredAgentId 指定优先选中的员工 ID（可选）
   */
  function loadAgents(preferredAgentId = "") {
    return api
      .get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`)
      .then((rows) => {
        setAgents(rows);
        const selectableRows = rows.filter((item) => canUseAgentScope(item));
        setSelectedAgentId((current) => {
          const requestedAgentId = preferredAgentId || current;
          if (
            requestedAgentId &&
            selectableRows.some((item) => item.id === requestedAgentId)
          ) {
            persistSharedAgentScope(requestedAgentId, auth.user.id);
            return requestedAgentId;
          }
          const manageableRows = selectableRows.filter((item) =>
            canManageEmployeeAgent(item, auth.user),
          );
          const next = isAdmin
            ? preferredEmployeeAgent(selectableRows)?.id || ""
            : preferredEmployeeAgent(manageableRows)?.id ||
              preferredEmployeeAgent(selectableRows)?.id ||
              "";
          if (next) {
            persistSharedAgentScope(next, auth.user.id);
            if (next !== current) {
              emitAgentScopeChange(next);
            }
          }
          return next;
        });
      })
      .catch(() => setAgents([]))
      .finally(() => setAgentsLoaded(true));
  }

  /** 判断指定员工是否可用于当前用户的作用域（活跃且用户可选择） */
  function canUseAgentScope(agent: AgentProfileRead): boolean {
    return canSelectCurrentEmployeeAgent(agent, auth.user, { activeOnly: true });
  }

  /**
   * 切换当前数字员工作用域。
   * 更新本地状态、持久化到 localStorage，并广播自定义事件通知其他组件。
   *
   * @param agentId 新选中的员工 ID
   */
  function changeAgentScope(agentId: string) {
    setSelectedAgentId(agentId);
    persistSharedAgentScope(agentId, auth.user.id);
    emitAgentScopeChange(agentId);
  }

  /**
   * 侧边栏展开/折叠状态变更处理。
   * 同步更新状态并持久化到 localStorage。
   *
   * @param open 是否展开
   */
  function handleSidebarOpenChange(open: boolean) {
    setSidebarExpanded(open);
    window.localStorage.setItem(
      ENTERPRISE_SIDEBAR_STORAGE_KEY,
      open ? "1" : "0",
    );
  }

  // 当前用户可选择使用的员工列表
  const scopeAgents = agents.filter(canUseAgentScope);
  // 是否存在至少一个已启用的模型配置
  const hasUsableModelConfig = modelConfigs.some((item) => item.enabled);
  // 是否显示模型配置缺失提示（引导已完成、模型已加载、但无可用模型）
  const showModelSetupNotice = guidesCompleted && modelConfigsLoaded && !hasUsableModelConfig;
  // 模型提示文案：管理员看到操作指引，普通用户看到联系管理员
  const modelSetupNoticeText = isAdmin
    ? t("还没有可用模型配置，数字员工暂不能调用模型。请先完成模型配置。")
    : t("系统管理员尚未配置可用模型，数字员工暂不能调用模型。请联系管理员完成模型配置。");
  const selectedAgent = scopeAgents.find((item) => item.id === selectedAgentId);
  const sidebarAgent = selectedAgent;
  // 当前路由是否为需要数字员工才能操作的页面（无员工时显示空状态引导）
  const EMPLOYEE_SCOPED_PREFIXES = [
    "/enterprise/dashboard",
    "/enterprise/scheduled-tasks",
    "/enterprise/memories",
    "/enterprise/feedback",
    "/enterprise/knowledge",
    "/enterprise/general-skills",
    "/enterprise/skills",
    "/enterprise/tools",
  ];
  const hasEmployees = scopeAgents.some((item) => !item.is_overall);
  const isEmployeeScopedRoute = EMPLOYEE_SCOPED_PREFIXES.some((prefix) =>
    location.pathname.startsWith(prefix),
  );
  const showEmployeeEmptyState =
    agentsLoaded && !hasEmployees && isEmployeeScopedRoute;
  const sourceAgents = agents.filter((item) =>
    canAccessEmployeeAgent(item, auth.user, {
      activeOnly: true,
      includeOverall: isAdmin,
    }),
  );
  const selectedAgentName = selectedAgent
    ? employeeDisplayName(selectedAgent)
    : "未选择";
  const selectedAgentCaption = selectedAgent
    ? selectedAgent.is_overall
      ? "开放广场"
      : employeeProfile(selectedAgent).roleName
    : "-";
  /**
   * 打开"新建数字员工"弹窗，初始化表单默认值（复制来源默认为当前选中的员工）。
   */
  function openCreateAgentModal() {
    setAgentForm({
      ...EMPTY_AGENT_FORM,
      copyFromAgentId: selectedAgentId || sourceAgents[0]?.id || "",
    });
    setAgentCreateOpen(true);
  }

  /**
   * 保存"新建数字员工"表单。
   *
   * 数据流：
   * 1. 校验员工姓名非空；
   * 2. 根据创建模式（复制/空白）确定来源员工的 metadata 和职位名；
   * 3. 组装基础 metadata（含所有者信息、创建者信息、角色等）；
   * 4. 调用后端 API 创建员工（复制模式传 copy_from_agent_id，空白模式使用 blank metadata）；
   * 5. 创建成功后重新加载列表、切换到新员工作用域、关闭弹窗并提示成功。
   */
  async function saveAgentCreateModal() {
    const name = agentForm.name.trim();
    if (!name) {
      notify.error("请填写数字员工姓名");
      return;
    }
    const isBlankOnboarding = agentForm.sourceMode === "blank";
    const sourceAgent = agentForm.copyFromAgentId
      ? sourceAgents.find((item) => item.id === agentForm.copyFromAgentId)
      : undefined;
    const sourceMetadata =
      !isBlankOnboarding && sourceAgent?.metadata ? sourceAgent.metadata : {};
    const sourceRoleName =
      sourceAgent && !sourceAgent.is_overall
        ? employeeProfile(sourceAgent).roleName
        : "";
    const roleName =
      agentForm.roleName.trim() ||
      (!isBlankOnboarding ? sourceRoleName : "") ||
      "待补充职位";
    const description =
      agentForm.description.trim() ||
      (!isBlankOnboarding
        ? sourceAgent?.description ||
          String(sourceMetadata.system_prompt_summary || "")
        : "") ||
      "";
    const baseMetadata = {
      ...sourceMetadata,
      system_prompt_summary: description,
      owner_user_id: auth.user.id,
      owner_username: auth.user.username,
      owner_display_name: auth.user.display_name || auth.user.username,
      created_by_user_id: auth.user.id,
      created_by_username: auth.user.username,
      created_by: auth.user.username,
      created_by_display_name: auth.user.display_name || auth.user.username,
      creator_name: auth.user.username,
      role_key: "",
      role_name: roleName,
      onboarded_at: new Date().toISOString().slice(0, 10),
      blank_onboarding: isBlankOnboarding,
    };
    try {
      const created = await api.post<AgentProfileRead>(
        "/api/enterprise/agents",
        {
          tenant_id: TENANT_ID,
          name,
          description,
          source_mode: agentForm.sourceMode,
          copy_from_agent_id:
            agentForm.sourceMode === "copy"
              ? agentForm.copyFromAgentId || undefined
              : undefined,
          metadata: isBlankOnboarding
            ? employeeBlankMetadata(baseMetadata)
            : baseMetadata,
        },
      );
      await loadAgents();
      changeAgentScope(created.id);
      setAgentCreateOpen(false);
      notify.success("数字员工创建成功");
    } catch (error) {
      notify.error(error instanceof Error ? error.message : "创建数字员工失败");
    }
  }

  return (
    <SidebarProvider
      open={sidebarExpanded}
      onOpenChange={handleSidebarOpenChange}
      style={
        {
          "--sidebar-width": "220px",
          "--sidebar-width-icon": "72px",
        } as CSSProperties
      }
      className={`app-shell ${sidebarExpanded ? "sidebar-expanded" : "sidebar-collapsed"} ${isAgentRosterRoute ? "is-agent-roster" : ""}`}
    >
      <AppSidebar
        selected={selected}
        onNavigate={navigate}
        isAdmin={isAdmin}
        sidebarAgent={sidebarAgent}
        scopeAgents={scopeAgents}
        selectedAgentId={selectedAgentId}
        onSelectAgent={(agentId) => {
          if (agentId !== selectedAgentId) changeAgentScope(agentId);
          navigate(EnterpriseRoute.Dashboard);
        }}
        onOpenChat={() => {
          navigate(EnterpriseRoute.Gallery);
        }}
        modelSetupAttention={isAdmin && showModelSetupNotice}
      />
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div
          className={`content flex-1 ${isDistillRoute ? "flex min-h-0 flex-col overflow-hidden p-0!" : ""} ${selected === "/enterprise/dashboard" ? "sd1-dashboard-content" : ""} ${selected !== "/enterprise/dashboard" && !isDistillRoute ? "sd1-management-content" : ""}`}
        >
          {showModelSetupNotice && (
            <div className="mx-[24px] mt-[18px] mb-[10px] flex shrink-0 flex-col items-start justify-between gap-[12px] rounded-[12px] border border-[#f3d28b] bg-[#fff8e8] px-[18px] py-[12px] text-[#6f4500] shadow-[0_8px_24px_rgba(92,62,0,0.08)] sm:flex-row sm:items-center">
              <div className="flex min-w-0 items-center gap-[10px]">
                <span className="flex size-[28px] shrink-0 items-center justify-center rounded-[8px] bg-[#ffe7ad] text-[#8a4b00]">
                  <StaffdeckIcon name="model" className="size-[15px]" />
                </span>
                <span className="min-w-0 text-[13px] leading-[20px]">{modelSetupNoticeText}</span>
              </div>
              {isAdmin && (
                <UIButton
                  type="button"
                  size="sm"
                  onClick={() => navigate(EnterpriseRoute.Models)}
                  className="h-[32px] shrink-0 rounded-[8px] bg-[#1a71ff] px-[12px] text-[12px] text-white hover:bg-[#0f5ed7]"
                >
                  {t("去配置")}
                </UIButton>
              )}
            </div>
          )}
          <div
            className={
              isDistillRoute
                ? "persistent-distill active flex min-h-0 flex-1 flex-col"
                : "persistent-distill hidden"
            }
          >
            <DistillPage
              active={isDistillRoute}
              searchParamsOverride={distillSearchParams}
              currentUser={auth.user}
              onLogout={onLogout}
            />
          </div>
          {!isDistillRoute && showEmployeeEmptyState && (
            <EmptyEmployeeState
              isAdmin={isAdmin}
              onCreate={openCreateAgentModal}
              onBrowsePlatform={() => navigate(EnterpriseRoute.Platform)}
            />
          )}
          {!isDistillRoute && !showEmployeeEmptyState && (
            <Routes>
              <Route
                path="/enterprise"
                element={<Navigate to="/enterprise/dashboard" replace />}
              />
              <Route
                path="/enterprise/platform"
                element={
                  <OpenPlatformPage
                    currentUser={auth.user}
                    isAdmin={isAdmin}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/platform/:kind"
                element={
                  <OpenPlatformPage
                    currentUser={auth.user}
                    isAdmin={isAdmin}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/dashboard"
                element={
                  <DashboardPage
                    currentUser={auth.user}
                    isAdmin={isAdmin}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/agents"
                element={
                  <AgentsPage
                    currentUser={auth.user}
                    isAdmin={isAdmin}
                    onCreateAgent={openCreateAgentModal}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/memories"
                element={
                  <DashboardPage
                    currentUser={auth.user}
                    isAdmin={isAdmin}
                    profileTab="memories"
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/knowledge"
                element={
                  <KnowledgeManagePage
                    currentUser={auth.user}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/knowledge/new"
                element={
                  <KnowledgeAddPage
                    currentUser={auth.user}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/feedback"
                element={
                  <DashboardPage
                    currentUser={auth.user}
                    isAdmin={isAdmin}
                    profileTab="logs"
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/scheduled-tasks"
                element={
                  <DashboardPage
                    currentUser={auth.user}
                    isAdmin={isAdmin}
                    profileTab="scheduled"
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/scheduled-tasks/new"
                element={
                  <ScheduledTaskNewPage
                    currentUser={auth.user}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/scheduled-tasks/:taskId/edit"
                element={
                  <ScheduledTaskEditPage
                    currentUser={auth.user}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/skills"
                element={
                  <SkillsPage currentUser={auth.user} onLogout={onLogout} />
                }
              />
              <Route
                path="/enterprise/general-skills"
                element={
                  <GeneralSkillsPage
                    currentUser={auth.user}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/general-skills/new"
                element={
                  <GeneralSkillNewPage
                    currentUser={auth.user}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/general-skills/:slug/edit"
                element={
                  <GeneralSkillEditPage
                    currentUser={auth.user}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/accounts"
                element={
                  isAdmin ? (
                    <AccountsPage currentUser={auth.user} onLogout={onLogout} />
                  ) : (
                    <Navigate to={EnterpriseRoute.Gallery} replace />
                  )
                }
              />
              <Route
                path="/enterprise/models"
                element={
                  isAdmin ? (
                    <ModelsPage currentUser={auth.user} onLogout={onLogout} />
                  ) : (
                    <Navigate to={EnterpriseRoute.Gallery} replace />
                  )
                }
              />
              <Route
                path="/enterprise/tools"
                element={
                  <ToolsPage currentUser={auth.user} onLogout={onLogout} />
                }
              />
              <Route
                path="/enterprise/tools/new"
                element={
                  <ToolNewPage currentUser={auth.user} onLogout={onLogout} />
                }
              />
              <Route
                path="/enterprise/tools/mcp/new"
                element={
                  <McpServerNewPage
                    currentUser={auth.user}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/tools/mcp/:serverId/edit"
                element={
                  <McpServerEditPage
                    currentUser={auth.user}
                    onLogout={onLogout}
                  />
                }
              />
              <Route
                path="/enterprise/tools/:toolId/edit"
                element={
                  <ToolEditPage currentUser={auth.user} onLogout={onLogout} />
                }
              />
              <Route
                path="/enterprise/tools/:toolId/test"
                element={
                  <ToolTestPage currentUser={auth.user} onLogout={onLogout} />
                }
              />
              <Route
                path="/enterprise/persona"
                element={<Navigate to="/enterprise/dashboard" replace />}
              />
              <Route
                path="*"
                element={<Navigate to="/enterprise/dashboard" replace />}
              />
            </Routes>
          )}
        </div>
      </div>
      <Dialog open={agentCreateOpen} onOpenChange={setAgentCreateOpen}>
        <DialogContent className="flex max-h-[calc(100dvh-32px)] w-[calc(100%-32px)] flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-[520px]">
          <DialogTitle className="shrink-0 px-[24px] py-[16px] text-[16px] font-semibold text-foreground">
            新建数字员工
          </DialogTitle>
          <div className="agent-editor-form min-h-0 flex-1 overflow-y-auto px-[24px] pb-[16px]">
            <label>
              创建方式
              <div className="inline-flex w-fit gap-[4px] rounded-[10px] border border-border p-[2px]">
                {[
                  { label: "从广场复制", value: "copy" as const },
                  { label: "从空白开始", value: "blank" as const },
                ].map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    className={cn(
                      "rounded-[8px] px-[14px] py-[5px] text-[13px] font-medium transition-colors",
                      agentForm.sourceMode === option.value
                        ? "bg-[#18181a] text-white"
                        : "text-[#5b6273] hover:text-foreground",
                    )}
                    onClick={() =>
                      setAgentForm((prev) => ({
                        ...prev,
                        sourceMode: option.value,
                        copyFromAgentId:
                          option.value === "blank" ? "" : prev.copyFromAgentId,
                      }))
                    }
                  >
                    {option.label}
                  </button>
                ))}
              </div>
            </label>
            <label>
              职位
              <Input
                value={agentForm.roleName}
                onChange={(event) =>
                  setAgentForm((prev) => ({
                    ...prev,
                    roleName: event.target.value,
                  }))
                }
                placeholder="例如 研发工程师、财务助理"
              />
            </label>
            <div className="grid content-start gap-[6px]">
            {agentForm.sourceMode === "copy" && (
              <label>
                复制来源
                <UISelect
                  value={agentForm.copyFromAgentId || undefined}
                  onValueChange={(value) =>
                    setAgentForm((prev) => {
                      const nextSource = sourceAgents.find(
                        (item) => item.id === value,
                      );
                      return {
                        ...prev,
                        copyFromAgentId: value,
                        roleName:
                          prev.roleName ||
                          (nextSource && !nextSource.is_overall
                            ? employeeProfile(nextSource).roleName
                            : ""),
                      };
                    })
                  }
                >
                  <SelectTrigger className={cn(SELECT_TRIGGER_CLASS, "w-full")}>
                    <SelectValue placeholder="选择复制来源" />
                  </SelectTrigger>
                  <SelectContent>
                    {sourceAgents.map((agent) => (
                      <SelectItem key={agent.id} value={agent.id}>
                        {agent.is_overall
                          ? "开放广场"
                          : `${employeeDisplayNameWithCreator(agent)} · ${employeeProfile(agent).roleName}${isGalleryEmployee(agent) ? " · 广场" : ""}`}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </UISelect>
              </label>
            )}
            {agentForm.sourceMode === "blank" && (
              <div className="agent-definition-note">
                从空白开始创建，不继承任何已有配置。
              </div>
            )}
            </div>
            <label>
              数字员工姓名
              <Input
                value={agentForm.name}
                onChange={(event) =>
                  setAgentForm((prev) => ({
                    ...prev,
                    name: event.target.value,
                  }))
                }
              />
            </label>
            <label>
              岗位描述
              <Textarea
                rows={3}
                value={agentForm.description}
                onChange={(event) =>
                  setAgentForm((prev) => ({
                    ...prev,
                    description: event.target.value,
                  }))
                }
                placeholder="概括这个数字员工的岗位边界、服务风格和执行重点"
              />
            </label>
          </div>
          <div className={cn(DIALOG_FOOTER_CLASS, "shrink-0 border-t border-border")}>
            <UIButton
              variant="outline"
              className={DIALOG_CANCEL_BUTTON_CLASS}
              onClick={() => setAgentCreateOpen(false)}
            >
              取消
            </UIButton>
            <UIButton
              className={DIALOG_PRIMARY_BUTTON_CLASS}
              onClick={() => void saveAgentCreateModal()}
            >
              创建
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>
    </SidebarProvider>
  );
}

/**
 * 鉴权通过后的路由分发组件。
 *
 * 职责：
 * - 处理旧版路由路径（`/chat/*`）到新路径（`/workspace/chat/*`）的重定向；
 * - 将 `/workspace/*` 路由分发到聊天相关页面（Gallery / ChatPage）；
 * - 其他路径（含 `/enterprise/*`）渲染 `Shell` 企业管理框架。
 *
 * @param props.auth     当前鉴权会话
 * @param props.onLogout 退出登录回调
 * @returns 路由对应的页面组件或重定向
 */
function AuthedApp({
  auth,
  onLogout,
}: {
  auth: EnterpriseAuthSession;
  onLogout: () => void;
}) {
  const location = useLocation();
  if (location.pathname === "/") {
    return <Navigate to={EnterpriseRoute.Gallery} replace />;
  }
  if (location.pathname === "/chat" || location.pathname === "/chat/") {
    return <Navigate to={EnterpriseRoute.Gallery} replace />;
  }
  if (location.pathname.startsWith("/chat/draft/")) {
    const nextPath = location.pathname.replace(/^\/chat/, EnterpriseRoute.Chat);
    return <Navigate to={`${nextPath}${location.search}`} replace />;
  }
  if (location.pathname.startsWith("/chat/session_")) {
    const nextPath = location.pathname.replace(/^\/chat/, EnterpriseRoute.Chat);
    return <Navigate to={`${nextPath}${location.search}`} replace />;
  }
  if (location.pathname === "/enterprise/chat" || location.pathname === "/enterprise/chat/") {
    return <Navigate to={EnterpriseRoute.Gallery} replace />;
  }
  if (location.pathname.startsWith("/enterprise/chat/draft/")) {
    const nextPath = location.pathname.replace(/^\/enterprise\/chat/, EnterpriseRoute.Chat);
    return <Navigate to={`${nextPath}${location.search}`} replace />;
  }
  if (location.pathname.startsWith("/enterprise/chat/session_")) {
    const nextPath = location.pathname.replace(/^\/enterprise\/chat/, EnterpriseRoute.Chat);
    return <Navigate to={`${nextPath}${location.search}`} replace />;
  }
  if (location.pathname.startsWith(EnterpriseRoute.Workspace)) {
    return (
      <Routes>
        <Route
          path="/workspace"
          element={<Navigate to="/workspace/gallery" replace />}
        />
        <Route path="/workspace/gallery" element={<ChatGalleryPage />} />
        <Route path="/workspace/chat" element={<ChatPage />} />
        <Route
          path="/workspace/chat/draft/:draftAgentId"
          element={<ChatPage />}
        />
        <Route path="/workspace/chat/:sessionId" element={<ChatPage />} />
      </Routes>
    );
  }
  return <Shell auth={auth} onLogout={onLogout} />;
}

/**
 * 应用根组件（默认导出）。
 *
 * 职责：
 * - 订阅国际化语言变化（使 locale 敏感的日期和计算标签更新而不丢失表单状态）；
 * - 管理鉴权状态：从 localStorage 恢复会话，并通过 `/api/auth/me` 验证 token 有效性；
 * - 鉴权通过且 token 有效时渲染 `AuthedApp`，否则渲染 `LoginPage`；
 * - 鉴权验证期间（authChecked 为 false）不渲染任何内容，避免闪烁；
 * - 鉴权通过后挂载新手引导组件（OnboardingGuide、QuickStartGuide）；
 * - 提供全局 Tooltip Provider、路由器和 Toast 通知容器。
 *
 * @returns 应用根 JSX
 */
export default function App() {
  // 订阅语言变化，使 locale 敏感的 UI 更新而不重新挂载
  useI18n();
  // 从 localStorage 恢复鉴权会话
  const [auth, setAuth] = useState<EnterpriseAuthSession | null>(() =>
    getEnterpriseAuthSession(),
  );
  // 鉴权是否已验证（无 token 时直接标记为已检查）
  const [authChecked, setAuthChecked] = useState(() => !auth?.token);

  // 鉴权验证：若有 token 则调用 /api/auth/me 验证有效性并刷新用户信息
  useEffect(() => {
    if (!auth?.token) {
      setAuthChecked(true);
      return undefined;
    }
    let cancelled = false;
    setAuthChecked(false);
    void api.get<EnterpriseAuthUser>("/api/auth/me")
      .then((user) => {
        if (cancelled) return;
        // token 有效：更新会话中的用户信息并持久化
        const refreshed = { token: auth.token, user };
        setEnterpriseAuthSession(refreshed);
        setAuth(refreshed);
        setAuthChecked(true);
      })
      .catch((error) => {
        if (cancelled) return;
        // token 无效（401）：清除会话，回到登录页
        if (isAuthError(error)) {
          clearEnterpriseAuthSession();
          setAuth(null);
        }
        setAuthChecked(true);
      });
    return () => {
      cancelled = true;
    };
  }, [auth?.token]);

  /**
   * 退出登录：清除会话、重置 auth 状态。
   */
  function logout() {
    clearEnterpriseAuthSession();
    setAuth(null);
    setAuthChecked(true);
  }

  return (
    <TooltipProvider>
      <BrowserRouter>
        <Routes>
          <Route
            path="/*"
            element={
              auth && !authChecked ? null : auth ? (
                <AuthedApp auth={auth} onLogout={logout} />
              ) : (
                <LoginPage onLogin={setAuth} />
              )
            }
          />
        </Routes>
        {auth && authChecked ? <OnboardingGuide /> : null}
        {auth && authChecked ? <QuickStartGuide isAdmin={isEnterpriseAdmin(auth.user)} /> : null}
      </BrowserRouter>
      <Toaster richColors closeButton position="top-center" />
    </TooltipProvider>
  );
}

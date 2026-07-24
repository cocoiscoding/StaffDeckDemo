/**
 * @file pages/EmployeeGalleryPage.tsx
 * @description 数字员工广场页面（对话端入口）。
 *
 * 展示当前用户可访问的数字员工列表，支持三种作用域切换：
 * - 所有员工：当前用户可访问的全部员工
 * - 我的数字员工：当前用户可管理的员工
 * - 数字员工广场：已发布到公共广场但不在"我的"列表中的员工
 *
 * 功能包括：
 * - 搜索（按名称、角色名、描述、工作风格、专长标签）
 * - 发起对话（点击员工卡片）
 * - 编辑头像、编辑档案（需管理权限）
 * - 删除员工（需管理权限）
 *
 * 数据通过 /api/enterprise/agents 接口获取。
 */

import { UnderlineTabs, type UnderlineTabItem } from '@/components/ui';
import { notify } from '@/components/ui/app-toast';

import IconSearch from '../assets/icons/search.svg?react';

import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api, TENANT_ID } from '../api/client';
import { isGalleryEmployee, type EnterpriseAuthUser } from '../auth';

import AppHeader from '../components/AppHeader';
import { ConfirmDialog } from '../components/ConfirmDialog';
import EmployeeAvatarEditor from '../components/EmployeeAvatarEditor';
import EmployeeCard from '../components/EmployeeCard';
import EmployeeProfileEditor from '../components/EmployeeProfileEditor';
import {
  canManageEmployeeAgent,
  employeeDisplayName,
  employeeDisplayNameWithCreator,
  employeeProfile,
  isMyEmployeeAgent,
  visibleEmployeeAgents,
} from '../employee';
import type { AgentProfileRead } from '../types';

/** localStorage 键名：当前选中的数字员工作用域 */
const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';

/** 广场作用域：全部 / 我的 / 公共广场 */
type GalleryScope = 'all' | 'mine' | 'gallery';

/**
 * 数字员工广场页面主组件（对话端）。
 *
 * @param props - 组件属性
 * @param props.currentUser - 当前登录用户
 * @param props.isAdmin - 是否为管理员
 * @param props.onStartChat - 发起对话回调
 * @param props.onLogout - 退出登录回调
 * @returns 渲染好的数字员工广场页面
 */
export default function EmployeeGalleryPage({
  currentUser,
  isAdmin = false,
  onStartChat,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  isAdmin?: boolean;
  onStartChat?: (agent: AgentProfileRead) => void | Promise<void>;
  onLogout?: () => void;
}) {
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [avatarAgent, setAvatarAgent] = useState<AgentProfileRead | null>(null);
  const [profileAgent, setProfileAgent] = useState<AgentProfileRead | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<AgentProfileRead | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [startingAgentId, setStartingAgentId] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [scope, setScope] = useState<GalleryScope>('all');
  const navigate = useNavigate();

  /** 从后端加载全部数字员工列表 */
  async function load() {
    setLoading(true);
    try {
      const rows = await api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`);
      setAgents(rows);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载员工失败');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  // Keep these tabs aligned with the rest of the app:
  // - 所有员工: employees the current user can access and chat with
  // - 我的数字员工: employees the current user can manage/edit
  // - 数字员工广场: public employees not already listed as mine
  const availableAgents = useMemo(
    () => visibleEmployeeAgents(agents, currentUser, { activeOnly: true }),
    [agents, currentUser],
  );
  const myEmployees = useMemo(
    () => availableAgents.filter((item) => isMyEmployeeAgent(item, currentUser)),
    [availableAgents, currentUser],
  );
  const galleryEmployees = useMemo(() => {
    const myIds = new Set(myEmployees.map((item) => item.id));
    return availableAgents.filter((item) => isGalleryEmployee(item) && !myIds.has(item.id));
  }, [availableAgents, myEmployees]);

  const scopedEmployees = scope === 'mine'
    ? myEmployees
    : scope === 'gallery'
      ? galleryEmployees
      : availableAgents;

  const filteredEmployees = scopedEmployees.filter((item) => {
    const profile = employeeProfile(item);
    const keyword = searchTerm.trim().toLowerCase();
    if (!keyword) return true;
    return [
      employeeDisplayName(item),
      employeeDisplayNameWithCreator(item),
      profile.roleName,
      item.description || '',
      profile.workStyles.join(' '),
      profile.expertiseTags.join(' '),
    ].some((value) => value.toLowerCase().includes(keyword));
  });

  /**
   * 发起与指定员工的对话。
   * 如果传入了 onStartChat 回调则调用它，否则导航到草稿对话页。
   * @param row 目标员工
   */
  async function startEmployeeChat(row: AgentProfileRead) {
    // 防止重复点击
    if (startingAgentId) return;
    setStartingAgentId(row.id);
    try {
      if (onStartChat) {
        await onStartChat(row);
        return;
      }
      // 默认导航到对话草稿页
      navigate(`/workspace/chat/draft/${row.id}`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '发起对话失败');
    } finally {
      setStartingAgentId(null);
    }
  }

  /**
   * 更新员工状态（上线/下线）。
   * @param row 目标员工
   * @param status 目标状态：active 上线 | archived 下线
   */
  async function updateStatus(row: AgentProfileRead, status: 'active' | 'archived') {
    try {
      await api.put<AgentProfileRead>(`/api/enterprise/agents/${row.id}`, {
        tenant_id: TENANT_ID,
        status,
        metadata: row.metadata || {},
      });
      notify.success(status === 'active' ? '员工已上线' : '员工已下线');
      await load();
      window.dispatchEvent(new Event('ultrarag-enterprise-agent-scope-refresh'));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新员工状态失败');
    }
  }

  async function updateGalleryState(row: AgentProfileRead, published: boolean) {
    try {
      const metadata = {
        ...(row.metadata || {}),
        published_to_gallery: published,
        gallery_published_at: published ? new Date().toISOString() : undefined,
        gallery_published_by: published ? currentUser?.username : undefined,
      };
      await api.put<AgentProfileRead>(`/api/enterprise/agents/${row.id}`, {
        tenant_id: TENANT_ID,
        metadata,
      });
      notify.success(published ? '已发布到广场' : '已从广场下架');
      await load();
      window.dispatchEvent(new Event('ultrarag-enterprise-agent-scope-refresh'));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新广场状态失败');
    }
  }

  async function confirmDelete() {
    const row = deleteTarget;
    if (!row) return;
    setDeleting(true);
    try {
      await api.delete(`/api/enterprise/agents/${row.id}?tenant_id=${TENANT_ID}`);
      if (window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) === row.id) {
        const nextAgent = availableAgents.find((item) => item.id !== row.id && item.status === 'active')
          || availableAgents.find((item) => item.id !== row.id);
        if (nextAgent) {
          window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, nextAgent.id);
          window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: nextAgent.id } }));
        } else {
          window.localStorage.removeItem(ENTERPRISE_AGENT_STORAGE_KEY);
          window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: '' } }));
        }
      }
      notify.success('员工已删除');
      setDeleteTarget(null);
      await load();
      window.dispatchEvent(new Event('ultrarag-enterprise-agent-scope-refresh'));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除员工失败');
    } finally {
      setDeleting(false);
    }
  }

  function updateAgentInList(row: AgentProfileRead) {
    setAgents((current) => current.map((item) => (item.id === row.id ? row : item)));
  }

  const galleryTabs: UnderlineTabItem<GalleryScope>[] = [
    { value: 'all', label: '所有员工' },
    { value: 'mine', label: '我的数字员工' },
    { value: 'gallery', label: '数字员工广场' },
  ];

  const hasSearchTerm = Boolean(searchTerm.trim());
  const emptyText = hasSearchTerm ? '没有匹配的数字员工' : '暂无数字员工';
  const emptyDescription = hasSearchTerm
    ? '换个关键词，或切换员工分类再试试'
    : '当前分类还没有可用员工';

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]" aria-busy={loading}>
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
        left={(
          <div className="flex h-[50px] w-full items-center gap-[6px] rounded-[20px] bg-white px-[20px] text-[#757F9C] shadow-[0_0_6px_rgba(0,0,0,0.05)]">
            <IconSearch className="size-[20px] shrink-0" />
            <input
              value={searchTerm}
              onChange={(event) => setSearchTerm(event.target.value)}
              placeholder="搜索"
              aria-label="搜索数字员工"
              className="min-w-0 flex-1 border-0 bg-transparent text-[14px] text-[#18181A] outline-none placeholder:text-[#757F9C]"
            />
          </div>
        )}
      />

      <UnderlineTabs
        className="mt-[36px] mb-[16px] max-[560px]:w-full"
        aria-label="数字员工分类"
        value={scope}
        onChange={setScope}
        items={galleryTabs}
        tabClassName="max-[560px]:min-h-[54px] max-[560px]:w-auto max-[560px]:flex-1 max-[560px]:px-[6px] max-[560px]:text-[12px] max-[560px]:leading-[16px]"
      />

      <div className="grid auto-rows-[minmax(262px,auto)] grid-cols-1 content-start gap-[32px] sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4 max-[900px]:gap-[18px]">
        {filteredEmployees.map((employee) => (
          <EmployeeCard
            key={employee.id}
            employee={employee}
            busy={startingAgentId === employee.id}
            canManage={canManageEmployeeAgent(employee, currentUser)}
            showMenu={false}
            onOpen={() => void startEmployeeChat(employee)}
            onStatus={(status) => void updateStatus(employee, status)}
            onGallery={(published) => void updateGalleryState(employee, published)}
            onDelete={() => setDeleteTarget(employee)}
            onAvatar={() => setAvatarAgent(employee)}
            onEdit={() => setProfileAgent(employee)}
            onChat={() => void startEmployeeChat(employee)}
          />
        ))}
        {!filteredEmployees.length && (
          <EmployeeGalleryEmptyState title={emptyText} description={emptyDescription} />
        )}
      </div>

      <EmployeeAvatarEditor
        agent={avatarAgent}
        open={Boolean(avatarAgent)}
        onClose={() => setAvatarAgent(null)}
        onSaved={updateAgentInList}
      />
      <EmployeeProfileEditor
        agent={profileAgent}
        open={Boolean(profileAgent)}
        currentUser={currentUser}
        onClose={() => setProfileAgent(null)}
        onSaved={updateAgentInList}
      />
      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        loading={deleting}
        title={`删除员工「${deleteTarget ? employeeDisplayName(deleteTarget) : ''}」？`}
        description="删除后该员工的所有配置将一并移除，操作不可撤销。"
        onConfirm={() => void confirmDelete()}
      />
    </div>
  );
}

function EmployeeGalleryEmptyState({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <div className="flex h-[262px] w-full items-center justify-center rounded-[20px] border border-dashed border-[#e4e9f2] bg-[#fbfcfe] px-[24px] text-center">
      <div className="flex max-w-[210px] flex-col items-center">
        <span className="grid size-[34px] place-items-center rounded-[12px] bg-white text-[#98a2b3] shadow-[0_1px_8px_rgba(70,76,94,0.06)] ring-1 ring-[#edf1f6]">
          <IconSearch className="size-[16px] shrink-0" />
        </span>
        <p className="mt-[12px] text-[14px] font-medium leading-[20px] text-[#7f879a]">
          {title}
        </p>
        <p className="mt-[4px] text-[11px] leading-[17px] text-[#a7adbb]">
          {description}
        </p>
      </div>
    </div>
  );
}

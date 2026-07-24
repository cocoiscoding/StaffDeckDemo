/**
 * @file 开放广场单模块全量列表视图
 * @description
 * 对应路由 /enterprise/platform/:kind，展示某一类广场资源的完整列表。
 * 在标准企业页面外壳（AppHeader）内复用主页的卡片体系，提供：
 * - 统计卡片（当前数量）
 * - 筛选信号标签行
 * - 本地搜索框（按标题、描述、元信息、标签模糊匹配）
 * - 响应式卡片网格（数字员工用 PlatformEmployeeCard，其余用 PlatformResourceCard）
 * - 加载骨架与空状态占位
 */

import type { ComponentType, ReactNode, SVGProps } from 'react';
import { useMemo, useState } from 'react';

import AppHeader from '@/components/AppHeader';
import { StatCard } from '@/components/StatCard';
import { Button as UIButton } from '@/components/ui/button';
import { cn } from '@/lib/utils';

const RETURN_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-5 text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]';

import IconArrowRight from '../../assets/icons/arrow-right.svg?react';
import IconRefresh from '../../assets/icons/refresh.svg?react';
import IconSearch from '../../assets/icons/search.svg?react';
import plazaKnowledgeIcon from '../../assets/icons/plaza-knowledge.svg';
import plazaSkillIcon from '../../assets/icons/plaza-skill.svg';
import plazaSopIcon from '../../assets/icons/plaza-sop.svg';
import plazaToolIcon from '../../assets/icons/plaza-tool.svg';
import EmployeeAvatar from '../EmployeeAvatar';
import type { AgentProfileRead } from '../../types';

import PlatformEmployeeCard, { type PlatformStat } from './PlatformEmployeeCard';
import PlatformResourceCard, { type PlatformResourceAccent } from './PlatformResourceCard';

/** 广场资源种类枚举。 */
export type PlatformDetailKind = 'agents' | 'knowledge' | 'general-skills' | 'skills' | 'tools';

/** 单个广场资源项的数据结构。 */
export type PlatformDetailItem = {
  /** 资源唯一 ID。 */
  id: string;
  /** 资源标题。 */
  title: string;
  /** 资源描述。 */
  description: string;
  /** 元信息文本（如"12M / 6个片段"）。 */
  meta: string;
  /** 标签数组。 */
  tags: string[];
  /** 当 kind 为 agents 时关联的员工档案数据。 */
  agent?: AgentProfileRead;
};

/** 各资源种类对应的图标资源路径。 */
const PLATFORM_RESOURCE_ICON: Partial<Record<PlatformDetailKind, string>> = {
  knowledge: plazaKnowledgeIcon,
  'general-skills': plazaSkillIcon,
  skills: plazaSopIcon,
  tools: plazaToolIcon,
};

/** 各资源种类对应的主题强调色。 */
const PLATFORM_ACCENT: Partial<Record<PlatformDetailKind, PlatformResourceAccent>> = {
  knowledge: 'green',
  'general-skills': 'indigo',
  skills: 'blue',
  tools: 'orange',
};

export type PlatformKindDetailViewProps = {
  /** 当前视图的资源种类。 */
  kind: PlatformDetailKind;
  /** 页面标题。 */
  title: string;
  /** 页面副标题。 */
  subtitle: string;
  /** 计数单位标签（如"员工 / 知识库"）。 */
  countLabel: string;
  /** 筛选信号标签数组。 */
  signals: string[];
  /** 页面图标组件。 */
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  /** 资源列表数据。 */
  items: PlatformDetailItem[];
  /** 是否正在加载。 */
  loading: boolean;
  /** 根据员工档案计算统计指标的函数。 */
  employeeStats: (agent: AgentProfileRead) => PlatformStat[];
  /** 返回广场主页回调。 */
  onBack: () => void;
  /** 刷新数据回调。 */
  onRefresh: () => void;
  /** 打开某一项详情的回调。 */
  onOpenItem: (item: PlatformDetailItem) => void;
  /** 登出回调。 */
  onLogout?: () => void;
  /** 当前用户名。 */
  userName?: string;
};

/**
 * 列表加载骨架占位组件。
 *
 * @param kind - 资源种类，决定骨架方块的高度（员工卡片更高）
 * @returns 8 个骨架方块的网格布局
 */
function DetailSkeleton({ kind }: { kind: PlatformDetailKind }) {
  const cardHeight = kind === 'agents' ? 'h-[140px]' : 'h-[112px]';
  return (
    <div className="grid grid-cols-1 gap-[16px] sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5">
      {Array.from({ length: 8 }, (_, index) => (
        <div
          key={index}
          className={cn(
            'w-full animate-pulse rounded-[20px] border-[0.5px] border-[#f0f1f5] bg-[#f6f6f6]',
            cardHeight,
          )}
        />
      ))}
    </div>
  );
}

/**
 * 开放广场单模块全量列表视图组件。
 *
 * 数据流转：
 * 1. 父组件传入 `items` 全量数据和 `loading` 状态
 * 2. 组件内部维护 `searchText`，通过 `useMemo` 对 items 做本地过滤
 * 3. 根据 `kind` 决定渲染员工卡片还是资源卡片
 * 4. 用户点击卡片时通过 `onOpenItem` 回调通知父组件
 *
 * @param props - 组件属性，参见 {@link PlatformKindDetailViewProps}
 * @returns 全量列表视图的 JSX 元素
 */
export default function PlatformKindDetailView({
  kind,
  title,
  subtitle,
  countLabel,
  signals,
  icon: PlatformIcon,
  items,
  loading,
  employeeStats,
  onBack,
  onRefresh,
  onOpenItem,
  onLogout,
  userName,
}: PlatformKindDetailViewProps) {
  const [searchText, setSearchText] = useState('');

  // 本地搜索过滤：对标题、描述、元信息、标签做大小写不敏感的模糊匹配
  const filteredItems = useMemo(() => {
    const keyword = searchText.trim().toLowerCase();
    if (!keyword) return items;
    return items.filter((item) => [
      item.title,
      item.description,
      item.meta,
      item.tags.join(' '),
    ].some((value) => value.toLowerCase().includes(keyword)));
  }, [items, searchText]);

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]" aria-busy={loading}>
      <AppHeader
        onLogout={onLogout}
        userName={userName}
        title={title}
        description={subtitle}
      />

      {/* 顶部操作按钮：返回广场 + 刷新 */}
      <div className="mt-[20px] mb-[16px] flex flex-wrap justify-end gap-[16px]">
        <UIButton variant="outline" onClick={onBack} className={RETURN_BUTTON_CLASS}>
          <IconArrowRight className="size-3.5 rotate-180" />
          返回开放广场
        </UIButton>
        <UIButton
          variant="outline"
          onClick={onRefresh}
          disabled={loading}
          className={RETURN_BUTTON_CLASS}
        >
          <IconRefresh className={cn('size-[14px]', loading && 'animate-spin')} />
          刷新
        </UIButton>
      </div>

      <div className="flex flex-col gap-[24px] rounded-[20px] bg-white p-[18px_18px_24px_18px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        {/* 统计卡片 */}
        <div className="flex flex-wrap items-stretch gap-[20px]" aria-label={`${title}统计`}>
          <StatCard value={items.length} label={countLabel} className="max-w-[220px]" />
        </div>

        <div className="flex flex-col gap-[18px]">
          {/* 模块标题行 */}
          <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
            <PlatformIcon className="size-[14px] shrink-0" />
            <span className="text-[14px] font-normal leading-none">{title}</span>
          </div>

          {/* 筛选信号标签 */}
          {signals.length > 0 && (
            <div className="flex flex-wrap items-center gap-[6px] px-[12px]">
              {signals.map((signal) => (
                <span
                  key={signal}
                  className="rounded-[20px] border-[0.5px] border-[#e3e7f1] px-[8px] py-[2px] text-[10px] leading-[normal] text-[#757f9c]"
                >
                  {signal}
                </span>
              ))}
            </div>
          )}

          {/* 搜索框 */}
          <label className="flex h-[34px] w-full max-w-[360px] items-center gap-[8px] overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] transition-colors focus-within:border-[#18181a]">
            <IconSearch className="size-[14px] shrink-0 text-[#858b9c]" />
            <input
              value={searchText}
              placeholder={`搜索${countLabel}`}
              onChange={(event) => setSearchText(event.target.value)}
              className="min-w-0 flex-1 border-0 bg-transparent text-[12px] text-[#18181a] outline-none placeholder:text-[#858b9c]"
            />
          </label>

          {/* 内容区：加载骨架 / 空状态 / 卡片网格 */}
          {loading ? (
            <DetailSkeleton kind={kind} />
          ) : filteredItems.length === 0 ? (
            // 空状态：区分"全量为空"和"搜索无结果"两种文案
            <div className="grid min-h-[180px] w-full place-items-center content-center gap-[10px] rounded-[18px] border border-dashed border-[#dfe4ec] bg-[#fbfcfd] px-[20px] py-[40px] text-center font-bold text-[#8b94aa]">
              <IconSearch className="size-[20px] shrink-0" />
              <span>{items.length === 0 ? '暂无开放内容' : '没有匹配的广场内容'}</span>
            </div>
          ) : kind === 'agents' ? (
            // 数字员工：渲染员工卡片
            <div className="grid grid-cols-1 gap-[16px] sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5">
              {filteredItems.map((item) => item.agent && (
                <PlatformEmployeeCard
                  key={item.id}
                  avatar={(
                    <EmployeeAvatar
                      agent={item.agent}
                      width={50}
                      height={59}
                      fit="contain"
                      objectPosition="center bottom"
                      className="overflow-visible! rounded-none! border-0! bg-transparent! bg-none! shadow-none! after:hidden!"
                    />
                  )}
                  name={item.title}
                  role={item.meta}
                  online={item.agent.status === 'active'}
                  description={item.description}
                  stats={employeeStats(item.agent)}
                  onOpen={() => onOpenItem(item)}
                />
              ))}
            </div>
          ) : (
            // 其他资源：渲染资源卡片（知识库 / 技能 / SOP / 工具）
            <div className="grid grid-cols-1 gap-[16px] sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5">
              {filteredItems.map((item) => (
                <PlatformResourceCard
                  key={item.id}
                  icon={PLATFORM_RESOURCE_ICON[kind]
                    ? <img src={PLATFORM_RESOURCE_ICON[kind]} alt="" className="size-[32px] shrink-0 object-contain" />
                    : undefined}
                  accent={PLATFORM_ACCENT[kind]}
                  title={item.title}
                  meta={item.meta}
                  description={item.description}
                  tags={item.tags.slice(0, 2)}
                  onClick={() => onOpenItem(item)}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

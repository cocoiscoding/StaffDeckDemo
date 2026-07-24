/**
 * @file 开放广场组件模块统一导出入口
 * @description
 * 本文件是 openPlatform 目录的桶文件（barrel file），将开放广场相关的所有
 * 公开组件和类型统一从此处导出，方便外部以扁平化的路径引用，例如：
 * `import { PlatformColumn, PlatformEmployeeCard } from '@/components/openPlatform'`。
 *
 * 开放广场是企业端的核心模块之一，聚合了数字员工、知识库、技能、SOP 和工具
 * 五类资源的浏览与详情查看能力。本目录下的每个组件分别负责：
 * - PlatformColumn：列布局外壳（头部 + 筛选 + 卡片列表 + 查看全部）
 * - PlatformEmployeeCard / Drawer：数字员工卡片及其详情抽屉
 * - PlatformResourceCard / Drawer：通用资源卡片及其详情抽屉
 * - PlatformKindDetailView：单模块全量列表视图（含搜索、统计）
 */

export { default as PlatformColumn } from './PlatformColumn';
export type { PlatformColumnProps } from './PlatformColumn';
export { default as PlatformEmployeeCard } from './PlatformEmployeeCard';
export type { PlatformEmployeeCardProps, PlatformStat } from './PlatformEmployeeCard';
export { default as PlatformEmployeeDrawer } from './PlatformEmployeeDrawer';
export type { PlatformEmployeeDrawerProps } from './PlatformEmployeeDrawer';
export { default as PlatformKindDetailView } from './PlatformKindDetailView';
export type { PlatformDetailItem, PlatformDetailKind, PlatformKindDetailViewProps } from './PlatformKindDetailView';
export { default as PlatformResourceCard } from './PlatformResourceCard';
export type { PlatformResourceCardProps, PlatformResourceAccent } from './PlatformResourceCard';
export { platformResourceAccentStyles } from './PlatformResourceCard';
export { default as PlatformResourceDrawer } from './PlatformResourceDrawer';
export type { PlatformResourceDrawerProps } from './PlatformResourceDrawer';

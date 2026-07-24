/**
 * @file 图标组件统一导出模块。
 *
 * 将项目自有的 `StaffdeckIcon` 图标库映射为与 Ant Design 图标同名的导出组件，
 * 使得业务代码可以无缝替换图标库而无需修改引用路径。
 *
 * 核心概念：
 * - **Sd1AntIcon**：内部基础包装组件，接收图标名称（`name`）、旋转角度（`rotate`）、
 *   旋转动画（`spin`）等属性，转发给 `StaffdeckIcon`。
 * - **命名导出**：每个 `XxxOutlined` / `XxxFilled` 导出对应一个固定 `name` 的 `Sd1AntIcon` 实例，
 *   部分图标通过 `rotate` 实现方向变体（如箭头系列）。
 */

import type { CSSProperties } from 'react';
import StaffdeckIcon, { type StaffdeckIconName } from './components/StaffdeckIcon';

/** 图标组件的通用属性 */
type IconProps = {
  /** CSS 类名 */
  className?: string;
  /** 旋转角度（度），0 为不旋转 */
  rotate?: number;
  /** 是否启用无限旋转动画（用于 loading 状态） */
  spin?: boolean;
  /** 内联样式 */
  style?: CSSProperties;
  /** 其他透传给底层 SVG 的属性 */
  [key: string]: unknown;
};

/**
 * StaffDeck 图标基础包装组件。
 *
 * 根据 `name` 选择对应的 SVG 图标，合并 `rotate` 旋转和 `spin` 动画。
 * 旋转通过 CSS `transform: rotate(Ndeg)` 实现，会与传入的 `style.transform` 合并。
 *
 * @param props.name    StaffdeckIcon 图标名称
 * @param props.rotate  旋转角度（默认 0）
 * @param props.spin    是否旋转动画（默认 false）
 * @param props.className 额外的 CSS 类名
 * @param props.style   内联样式
 * @returns 渲染后的图标 JSX 元素
 */
function Sd1AntIcon({ name, rotate = 0, spin = false, className = '', style }: IconProps & { name: StaffdeckIconName }) {
  // 合并 style 上的 transform 和 rotate 属性
  const transform = [style?.transform, rotate ? `rotate(${rotate}deg)` : ''].filter(Boolean).join(' ');
  return (
    <StaffdeckIcon
      name={name}
      className={`${spin ? 'sd1-icon-spin ' : ''}${className}`.trim()}
      style={{ ...style, transform: transform || undefined }}
    />
  );
}

// 以下为各业务图标的具名导出，每个映射到 StaffdeckIcon 的一个内部图标名称。
// 箭头系列通过 rotate 实现不同方向：RightOutlined=0°, DownOutlined=90°, ArrowLeftOutlined=180°

export const ApiOutlined = (props: IconProps) => <Sd1AntIcon name="model" {...props} />;
export const AppstoreOutlined = (props: IconProps) => <Sd1AntIcon name="grid" {...props} />;
export const ArrowLeftOutlined = (props: IconProps) => <Sd1AntIcon name="arrow" rotate={180} {...props} />;
export const AuditOutlined = (props: IconProps) => <Sd1AntIcon name="file" {...props} />;
export const BranchesOutlined = (props: IconProps) => <Sd1AntIcon name="branch" {...props} />;
export const CheckCircleFilled = (props: IconProps) => <Sd1AntIcon name="check" {...props} />;
export const CheckCircleOutlined = (props: IconProps) => <Sd1AntIcon name="check" {...props} />;
export const CheckOutlined = (props: IconProps) => <Sd1AntIcon name="check" {...props} />;
export const ClockCircleOutlined = (props: IconProps) => <Sd1AntIcon name="clock" {...props} />;
export const CloseCircleOutlined = (props: IconProps) => <Sd1AntIcon name="close" {...props} />;
export const CloseOutlined = (props: IconProps) => <Sd1AntIcon name="close" {...props} />;
export const CloudOutlined = (props: IconProps) => <Sd1AntIcon name="cloud" {...props} />;
export const CloudSyncOutlined = (props: IconProps) => <Sd1AntIcon name="refresh" {...props} />;
export const CodeOutlined = (props: IconProps) => <Sd1AntIcon name="code" {...props} />;
export const DatabaseOutlined = (props: IconProps) => <Sd1AntIcon name="database" {...props} />;
export const DeleteOutlined = (props: IconProps) => <Sd1AntIcon name="trash" {...props} />;
export const DesktopOutlined = (props: IconProps) => <Sd1AntIcon name="desktop" {...props} />;
export const DownOutlined = (props: IconProps) => <Sd1AntIcon name="arrow" rotate={90} {...props} />;
export const DownloadOutlined = (props: IconProps) => <Sd1AntIcon name="download" {...props} />;
export const EditOutlined = (props: IconProps) => <Sd1AntIcon name="edit" {...props} />;
export const ExperimentOutlined = (props: IconProps) => <Sd1AntIcon name="tool" {...props} />;
export const EyeOutlined = (props: IconProps) => <Sd1AntIcon name="eye" {...props} />;
export const FileAddOutlined = (props: IconProps) => <Sd1AntIcon name="plus" {...props} />;
export const FileMarkdownOutlined = (props: IconProps) => <Sd1AntIcon name="file" {...props} />;
export const FileSearchOutlined = (props: IconProps) => <Sd1AntIcon name="file" {...props} />;
export const FileTextOutlined = (props: IconProps) => <Sd1AntIcon name="file" {...props} />;
export const FolderOpenOutlined = (props: IconProps) => <Sd1AntIcon name="folder" {...props} />;
export const GithubOutlined = (props: IconProps) => <Sd1AntIcon name="code" {...props} />;
export const HistoryOutlined = (props: IconProps) => <Sd1AntIcon name="history" {...props} />;
export const IdcardOutlined = (props: IconProps) => <Sd1AntIcon name="user" {...props} />;
export const InboxOutlined = (props: IconProps) => <Sd1AntIcon name="inbox" {...props} />;
export const InfoCircleOutlined = (props: IconProps) => <Sd1AntIcon name="info" {...props} />;
export const LoadingOutlined = (props: IconProps) => <Sd1AntIcon name="refresh" spin {...props} />;
export const LockOutlined = (props: IconProps) => <Sd1AntIcon name="lock" {...props} />;
export const MessageOutlined = (props: IconProps) => <Sd1AntIcon name="chat" {...props} />;
export const MoonOutlined = (props: IconProps) => <Sd1AntIcon name="moon" {...props} />;
export const MoreOutlined = (props: IconProps) => <Sd1AntIcon name="more" {...props} />;
export const PauseCircleOutlined = (props: IconProps) => <Sd1AntIcon name="pause" {...props} />;
export const PlayCircleOutlined = (props: IconProps) => <Sd1AntIcon name="play" {...props} />;
export const PlusOutlined = (props: IconProps) => <Sd1AntIcon name="plus" {...props} />;
export const ProfileOutlined = (props: IconProps) => <Sd1AntIcon name="filter" {...props} />;
export const ReloadOutlined = (props: IconProps) => <Sd1AntIcon name="refresh" {...props} />;
export const RightOutlined = (props: IconProps) => <Sd1AntIcon name="arrow" {...props} />;
export const RollbackOutlined = (props: IconProps) => <Sd1AntIcon name="history" {...props} />;
export const SaveOutlined = (props: IconProps) => <Sd1AntIcon name="save" {...props} />;
export const SearchOutlined = (props: IconProps) => <Sd1AntIcon name="search" {...props} />;
export const SendOutlined = (props: IconProps) => <Sd1AntIcon name="send" {...props} />;
export const SolutionOutlined = (props: IconProps) => <Sd1AntIcon name="spark" {...props} />;
export const StopOutlined = (props: IconProps) => <Sd1AntIcon name="stop" {...props} />;
export const SunOutlined = (props: IconProps) => <Sd1AntIcon name="sun" {...props} />;
export const SyncOutlined = (props: IconProps) => <Sd1AntIcon name="refresh" {...props} />;
export const TeamOutlined = (props: IconProps) => <Sd1AntIcon name="user" {...props} />;
export const ToolOutlined = (props: IconProps) => <Sd1AntIcon name="tool" {...props} />;
export const UploadOutlined = (props: IconProps) => <Sd1AntIcon name="upload" {...props} />;
export const UserOutlined = (props: IconProps) => <Sd1AntIcon name="user" {...props} />;
export const UsergroupAddOutlined = (props: IconProps) => <Sd1AntIcon name="user" {...props} />;
export const WarningOutlined = (props: IconProps) => <Sd1AntIcon name="warning" {...props} />;

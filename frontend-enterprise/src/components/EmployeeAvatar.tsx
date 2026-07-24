/**
 * @file EmployeeAvatar.tsx
 * @module components/EmployeeAvatar
 * @description 数字员工头像组件。根据员工的头像配置（预设头像或上传图片）
 *              渲染对应头像，支持自定义尺寸、圆角、填充模式等参数。
 */

import type { CSSProperties } from 'react';

import {
  DEFAULT_AVATAR_PRESET,
  employeeAvatarImage,
  employeeProfile,
  type EmployeeProfile,
} from '../employee';
import type { AgentProfileRead } from '../types';

/**
 * 头像配置子集类型，仅包含头像渲染所需的字段。
 */
type AvatarProfile = Pick<EmployeeProfile, 'avatarKind' | 'avatarImage' | 'avatarPreset' | 'avatarText' | 'avatarTone'>;

/**
 * 数字员工头像组件的属性定义。
 */
type EmployeeAvatarProps = {
  /** 数字员工数据。当未提供 profile 时，从头像数据中派生。 */
  agent?: AgentProfileRead | null;
  /** 预解析的头像配置。省略时从 agent 派生。 */
  profile?: AvatarProfile;
  /** 正方形宽高的简写（px）。当 width/height 未提供时使用。 */
  size?: number;
  /** 显式宽度（px），优先于 size。 */
  width?: number;
  /** 显式高度（px），优先于 size。 */
  height?: number;
  /** 圆角覆盖（px 或任意 CSS 长度值）。 */
  radius?: number | string;
  /** 图片在容器中的填充方式。cover 为填满且不变形。 */
  fit?: CSSProperties['objectFit'];
  /** 图片在容器中的对齐方式，例如 center bottom。 */
  objectPosition?: CSSProperties['objectPosition'];
  /** 附加 CSS 类名。 */
  className?: string;
  /** 行内样式覆盖。 */
  style?: CSSProperties;
};

/**
 * 数字员工头像组件。根据 profile 配置渲染预设头像或上传的图片。
 * 支持灵活的尺寸、圆角、填充模式与对齐方式控制。
 *
 * @param props - 头像组件属性
 * @param props.agent - 数字员工数据
 * @param props.profile - 预解析的头像配置
 * @param props.size - 正方形尺寸（px），默认 54
 * @param props.width - 显式宽度
 * @param props.height - 显式高度
 * @param props.radius - 圆角
 * @param props.fit - 填充模式，默认 cover
 * @param props.objectPosition - 对齐方式，默认 center
 * @param props.className - 附加类名
 * @param props.style - 行内样式
 * @returns 渲染好的头像 span 元素
 */
export default function EmployeeAvatar({
  agent,
  profile: profileOverride,
  size = 54,
  width,
  height,
  radius,
  fit = 'cover',
  objectPosition = 'center',
  className = '',
  style,
}: EmployeeAvatarProps) {
  // 若提供了预解析的 profile 则直接使用，否则从 agent 数据中派生
  const profile = profileOverride || employeeProfile(agent);

  // 组合最终类名
  const className_ = [
    'employee-avatar',
    className,
  ]
    .filter(Boolean)
    .join(' ');

  // 容器样式：尺寸 + 圆角 + 自定义样式覆盖
  const boxStyle: CSSProperties = {
    width: width ?? size,
    height: height ?? size,
    ...(radius != null ? { borderRadius: radius } : null),
    ...style,
  };

  // 图片样式：锁定为填满容器。
  // cover 填充且不变形，同时重置 transform/maxWidth 以消除各上下文中的覆盖样式。
  const imageStyle: CSSProperties = {
    width: '100%',
    height: '100%',
    maxWidth: 'none',
    objectFit: fit,
    objectPosition,
    transform: 'none'
  };

  return (
    <span
      className={className_}
      style={boxStyle}
      aria-label={`${profile.avatarText || '员'}员工头像`}
    >
      <img src={employeeAvatarImage(profile)} alt="" style={imageStyle} />
    </span>
  );
}

/**
 * @file 开放广场资源卡片组件
 * @description
 * 广场中知识库、技能、SOP、工具四个模块共用的资源卡片（SD1 232:4923）。
 * 布局自上而下：彩色模块图标 + 标题 + 强调色元信息行，两行描述文本，
 * 以及一排彩色标签胶囊。整张卡片为可点击按钮，点击触发 onClick 回调。
 *
 * 支持通过 `accent` 属性切换四种主题色，使不同模块的卡片具有视觉区分度。
 */

import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import IconFolder from '../../assets/icons/cap-folder.svg?react';

/**
 * 各模块的强调色类型（SD1 232:4634 系列）。
 * - green：知识库（默认）
 * - blue：SOP
 * - indigo：技能
 * - orange：工具
 */
export type PlatformResourceAccent = 'green' | 'blue' | 'indigo' | 'orange';

/**
 * 强调色到具体 Tailwind 类名的映射表。
 * 每种强调色包含 meta（元信息行文字色）和 tag（标签胶囊背景+文字色）两组样式。
 */
const ACCENT_STYLES: Record<PlatformResourceAccent, { meta: string; tag: string }> = {
  green: { meta: 'text-[#2cb360]', tag: 'bg-[#e9f7ef] text-[#2cb360]' },
  blue: { meta: 'text-[#27c9ff]', tag: 'bg-[#c4f1ff] text-[#25c7ff]' },
  indigo: { meta: 'text-[#1a71ff]', tag: 'bg-[#e8f0ff] text-[#1a71ff]' },
  orange: { meta: 'text-[#ff7f00]', tag: 'bg-[#fff2e5] text-[#ff7f00]' },
};

export const platformResourceAccentStyles = ACCENT_STYLES;

export type PlatformResourceCardProps = {
  /** 资源标题。 */
  title: ReactNode;
  /** 标题下方的强调色元信息行，例如"12M / 6个片段"。 */
  meta: ReactNode;
  /** 资源描述，最多两行。 */
  description: ReactNode;
  /** 标签数组。 */
  tags?: string[];
  /** 36px 完整图标视觉。省略时显示默认的文件夹图标方块。 */
  icon?: ReactNode;
  /** 模块强调色，控制元信息行和标签胶囊的颜色。默认为 green（知识库）。 */
  accent?: PlatformResourceAccent;
  /** 点击卡片时的回调。 */
  onClick?: () => void;
  className?: string;
};

/**
 * 广场资源卡片组件。
 *
 * 该组件被知识库、技能、SOP、工具四个模块复用，通过 `accent` 属性区分不同模块的主题色。
 * 布局结构：
 * 1. 图标 + 标题 + 强调色元信息行（水平排列）
 * 2. 两行描述文本
 * 3. 彩色标签胶囊行
 *
 * @param props - 组件属性，参见 {@link PlatformResourceCardProps}
 * @returns 资源卡片的 JSX 元素
 */
export default function PlatformResourceCard({
  title,
  meta,
  description,
  tags,
  icon,
  accent = 'green',
  onClick,
  className,
}: PlatformResourceCardProps) {
  const accentStyles = ACCENT_STYLES[accent];
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'relative flex h-[112px] w-full shrink-0 flex-col items-center justify-center overflow-hidden rounded-[14px] border-[0.5px] border-[#f6f6f6] bg-white p-[4px] text-left backdrop-blur-[1.835px] transition-shadow hover:shadow-[0_8px_20px_rgba(15,23,42,0.06)]',
        '',
        className,
      )}
    >
      <div className="flex w-full flex-col items-start gap-[6px] px-[8px]">
        {/* 图标 + 标题 + 元信息行 */}
        <div className="flex w-full items-center gap-[4px]">
          {icon ?? (
            // 未传入图标时使用默认的文件夹图标方块
            <span className="grid size-[32px] shrink-0 place-items-center rounded-[10px] bg-[#f2f4f8] text-[#8a94a6]">
              <IconFolder className="size-[18px]" />
            </span>
          )}
          <div className="flex min-w-0 flex-1 flex-col gap-[4px]">
            <p className="truncate text-[12px] font-medium text-[#464c5e]">{title}</p>
            <p className={cn('truncate text-[10px]', accentStyles.meta)}>{meta}</p>
          </div>
        </div>

        {/* 两行描述文本 */}
        <p className="line-clamp-2 h-[26px] w-full text-[10px] leading-[13px] text-[#757f9c]">
          {description}
        </p>

        {/* 标签胶囊行 */}
        {tags && tags.length > 0 && (
          <div className="flex flex-wrap items-center gap-[6px]">
            {tags.map((tag) => (
              <span
                key={tag}
                className={cn(
                  'inline-flex items-center rounded-[90px] px-[8px] py-[2px] text-[8px] leading-[normal]',
                  accentStyles.tag,
                )}
              >
                {tag}
              </span>
            ))}
          </div>
        )}
      </div>
    </button>
  );
}

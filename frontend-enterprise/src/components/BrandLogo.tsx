/**
 * @file BrandLogo.tsx
 * @module components/BrandLogo
 * @description 品牌徽标组件，渲染 StaffDeck 的 Logo 图标 + 文字组合。
 *              可通过 markOnly 属性仅显示图标标记，或同时展示文字标识。
 */

import { cn } from '@/lib/utils';
import logoMark from '../assets/logo.png';

/**
 * BrandLogo 组件的属性定义。
 */
export type BrandLogoProps = {
  /** 是否仅显示 Logo 图标标记，隐藏 "StaffDeck" 文字标识。 */
  markOnly?: boolean;
  /** Logo 图标标记的方形尺寸（px）。 */
  markSize?: number;
  /** 附加到根容器上的 CSS 类名。 */
  className?: string;
  /** 额外应用到文字标识包裹层的 CSS 类名（例如响应式隐藏）。 */
  wordmarkClassName?: string;
};

/**
 * 品牌徽标组件（Logo 图标 + "StaffDeck" 文字标识）。
 * 当 markOnly 为 true 时仅渲染图标标记部分。
 *
 * @param props - 组件属性
 * @param props.markOnly - 是否仅显示图标标记，默认 false
 * @param props.markSize - 图标尺寸（px），默认 28
 * @param props.className - 根容器附加类名
 * @param props.wordmarkClassName - 文字标识包裹层附加类名
 * @returns 品牌徽标的 JSX 元素
 */
export default function BrandLogo({
  markOnly = false,
  markSize = 28,
  className,
  wordmarkClassName,
}: BrandLogoProps) {
  return (
    <span className={cn('flex items-center gap-[8px] overflow-hidden p-[4px]', className)}>
      <img
        src={logoMark}
        alt="广通服数字员工系统"
        className="shrink-0"
        style={{ width: markSize, height: markSize }}
      />
      {!markOnly && (
        <span className={cn('flex flex-col items-center gap-[2px] leading-none', wordmarkClassName)}>
          {/* <span className="text-[12px] font-semibold leading-none text-[#0f136c]">
            OpenBMB
          </span> */}
          <strong className="text-[17px] font-semibold leading-none text-[#18181a]">
            广通服数字员工系统
          </strong>
        </span>
      )}
    </span>
  );
}

/**
 * @file 时区处理工具模块。
 *
 * 负责后端 UTC 时间戳在前端的解析与本地化格式化展示。
 *
 * 核心概念：
 * - **客户端时区探测**：通过 `Intl.DateTimeFormat` 获取浏览器所在时区，失败时回退到 `Asia/Shanghai`。
 * - **后端时间解析**：后端返回的时间可能带或不带时区标识，统一补全为 UTC 后解析。
 * - **本地化展示**：结合当前国际化 locale（中/英）格式化日期时间字符串。
 */

import { getDateLocale } from '@/i18n';

/** 时区探测失败时的回退时区 */
const FALLBACK_TIME_ZONE = 'Asia/Shanghai';

/**
 * 获取浏览器当前所在的 IANA 时区标识。
 *
 * @returns 时区字符串，如 `Asia/Shanghai`、`America/New_York`；探测失败时回退到 `Asia/Shanghai`
 */
export function getClientTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || FALLBACK_TIME_ZONE;
  } catch {
    return FALLBACK_TIME_ZONE;
  }
}

/**
 * 解析后端返回的时间字符串为 Date 对象。
 *
 * 后端时间可能为以下形式：
 * - 带 UTC 标识：`2024-01-01T12:00:00Z` 或 `...+08:00` — 直接解析；
 * - 无时区标识：`2024-01-01T12:00:00` — 补全 `Z` 后缀视为 UTC 时间再解析。
 *
 * @param value 后端时间字符串，为空时返回 Invalid Date
 * @returns 对应的 Date 对象
 */
export function parseBackendDateTime(value?: string): Date {
  const text = String(value || '').trim();
  if (!text) return new Date('');
  // 检测是否已带时区标识（Z / +08:00）
  if (/[zZ]|[+-]\d{2}:\d{2}$/.test(text)) return new Date(text);
  // 无时区标识时补全为 UTC
  return new Date(`${text}Z`);
}

/**
 * 将后端时间字符串格式化为客户端本地化的日期时间字符串。
 *
 * @param value    后端时间字符串
 * @param emptyText 为空或无效时的占位文本，默认为 `'-'`
 * @returns 格式化后的本地时间字符串（24小时制，带时区转换），无效时返回 `emptyText`
 */
export function formatClientDateTime(value?: string, emptyText = '-'): string {
  if (!value) return emptyText;
  const date = parseBackendDateTime(value);
  // 校验日期是否有效
  if (Number.isNaN(date.getTime())) return emptyText;
  return date.toLocaleString(getDateLocale(), {
    hour12: false,
    timeZone: getClientTimeZone(),
  });
}

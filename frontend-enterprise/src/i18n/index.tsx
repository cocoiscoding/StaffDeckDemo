/**
 * @file 国际化引擎（I18n Engine）模块。
 *
 * 该模块实现了一套基于 **运行时 DOM 自动翻译** 的中英双语国际化方案，
 * 核心思路是以中文为源语言（Source），通过 en.json 翻译目录（Catalog）自动将
 * DOM 中的中文文本和属性翻译为英文。
 *
 * 核心概念：
 * - **源语言（zh-CN）**：代码中直接书写的中文即为源文本，无需翻译函数包裹。
 * - **目标语言（en-US）**：翻译映射表存储在 `en.json` 中，切换语言时自动替换 DOM 文本。
 * - **DOM 自动翻译**：通过 `MutationObserver` 监听 DOM 变化，对新插入的文本节点和属性
 *   自动执行翻译，无需在每个组件中手动调用 `t()`。
 * - **模板匹配**：支持带占位符 `{0}` `{1}` 的模板翻译，先精确匹配再尝试正则模板匹配。
 * - **双向可逆**：切换回中文时，通过 `WeakMap` 记录的原始文本自动还原。
 *
 * 两种使用方式：
 * 1. **声明式**：直接在 JSX 中写中文，DOM 自动翻译（适用于大多数静态文本）。
 * 2. **命令式**：调用 `t()` 函数手动翻译（适用于动态拼接的字符串）。
 */

import {
  createContext,
  useCallback,
  useContext,
  useLayoutEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

// 英文翻译目录（source 中文 → target 英文 的映射表）
import englishCatalog from './en.json';

/** 应用支持的语言环境 */
export type AppLocale = 'zh-CN' | 'en-US';

/**
 * I18n 上下文值。通过 React Context 向子组件提供语言状态和操作方法。
 */
type I18nContextValue = {
  /** 当前语言 */
  locale: AppLocale;
  /** 设置当前语言 */
  setLocale: (locale: AppLocale) => void;
  /** 在中/英之间切换 */
  toggleLocale: () => void;
  /**
   * 命令式翻译函数。
   * @param source  中文源文本（可含 `{0}` 占位符）
   * @param values  占位符替换值
   * @returns 当前语言下的翻译文本
   */
  t: (source: string, values?: Record<string | number, string | number>) => string;
};

/**
 * 预编译的模板。将含占位符的源文本编译为正则表达式，加速运行时匹配。
 */
type CompiledTemplate = {
  /** 由源文本编译而成的正则表达式 */
  pattern: RegExp;
  /** 占位符序号映射（匹配组序号 → 占位符编号） */
  placeholderOrder: number[];
  /** 翻译目标文本（含占位符） */
  target: string;
};

/** localStorage 中存储语言偏好的键名 */
const STORAGE_KEY = 'staffdeck_locale';
/** 英文翻译目录，类型化为键值对 */
const CATALOG = englishCatalog as Record<string, string>;
/** 需要自动翻译的 HTML 属性白名单 */
const SAFE_ATTRIBUTES = ['placeholder', 'aria-label', 'title', 'alt', 'data-placeholder'] as const;
/** 占位符匹配正则：{0}、{1}、{2} ... */
const TEMPLATE_TOKEN = /\{(\d+)\}/g;
/** WeakMap：记录每个 Text 节点的原始中文文本（用于切换回中文时还原） */
const textOriginals = new WeakMap<Text, string>();
/** WeakMap：记录每个元素的属性原始中文文本 */
const attributeOriginals = new WeakMap<Element, Map<string, string>>();
/** 翻译结果缓存，避免重复匹配 */
const translationCache = new Map<string, string | null>();

/**
 * 初始化语言：从 localStorage 读取用户偏好，默认中文。
 *
 * @returns 初始语言环境
 */
function initialLocale(): AppLocale {
  if (typeof window === 'undefined') return 'zh-CN';
  return window.localStorage.getItem(STORAGE_KEY) === 'en-US' ? 'en-US' : 'zh-CN';
}

/** 模块级当前语言状态（供非 React 代码读取，如时区模块） */
let currentLocale: AppLocale = initialLocale();

/**
 * 反转义 HTML 实体（目录键名可能以 HTML 实体形式存储）。
 *
 * @param value 可能含 HTML 实体的字符串
 * @returns 反转义后的纯文本
 */
function decodeSourceEntities(value: string): string {
  return value
    .replace(/&gt;/g, '>')
    .replace(/&lt;/g, '<')
    .replace(/&amp;/g, '&')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'");
}

/**
 * 转义字符串中的正则特殊字符，用于安全地构建正则模式。
 *
 * @param value 原始字符串
 * @returns 转义后的正则安全字符串
 */
function escapePattern(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** 精确匹配表：源文本 → 翻译文本 */
const exactCatalog = new Map<string, string>();
/** 预编译的模板列表（含占位符的源文本编译而来） */
const compiledTemplates: CompiledTemplate[] = [];

// 模块加载时预编译翻译目录：将每条映射分别放入精确匹配表和模板列表
for (const [rawSource, target] of Object.entries(CATALOG)) {
  // 同时注册原始键名和 HTML 实体反转义后的键名
  const sources = new Set([rawSource, decodeSourceEntities(rawSource)]);
  for (const source of sources) {
    if (!source) continue;
    exactCatalog.set(source, target);
    // 无占位符的源文本只需精确匹配
    if (!TEMPLATE_TOKEN.test(source)) {
      TEMPLATE_TOKEN.lastIndex = 0;
      continue;
    }
    TEMPLATE_TOKEN.lastIndex = 0;
    // 将占位符替换为捕获组，构建完整正则
    const placeholderOrder: number[] = [];
    let cursor = 0;
    let pattern = '^';
    for (const match of source.matchAll(TEMPLATE_TOKEN)) {
      // 占位符之前的固定文本需转义
      pattern += escapePattern(source.slice(cursor, match.index));
      // 占位符位置替换为非贪婪捕获组
      pattern += '([\\s\\S]*?)';
      placeholderOrder.push(Number(match[1]));
      cursor = (match.index || 0) + match[0].length;
    }
    pattern += `${escapePattern(source.slice(cursor))}$`;
    compiledTemplates.push({ pattern: new RegExp(pattern), placeholderOrder, target });
  }
}

// 模板按模式长度降序排列，确保更长/更具体的模板优先匹配
compiledTemplates.sort((left, right) => right.pattern.source.length - left.pattern.source.length);

/**
 * 将占位符 `{n}` 替换为实际值。
 *
 * @param target 含占位符的目标文本
 * @param values 占位符替换值映射
 * @returns 插值后的最终文本
 */
function interpolate(target: string, values: Record<string | number, string | number>): string {
  return target.replace(TEMPLATE_TOKEN, (_, key: string) => String(values[key] ?? `{${key}}`));
}

/**
 * 翻译核心逻辑：先查精确匹配表，再逐个尝试模板匹配。
 *
 * 使用 `translationCache` 缓存翻译结果以加速重复查询。
 *
 * @param source 中文源文本
 * @returns 翻译后的英文文本；无匹配时返回 `null`
 */
function translateCore(source: string): string | null {
  // 优先查缓存
  if (translationCache.has(source)) return translationCache.get(source) || null;
  // 精确匹配
  const exact = exactCatalog.get(source);
  if (exact) {
    translationCache.set(source, exact);
    return exact;
  }
  // 模板匹配：逐个尝试预编译的正则模板
  for (const template of compiledTemplates) {
    const match = source.match(template.pattern);
    if (!match) continue;
    // 将捕获组按占位符顺序组装
    const values: Record<number, string> = {};
    template.placeholderOrder.forEach((placeholder, index) => {
      values[placeholder] = match[index + 1] || '';
    });
    const translated = interpolate(template.target, values);
    translationCache.set(source, translated);
    return translated;
  }
  // 无匹配，缓存 null
  translationCache.set(source, null);
  return null;
}

/**
 * 将文本拆分为前导空白、核心内容、尾部空白三部分。
 * 翻译时仅替换核心内容，保留原始空白格式。
 *
 * @param value 原始文本
 * @returns 拆分结果对象
 */
function splitWhitespace(value: string): { leading: string; core: string; trailing: string } {
  const leading = value.match(/^\s*/)?.[0] || '';
  const trailing = value.match(/\s*$/)?.[0] || '';
  return {
    leading,
    core: value.slice(leading.length, value.length - trailing.length),
    trailing,
  };
}

/**
 * 翻译文本但保留首尾空白。
 *
 * @param value 原始文本（可能含首尾空白）
 * @returns 翻译后的文本（空白保持不变）；无翻译时原样返回
 */
function translatePreservingWhitespace(value: string): string {
  const { leading, core, trailing } = splitWhitespace(value);
  if (!core) return value;
  const translated = translateCore(core);
  return translated ? `${leading}${translated}${trailing}` : value;
}

/**
 * 判断元素是否应被忽略翻译。
 *
 * 忽略以下元素：代码块、预格式化文本、脚本、样式、文本域、可编辑区域，
 * 以及标记了 `data-i18n-ignore` 的元素。
 *
 * @param element 待检查的 DOM 元素
 * @returns 是否忽略翻译
 */
function shouldIgnore(element: Element | null): boolean {
  return Boolean(
    element?.closest(
      '[data-i18n-ignore], code, pre, script, style, textarea, [contenteditable="true"]',
    ),
  );
}

/**
 * 判断元素的属性是否应被忽略翻译。
 *
 * @param element 待检查的 DOM 元素
 * @returns 是否忽略属性翻译
 */
function shouldIgnoreAttribute(element: Element): boolean {
  return Boolean(element.closest('[data-i18n-ignore], code, pre, script, style'));
}

/**
 * 翻译单个文本节点。
 *
 * 策略：
 * - **中文模式**：若当前显示的是翻译后的英文，且存有原始中文，则还原为中文。
 * - **英文模式**：记录原始中文，将文本替换为英文翻译。
 * - 通过比较 current/target/original 三者状态，避免重复翻译导致的"双重翻译"问题。
 *
 * @param node   文本节点
 * @param locale 当前语言
 */
function localizeTextNode(node: Text, locale: AppLocale): void {
  if (shouldIgnore(node.parentElement)) return;
  const current = node.data;
  const previousSource = textOriginals.get(node);
  const previousTarget = previousSource ? translatePreservingWhitespace(previousSource) : '';

  if (locale === 'zh-CN') {
    // 切回中文：若当前显示的是翻译结果，则还原原始中文
    if (previousSource && current === previousTarget && current !== previousSource) {
      node.data = previousSource;
    } else if (!previousSource || (current !== previousSource && current !== previousTarget)) {
      // 首次记录或内容已变更，更新原始文本记录
      textOriginals.set(node, current);
    }
    return;
  }

  // 英文模式
  let source = previousSource;
  if (!source || (current !== source && current !== previousTarget)) {
    source = current;
    textOriginals.set(node, source);
  }
  const target = translatePreservingWhitespace(source);
  if (target !== current) node.data = target;
}

/**
 * 翻译单个属性值。
 *
 * 逻辑与 `localizeTextNode` 类似，但操作对象是 HTML 属性。
 *
 * @param element 所属元素
 * @param name    属性名
 * @param locale  当前语言
 */
function localizeAttribute(element: Element, name: string, locale: AppLocale): void {
  if (shouldIgnoreAttribute(element)) return;
  const current = element.getAttribute(name);
  if (current == null) return;
  let originals = attributeOriginals.get(element);
  if (!originals) {
    originals = new Map();
    attributeOriginals.set(element, originals);
  }
  const previousSource = originals.get(name);
  const previousTarget = previousSource ? translatePreservingWhitespace(previousSource) : '';

  if (locale === 'zh-CN') {
    if (previousSource && current === previousTarget && current !== previousSource) {
      element.setAttribute(name, previousSource);
    } else if (!previousSource || (current !== previousSource && current !== previousTarget)) {
      originals.set(name, current);
    }
    return;
  }

  let source = previousSource;
  if (!source || (current !== source && current !== previousTarget)) {
    source = current;
    originals.set(name, source);
  }
  const target = translatePreservingWhitespace(source);
  if (target !== current) element.setAttribute(name, target);
}

/**
 * 翻译元素的白名单属性。
 *
 * @param element 待翻译的元素
 * @param locale  当前语言
 */
function localizeElement(element: Element, locale: AppLocale): void {
  for (const name of SAFE_ATTRIBUTES) localizeAttribute(element, name, locale);
}

/**
 * 递归翻译子树中的所有文本节点和元素属性。
 *
 * 使用 `TreeWalker` 遍历 DOM 树，逐个节点执行翻译。
 *
 * @param root   遍历根节点
 * @param locale 当前语言
 */
function localizeSubtree(root: Node, locale: AppLocale): void {
  // 根节点本身是文本节点时直接处理
  if (root.nodeType === Node.TEXT_NODE) {
    localizeTextNode(root as Text, locale);
    return;
  }
  // 仅处理元素节点和文档节点
  if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_NODE) return;
  if (root.nodeType === Node.ELEMENT_NODE) localizeElement(root as Element, locale);
  // 创建 TreeWalker 遍历所有子文本节点和元素节点
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
  let current = walker.nextNode();
  while (current) {
    if (current.nodeType === Node.TEXT_NODE) localizeTextNode(current as Text, locale);
    else localizeElement(current as Element, locale);
    current = walker.nextNode();
  }
}

/** I18n React Context 实例 */
const I18nContext = createContext<I18nContextValue | null>(null);

/**
 * 获取当前存储的语言环境（模块级，供非 React 代码使用）。
 *
 * @returns 当前语言环境
 */
export function getStoredLocale(): AppLocale {
  return currentLocale;
}

/**
 * 获取用于日期格式化的 locale 字符串。
 *
 * @returns `en-US` 或 `zh-CN`
 */
export function getDateLocale(): string {
  return currentLocale === 'en-US' ? 'en-US' : 'zh-CN';
}

/**
 * I18n Provider 组件。包裹应用根组件，提供语言上下文和 DOM 自动翻译能力。
 *
 * 功能：
 * - 管理 `locale` 状态并持久化到 localStorage；
 * - 提供 `setLocale`、`toggleLocale`、`t` 方法；
 * - 在 `locale` 变化时通过 `useLayoutEffect` 对整个 DOM 执行一次全量翻译；
 * - 通过 `MutationObserver` 持续监听 DOM 变化，对新插入的节点自动翻译。
 *
 * @param props.children 子组件树
 * @returns Provider 组件
 */
export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<AppLocale>(initialLocale);

  /**
   * 设置语言环境，同步更新模块状态、localStorage 和 DOM lang 属性。
   */
  const setLocale = useCallback((nextLocale: AppLocale) => {
    currentLocale = nextLocale;
    window.localStorage.setItem(STORAGE_KEY, nextLocale);
    document.documentElement.lang = nextLocale;
    setLocaleState(nextLocale);
  }, []);

  /** 在中英文之间切换 */
  const toggleLocale = useCallback(() => {
    setLocale(locale === 'zh-CN' ? 'en-US' : 'zh-CN');
  }, [locale, setLocale]);

  /**
   * 命令式翻译函数。
   * 中文模式下直接插值返回；英文模式下先翻译再插值。
   */
  const t = useCallback(
    (source: string, values: Record<string | number, string | number> = {}) => {
      if (locale === 'zh-CN') return interpolate(source, values);
      return interpolate(translateCore(source) || source, values);
    },
    [locale],
  );

  // 语言变化时执行全量翻译并启动 MutationObserver 监听后续 DOM 变化
  useLayoutEffect(() => {
    currentLocale = locale;
    document.documentElement.lang = locale;
    const root = document.documentElement;
    // 首次全量翻译
    localizeSubtree(root, locale);

    // 使用微任务批量处理 DOM 变更，避免高频回调
    let scheduled = false;
    let disposed = false;
    const pending = new Set<Node>();

    /** 执行批量翻译 */
    const flush = () => {
      scheduled = false;
      if (disposed) return;
      for (const node of pending) localizeSubtree(node, locale);
      pending.clear();
    };

    /** 将变更节点加入待处理队列 */
    const enqueue = (node: Node) => {
      pending.add(node);
      if (scheduled) return;
      scheduled = true;
      window.queueMicrotask(flush);
    };

    // 监听 DOM 变化：文本内容变更、属性变更、子节点新增
    const observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        if (mutation.type === 'characterData') enqueue(mutation.target);
        else if (mutation.type === 'attributes') enqueue(mutation.target);
        else mutation.addedNodes.forEach(enqueue);
      }
    });
    observer.observe(root, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
      attributeFilter: [...SAFE_ATTRIBUTES],
    });
    return () => {
      disposed = true;
      observer.disconnect();
      pending.clear();
    };
  }, [locale]);

  const value = useMemo<I18nContextValue>(
    () => ({ locale, setLocale, toggleLocale, t }),
    [locale, setLocale, t, toggleLocale],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

/**
 * 获取 I18n 上下文值的 Hook。
 *
 * @returns I18n 上下文值（locale、setLocale、toggleLocale、t）
 * @throws 若在 I18nProvider 外部使用则抛出错误
 */
export function useI18n(): I18nContextValue {
  const context = useContext(I18nContext);
  if (!context) throw new Error('useI18n must be used inside I18nProvider');
  return context;
}

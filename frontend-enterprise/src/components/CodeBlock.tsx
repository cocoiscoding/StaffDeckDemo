/**
 * @file CodeBlock.tsx
 * @module components/CodeBlock
 * @description 轻量级代码高亮组件。内置 JSON / Python / Markdown 三种语言的
 *              正则分词器，将代码文本拆分为带类型的 Token 并渲染为着色的 <pre><code> 块。
 *              无外部依赖，适合在对话消息、详情面板等场景展示代码片段。
 */

import type { ReactNode } from 'react';

/**
 * Token 类型枚举，决定每个代码片段的着色类别。
 */
type TokenType =
  | 'plain'
  | 'comment'
  | 'string'
  | 'number'
  | 'keyword'
  | 'builtin'
  | 'function'
  | 'operator'
  | 'punctuation'
  | 'property'
  | 'boolean';

/**
 * 单个代码 Token：包含文本内容与其语法类型。
 */
type CodeToken = {
  text: string;
  type: TokenType;
};

/** Python 关键字集合，用于分词时识别。 */
const PYTHON_KEYWORDS = new Set([
  'False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await', 'break', 'class',
  'continue', 'def', 'del', 'elif', 'else', 'except', 'finally', 'for', 'from', 'global',
  'if', 'import', 'in', 'is', 'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise',
  'return', 'try', 'while', 'with', 'yield',
]);

/** Python 内置函数/类型集合，用于分词时识别。 */
const PYTHON_BUILTINS = new Set([
  'dict', 'list', 'set', 'tuple', 'str', 'int', 'float', 'bool', 'len', 'range', 'print',
  'open', 'enumerate', 'zip', 'map', 'filter', 'sum', 'min', 'max', 'json', 'Path',
  'Exception', 'ValueError', 'TypeError',
]);

/**
 * 将纯文本片段追加到 Token 列表中（仅当文本非空时）。
 *
 * @param tokens - 当前已解析的 Token 列表
 * @param text - 待追加的纯文本
 */
function appendPlain(tokens: CodeToken[], text: string) {
  if (text) tokens.push({ text, type: 'plain' });
}

/**
 * JSON 代码分词器。使用正则表达式匹配 JSON 的各类语法元素
 * （键名、字符串、布尔值、null、数字、标点）。
 *
 * @param code - JSON 代码文本
 * @returns 解析后的 Token 列表
 */
function tokenizeJson(code: string): CodeToken[] {
  const tokens: CodeToken[] = [];
  // 匹配 JSON 键名（带后续冒号）、字符串值、布尔/null、数字、标点符号
  const pattern = /("(?:\\.|[^"\\])*"(?=\s*:)|"(?:\\.|[^"\\])*"|true|false|null|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[{}\[\]:,])/g;
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(code)) !== null) {
    appendPlain(tokens, code.slice(cursor, match.index));
    const value = match[0];
    // 判断当前匹配值后面是否紧跟冒号 → 是则为属性名（键名）
    if (value.startsWith('"') && code.slice(pattern.lastIndex).trimStart().startsWith(':')) {
      tokens.push({ text: value, type: 'property' });
    } else if (value.startsWith('"')) {
      tokens.push({ text: value, type: 'string' });
    } else if (/^(true|false|null)$/.test(value)) {
      tokens.push({ text: value, type: 'boolean' });
    } else if (/^-?\d/.test(value)) {
      tokens.push({ text: value, type: 'number' });
    } else {
      tokens.push({ text: value, type: 'punctuation' });
    }
    cursor = pattern.lastIndex;
  }
  appendPlain(tokens, code.slice(cursor));
  return tokens;
}

/**
 * Python 代码分词器。识别注释、字符串、数字、关键字、内置类型、
 * 运算符、标点，以及函数名（跟随 def/class 之后）。
 *
 * @param code - Python 代码文本
 * @returns 解析后的 Token 列表
 */
function tokenizePython(code: string): CodeToken[] {
  const tokens: CodeToken[] = [];
  const pattern = /(#.*$|"""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\b\d+(?:\.\d+)?\b|\b[A-Za-z_]\w*\b|[+\-*/%=<>!&|^~]+|[(){}\[\],.:;])/gm;
  let cursor = 0;
  // 标记下一个标识符是否为函数名（紧跟 def/class 之后）
  let expectFunctionName = false;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(code)) !== null) {
    appendPlain(tokens, code.slice(cursor, match.index));
    const value = match[0];
    let type: TokenType = 'plain';
    if (value.startsWith('#')) type = 'comment';
    else if (value.startsWith('"') || value.startsWith("'")) type = 'string';
    else if (/^\d/.test(value)) type = 'number';
    else if (expectFunctionName && /^[A-Za-z_]\w*$/.test(value)) {
      type = 'function';
      expectFunctionName = false;
    } else if (PYTHON_KEYWORDS.has(value)) {
      type = 'keyword';
      // 遇到 def/class 后，下一个标识符应为函数名/类名
      expectFunctionName = value === 'def' || value === 'class';
    } else if (PYTHON_BUILTINS.has(value)) type = 'builtin';
    else if (/^[+\-*/%=<>!&|^~]+$/.test(value)) type = 'operator';
    else if (/^[(){}\[\],.:;]$/.test(value)) type = 'punctuation';
    tokens.push({ text: value, type });
    cursor = pattern.lastIndex;
  }
  appendPlain(tokens, code.slice(cursor));
  return tokens;
}

/**
 * Markdown 代码分词器。识别标题、列表项、引用、行内代码、加粗、
 * 链接、URL 和数字。
 *
 * @param code - Markdown 文本
 * @returns 解析后的 Token 列表
 */
function tokenizeMarkdown(code: string): CodeToken[] {
  const tokens: CodeToken[] = [];
  const pattern = /(^#{1,6}\s.*$|^[-*+]\s+|^>\s.*$|`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^)]+\)|https?:\/\/[^\s)]+|\b\d+(?:\.\d+)?\b)/gm;
  let cursor = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(code)) !== null) {
    appendPlain(tokens, code.slice(cursor, match.index));
    const value = match[0];
    let type: TokenType = 'plain';
    if (value.startsWith('#') || value.startsWith('>')) type = 'keyword';
    else if (/^[-*+]\s+$/.test(value)) type = 'operator';
    else if (value.startsWith('`') || value.startsWith('**')) type = 'string';
    else if (value.startsWith('[') || value.startsWith('http')) type = 'function';
    else if (/^\d/.test(value)) type = 'number';
    tokens.push({ text: value, type });
    cursor = pattern.lastIndex;
  }
  appendPlain(tokens, code.slice(cursor));
  return tokens;
}

/**
 * 根据语言标识选择对应的分词器进行代码分词。
 * 未识别的语言默认使用 Python 分词器。
 *
 * @param code - 代码文本
 * @param language - 语言标识（如 json / python / markdown）
 * @returns 解析后的 Token 列表
 */
function tokenize(code: string, language?: string): CodeToken[] {
  const normalized = (language || '').toLowerCase();
  // 纯文本类语言不做分词
  if (['text', 'txt', 'log', 'stdout', 'stderr', 'plain'].includes(normalized)) return [{ text: code, type: 'plain' }];
  if (normalized.includes('json')) return tokenizeJson(code);
  if (normalized.includes('python') || normalized === 'py') return tokenizePython(code);
  if (normalized.includes('markdown') || normalized === 'md') return tokenizeMarkdown(code);
  // 默认回退到 Python 分词器
  return tokenizePython(code);
}

/**
 * 将代码文本分词并渲染为带语法着色的 ReactNode 数组。
 * 纯文本 Token 直接输出文本，其余 Token 包裹在带类型类名的 <span> 中。
 *
 * @param code - 代码文本
 * @param language - 语言标识
 * @returns 着色后的 ReactNode 数组
 */
export function renderCodeTokens(code: string, language?: string) {
  const tokens = tokenize(code, language);
  return tokens.map((token, index): ReactNode => (
    token.type === 'plain'
      ? token.text
      : <span key={`${token.type}-${index}`} className={`code-token ${token.type}`}>{token.text}</span>
  ));
}

/**
 * 代码块组件。将代码文本渲染为带语法高亮的 <pre><code> 块。
 *
 * @param props - 组件属性
 * @param props.code - 代码文本
 * @param props.language - 语言标识（可选）
 * @param props.className - 附加 CSS 类名（可选）
 * @returns 渲染好的代码块元素
 */
export default function CodeBlock({ code, language, className }: { code: string; language?: string; className?: string }) {
  return (
    <pre className={['code-block-vscode', className].filter(Boolean).join(' ')} data-language={language || undefined}>
      <code>
        {renderCodeTokens(code, language)}
      </code>
    </pre>
  );
}

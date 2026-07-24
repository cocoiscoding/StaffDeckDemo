/**
 * @file HTTP / SSE 客户端封装模块。
 *
 * 该模块是前端与后端 API 通信的统一入口，职责包括：
 * - 通过 Vite 环境变量解析 API 基地址、租户 ID 和调试开关；
 * - 提供统一的 `request` 函数，自动注入鉴权头（Bearer Token）和 JSON Content-Type；
 * - 暴露 `api` 对象，涵盖 GET / POST / PUT / DELETE / Blob 下载等常用语义方法；
 * - 支持 SSE（Server-Sent Events）流式读取（`streamPost` / `streamGet`），用于实时接收聊天事件；
 * - 统一的错误处理：将非 2xx 响应封装为 `ApiError`，并尝试从响应体中解析可读错误消息。
 *
 * 核心概念：
 * - **鉴权头注入**：每次请求通过 `authHeader()` 从会话存储读取 token，附加到 `Authorization` 头。
 * - **SSE 流式解析**：后端以 `\n\n` 分隔的事件块推送数据，前端通过 ReadableStream + TextDecoder 增量解析。
 * - **keepalive 请求**：`postKeepalive` 使用 `keepalive: true`，确保页面卸载（如关闭标签页）时请求仍能发出。
 */

import { getEnterpriseAuthSession } from '../auth';

/**
 * 解析 API 基地址。优先使用环境变量 `VITE_API_BASE_URL`，未配置则返回空串（即相对路径，同源访问）。
 *
 * @returns API 基地址字符串，如 `https://api.example.com` 或 `''`。
 */
const resolveApiBase = () => {
  if (import.meta.env.VITE_API_BASE_URL) {
    return import.meta.env.VITE_API_BASE_URL;
  }

  return '';
};

// 全局缓存的 API 基地址，模块加载时计算一次
const API_BASE = resolveApiBase();

/** 当前租户 ID，来自环境变量，缺省为 `tenant_demo` */
export const TENANT_ID = import.meta.env.VITE_TENANT_ID || 'tenant_demo';
/** 调试日志开关，仅当 `VITE_SHOW_DEBUG === 'true'` 时启用 */
export const SHOW_DEBUG = import.meta.env.VITE_SHOW_DEBUG === 'true';

/**
 * API 请求错误类。所有非 2xx 的 HTTP 响应都会被包装为此类实例。
 *
 * 除标准 `Error` 字段外，额外携带 HTTP 状态码 (`status`) 和原始响应体 (`body`)，
 * 方便上层根据状态码（如 401 鉴权失败）做差异化处理。
 */
export class ApiError extends Error {
  /** HTTP 状态码，如 401、404、500 */
  status: number;
  /** 原始响应体文本 */
  body: string;

  /**
   * @param status      HTTP 状态码
   * @param body        原始响应体文本
   * @param statusText  HTTP 状态描述（如 "Unauthorized"）
   */
  constructor(status: number, body: string, statusText: string) {
    super(parseErrorMessage(body) || statusText || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

/**
 * 判断错误是否为鉴权失败（HTTP 401）。
 *
 * @param error 任意捕获到的错误对象
 * @returns 是 `ApiError` 且状态码为 401 时返回 `true`
 */
export function isAuthError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

/**
 * 底层通用请求函数。所有 RESTful 方法（GET/POST/PUT/DELETE）最终都委托于此。
 *
 * 自动注入 JSON Content-Type 和鉴权头；响应非 2xx 时抛出 `ApiError`。
 *
 * @param path    API 路径（相对于 `API_BASE`），如 `/api/enterprise/agents`
 * @param options 原生 `fetch` 的 RequestInit 选项
 * @returns 解析后的 JSON 响应体（泛型 `T`）
 * @throws {ApiError} 当 HTTP 状态码非 2xx 时抛出
 */
async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...authHeader(),
      ...(options.headers || {}),
    },
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, text, response.statusText);
  }
  return response.json() as Promise<T>;
}

/**
 * 使用 `keepalive` 标志发起 POST 请求。
 *
 * `keepalive: true` 允许请求在页面卸载后继续发送，适用于 `navigator.sendBeacon`
 * 场景的替代方案（如退出前上报埋点、保存草稿等）。
 *
 * @param path  API 路径
 * @param body  请求体，若为 `undefined` 则不发 body
 * @returns 解析后的 JSON 响应体；若响应体为空则返回空对象
 * @throws {ApiError} 当 HTTP 状态码非 2xx 时抛出
 */
async function keepalivePost<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    keepalive: true,
    headers: {
      'Content-Type': 'application/json',
      ...authHeader(),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, text, response.statusText);
  }
  const text = await response.text();
  return (text ? JSON.parse(text) : {}) as T;
}

/**
 * 从会话存储读取 token 并构造鉴权请求头。
 *
 * @returns 包含 `Authorization` 头的对象；若当前无 token 则返回空对象
 */
function authHeader(): Record<string, string> {
  const session = getEnterpriseAuthSession();
  return session?.token ? { Authorization: `Bearer ${session.token}` } : {};
}

/**
 * 统一 API 调用接口对象。提供常用的 HTTP 语义方法：
 *
 * - `get`          — GET 请求
 * - `post`         — POST 请求（JSON body）
 * - `postWithSignal` — 可中断的 POST 请求（配合 `AbortController`）
 * - `postKeepalive` — 页面卸载时仍可发出的 POST 请求
 * - `put`          — PUT 请求
 * - `delete`       — DELETE 请求
 * - `blob`         — 下载二进制资源（不设 Content-Type，返回 Blob）
 */
export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  postWithSignal: <T>(path: string, body: unknown, signal?: AbortSignal) =>
    request<T>(path, { method: 'POST', body: JSON.stringify(body), signal }),
  postKeepalive: <T>(path: string, body?: unknown) => keepalivePost<T>(path, body),
  put: <T>(path: string, body: unknown) => request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  blob: async (path: string) => {
    const response = await fetch(`${API_BASE}${path}`, {
      headers: {
        ...authHeader(),
      },
    });
    if (!response.ok) {
      const text = await response.text();
      throw new ApiError(response.status, text, response.statusText);
    }
    return response.blob();
  },
};

/**
 * 以 `multipart/form-data` 上传聊天附件文件。
 *
 * 使用 FormData 将多个文件以 `files` 字段追加，后端据此解析并存储。
 *
 * @param tenantId 租户 ID
 * @param files    待上传的文件列表
 * @param signal   可选的中断信号
 * @returns 解析后的 JSON 响应体（泛型 `T`）
 * @throws {ApiError} 当 HTTP 状态码非 2xx 时抛出
 */
export async function uploadChatAttachments<T>(
  tenantId: string,
  files: File[],
  signal?: AbortSignal,
): Promise<T> {
  const form = new FormData();
  files.forEach((file) => form.append('files', file));
  const response = await fetch(`${API_BASE}/api/chat/attachments?tenant_id=${encodeURIComponent(tenantId)}`, {
    method: 'POST',
    headers: { ...authHeader() },
    body: form,
    signal,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, text, response.statusText);
  }
  return response.json() as Promise<T>;
}

/**
 * 发起一轮流式聊天对话（SSE），逐事件回调。
 *
 * 内部委托给 `streamPost`，以 POST 方式请求 `/api/chat/stream`。
 *
 * @param body    请求体（包含 session_id、消息内容等）
 * @param onEvent 每解析到一个 SSE 事件时的回调函数
 * @param signal  可选的中断信号，用于取消流式请求
 * @returns Promise 在流结束后 resolve
 */
export async function streamChatTurn(
  body: Record<string, unknown>,
  onEvent: (item: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamPost('/api/chat/stream', body, onEvent, signal);
}

/**
 * SSE 事件结构。`event` 为事件类型字符串，`data` 为 JSON 解析后的事件数据。
 */
export type StreamEvent = {
  event: string;
  data: Record<string, unknown>;
};

/**
 * 以 POST 方式发起 SSE 流式请求并逐事件回调。
 *
 * 流程：
 * 1. 发起 fetch 请求，获取 ReadableStream；
 * 2. 用 TextDecoder 增量解码字节为字符串，累积到 buffer；
 * 3. 以 `\n\n` 作为 SSE 事件块分隔符，拆分出完整块并解析；
 * 4. 不完整的尾部保留在 buffer 中等待下次拼接；
 * 5. 流结束后对残留 buffer 做最终解析。
 *
 * @param path    API 路径
 * @param body    请求体
 * @param onEvent 每解析到一个 SSE 事件时的回调函数
 * @param signal  可选的中断信号
 * @throws {ApiError} 当 HTTP 状态码非 2xx 时抛出
 */
export async function streamPost(
  path: string,
  body: Record<string, unknown>,
  onEvent: (item: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeader() },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, text, response.statusText);
  }
  if (!response.body) {
    throw new Error('当前浏览器不支持流式响应');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  // 持续读取流数据，直到 done
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // 以双换行作为 SSE 事件块分隔符
    const blocks = buffer.split('\n\n');
    // 最后一段可能不完整，保留到下次循环
    buffer = blocks.pop() || '';
    blocks.forEach((block) => {
      const parsed = parseSseBlock(block);
      if (parsed) onEvent(parsed);
    });
  }

  // 流结束后，对残留 buffer 做最终 flush
  buffer += decoder.decode();
  const parsed = parseSseBlock(buffer);
  if (parsed) onEvent(parsed);
}

/**
 * 以 GET 方式发起 SSE 流式请求并逐事件回调。
 *
 * 与 `streamPost` 逻辑一致，区别在于使用 GET 方法且不携带请求体，
 * 适用于订阅类场景（如监听服务端推送的任务进度）。
 *
 * @param path    API 路径
 * @param onEvent 每解析到一个 SSE 事件时的回调函数
 * @param signal  可选的中断信号
 * @throws {ApiError} 当 HTTP 状态码非 2xx 时抛出
 */
export async function streamGet(
  path: string,
  onEvent: (item: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API_BASE}${path}`, { headers: { ...authHeader() }, signal });
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, text, response.statusText);
  }
  if (!response.body) {
    throw new Error('当前浏览器不支持流式响应');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split('\n\n');
    buffer = blocks.pop() || '';
    blocks.forEach((block) => {
      const parsed = parseSseBlock(block);
      if (parsed) onEvent(parsed);
    });
  }

  buffer += decoder.decode();
  const parsed = parseSseBlock(buffer);
  if (parsed) onEvent(parsed);
}

/**
 * 解析单个 SSE 事件块。
 *
 * 一个 SSE 块由多行组成，形如：
 * ```
 * event: token
 * data: {"content":"hello"}
 * ```
 *
 * 解析规则：
 * - 提取以 `event:` 开头的行作为事件类型；
 * - 合并以 `data:` 开头的多行数据（用 `\n` 拼接）；
 * - 尝试将 data 解析为 JSON，失败则放入 `{ raw: rawData }`。
 *
 * @param block SSE 事件块文本
 * @returns 解析后的 `StreamEvent`；若无 event 行或 data 行则返回 `null`
 */
function parseSseBlock(block: string): StreamEvent | null {
  const lines = block.split('\n').map((line) => line.trimEnd());
  const eventLine = lines.find((line) => line.startsWith('event:'));
  const dataLines = lines.filter((line) => line.startsWith('data:'));
  if (!eventLine || dataLines.length === 0) return null;
  const event = eventLine.replace(/^event:\s*/, '');
  const rawData = dataLines.map((line) => line.replace(/^data:\s*/, '')).join('\n');
  try {
    return { event, data: JSON.parse(rawData) as Record<string, unknown> };
  } catch {
    // JSON 解析失败时，将原始文本放入 raw 字段
    return { event, data: { raw: rawData } };
  }
}

/**
 * 从错误响应体中提取人类可读的错误消息。
 *
 * 依次尝试解析 JSON 中的 `detail`、`message`、`error` 字段：
 * - 若为字符串则直接返回；
 * - 若为数组（如 Pydantic 校验错误列表）则逐条格式化后以分号拼接；
 * - JSON 解析失败时返回原始文本。
 *
 * @param text 原始响应体文本
 * @returns 解析后的错误消息，无法解析时返回原始文本或空串
 */
function parseErrorMessage(text: string): string {
  if (!text) return '';
  try {
    const payload = JSON.parse(text) as { detail?: unknown; message?: unknown; error?: unknown };
    const detail = payload.detail ?? payload.message ?? payload.error;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      return detail
        .map(formatValidationDetail)
        .filter(Boolean)
        .join('；');
    }
  } catch {
    return text;
  }
  return text;
}

/**
 * 格式化单条校验错误详情（Pydantic 风格）。
 *
 * 典型的 Pydantic 校验错误项形如：
 * `{ "loc": ["body", "name"], "msg": "field required" }`
 *
 * 格式化为 `body.name: field required` 形式。
 *
 * @param item 校验错误项
 * @returns 格式化后的错误描述
 */
function formatValidationDetail(item: unknown): string {
  if (typeof item === 'string') return item;
  if (!item || typeof item !== 'object') return '';

  const detail = item as { loc?: unknown; msg?: unknown };
  const message = typeof detail.msg === 'string' ? detail.msg : '';
  const location = Array.isArray(detail.loc)
    ? detail.loc.map((part) => String(part)).filter(Boolean).join('.')
    : '';

  if (location && message) return `${location}: ${message}`;
  return message;
}

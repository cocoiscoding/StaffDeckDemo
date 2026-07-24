/**
 * @file 应用入口文件（Entry Point）。
 *
 * 这是 Vite + React 项目的渲染起点，职责是将根组件 `App` 挂载到 DOM 的 `#root` 节点。
 *
 * 包装层级（从外到内）：
 * 1. `React.StrictMode` — 开发模式下启用严格检查（重复渲染检测、废弃 API 警告等）。
 * 2. `I18nProvider` — 国际化上下文 Provider，提供语言切换和自动翻译能力。
 * 3. `App` — 业务根组件，包含路由、鉴权、布局等核心逻辑。
 */

import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { I18nProvider } from './i18n';
import './styles.css';

// 获取挂载目标节点（index.html 中的 <div id="root">），断言非空
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <I18nProvider>
      <App />
    </I18nProvider>
  </React.StrictMode>,
);

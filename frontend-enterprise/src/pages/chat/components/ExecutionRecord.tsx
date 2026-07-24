/**
 * @file ExecutionRecord.tsx
 * @module pages/chat/components/ExecutionRecord
 * @description
 * 执行记录（思维链追踪）面板组件。
 *
 * 在 AI 助手消息气泡内展示推理过程的"执行记录"，包括：
 * - 可折叠的摘要行（显示执行状态图标和文本）
 * - 展开后的详细追踪行（每行包含图标、文本、详情、代码/输出）
 *
 * 支持多种追踪行类型：思考、决策、技能、工具、代码、知识检索，
 * 每种类型有对应的 CotTrace 图标。
 */

import type { ComponentType, SVGProps } from 'react';

import CodeBlock from '@/components/CodeBlock';
import StaffdeckIcon from '@/components/StaffdeckIcon';
import IconCotAdvance from '@/assets/staffdeck/cot-icons/advance.svg?react';
import IconCotExecute from '@/assets/staffdeck/cot-icons/execute.svg?react';
import IconCotGenerated from '@/assets/staffdeck/cot-icons/generated.svg?react';
import IconCotJudge from '@/assets/staffdeck/cot-icons/judge.svg?react';
import IconCotLoading from '@/assets/staffdeck/cot-icons/loading.svg?react';
import IconCotSelect from '@/assets/staffdeck/cot-icons/select.svg?react';
import IconCotTool from '@/assets/staffdeck/cot-icons/tool.svg?react';
import { cn } from '@/lib/utils';
import { useI18n } from '@/i18n';

import {
  CHAT_TRACE_CHEVRON_CLASS,
  CHAT_TRACE_CHEVRON_EXPANDED_CLASS,
  CHAT_TRACE_CODE_BLOCK_CLASS,
  CHAT_TRACE_CODE_DETAILS_CLASS,
  CHAT_TRACE_CODE_SUMMARY_CLASS,
  CHAT_TRACE_DETAILS_CLASS,
  CHAT_TRACE_FLOW_TEXT_CLASS,
  CHAT_TRACE_ICON_CLASS,
  CHAT_TRACE_LINE_CLASS,
  CHAT_TRACE_LINE_CONTENT_CLASS,
  CHAT_TRACE_LINE_DETAIL_CLASS,
  CHAT_TRACE_LINE_TEXT_CLASS,
  CHAT_TRACE_LINE_TEXT_FAILED_CLASS,
  CHAT_TRACE_SUMMARY_CLASS,
  CHAT_TRACE_SUMMARY_FAILED_CLASS,
  CHAT_TRACE_SUMMARY_RUNNING_CLASS,
  CHAT_TRACE_WRAP_CLASS,
} from '../chatPageStyles';
import { traceLineIconName, traceSummaryIconName } from '../chatHelpers';
import type { CotTraceIconName, TraceLine } from '../chatTypes';

/**
 * 思维链图标名称到 SVG 组件的映射表。
 */
const COT_ICON_MAP: Record<CotTraceIconName, ComponentType<SVGProps<SVGSVGElement>>> = {
  advance: IconCotAdvance,
  execute: IconCotExecute,
  generated: IconCotGenerated,
  judge: IconCotJudge,
  loading: IconCotLoading,
  select: IconCotSelect,
  tool: IconCotTool,
};

/**
 * 思维链图标渲染组件。
 *
 * @param name - 图标名称
 * @returns 包裹在 span 中的 SVG 图标
 */
function CotTraceIcon({ name }: { name: CotTraceIconName }) {
  const Icon = COT_ICON_MAP[name];
  return (
    <span className={CHAT_TRACE_ICON_CLASS} aria-hidden="true">
      <Icon />
    </span>
  );
}

/**
 * 执行记录组件的属性类型。
 */
type ExecutionRecordProps = {
  /** 追踪轮次 ID */
  traceTurnId: string;
  /** 摘要信息（文本和状态） */
  summary: { text: string; state: TraceLine['state'] };
  /** 详细追踪行列表 */
  details: TraceLine[];
  /** 是否展开 */
  expanded: boolean;
  /** 展开/折叠切换回调 */
  onToggle: (turnId: string, isExpanded: boolean) => void;
};

/**
 * 执行记录面板组件。
 * 展示 AI 推理过程的思维链追踪信息，支持折叠/展开。
 *
 * @param traceTurnId - 追踪轮次 ID
 * @param summary - 摘要信息
 * @param details - 详细追踪行
 * @param expanded - 是否展开
 * @param onToggle - 切换回调
 * @returns 执行记录面板 JSX
 */
export default function ExecutionRecord({
  traceTurnId,
  summary,
  details,
  expanded,
  onToggle,
}: ExecutionRecordProps) {
  const { t } = useI18n();

  return (
    <div className={CHAT_TRACE_WRAP_CLASS}>
      <button
        type="button"
        className={cn(
          CHAT_TRACE_SUMMARY_CLASS,
          summary.state === 'running' && CHAT_TRACE_SUMMARY_RUNNING_CLASS,
          summary.state === 'failed' && CHAT_TRACE_SUMMARY_FAILED_CLASS,
        )}
        onClick={() => onToggle(traceTurnId, expanded)}
      >
        <CotTraceIcon name={traceSummaryIconName(summary)} />
        <span className={cn(summary.state === 'running' && CHAT_TRACE_FLOW_TEXT_CLASS)}>{t(summary.text)}</span>
        {details.length > 0 && (
          <StaffdeckIcon
            name="arrow"
            size={14}
            className={cn(CHAT_TRACE_CHEVRON_CLASS, expanded && CHAT_TRACE_CHEVRON_EXPANDED_CLASS)}
          />
        )}
      </button>
      {expanded && details.length > 0 && (
        <div className={CHAT_TRACE_DETAILS_CLASS}>
          {details.map((line) => (
            <div key={line.id} className={CHAT_TRACE_LINE_CLASS}>
              <CotTraceIcon name={traceLineIconName(line)} />
              <span className={CHAT_TRACE_LINE_CONTENT_CLASS}>
                <span
                  className={cn(
                    CHAT_TRACE_LINE_TEXT_CLASS,
                    line.state === 'running' && CHAT_TRACE_FLOW_TEXT_CLASS,
                    line.state === 'failed' && CHAT_TRACE_LINE_TEXT_FAILED_CLASS,
                  )}
                >
                  {t(line.text)}
                </span>
                {line.detail && <span className={CHAT_TRACE_LINE_DETAIL_CLASS}>{t(line.detail)}</span>}
                {line.code && (
                  <details open className={CHAT_TRACE_CODE_DETAILS_CLASS}>
                    <summary className={CHAT_TRACE_CODE_SUMMARY_CLASS}>查看代码</summary>
                    <CodeBlock className={CHAT_TRACE_CODE_BLOCK_CLASS} code={line.code} language={line.language || 'python'} />
                  </details>
                )}
                {line.output && (
                  <details open className={CHAT_TRACE_CODE_DETAILS_CLASS}>
                    <summary className={CHAT_TRACE_CODE_SUMMARY_CLASS}>{t(line.outputTitle || '查看输出')}</summary>
                    <CodeBlock className={CHAT_TRACE_CODE_BLOCK_CLASS} code={line.output} language={line.outputLanguage || 'text'} />
                  </details>
                )}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

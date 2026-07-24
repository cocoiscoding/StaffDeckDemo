/**
 * @file scheduled-tasks/TaskActionsMenu.tsx
 * @description 定时任务列表行操作下拉菜单组件。
 *
 * 在任务列表每行末尾展示一个"更多"（⋯）按钮，点击后展开下拉操作菜单。
 * 可用操作包括：查看执行记录、编辑、立即执行、暂停/启用、删除。
 *
 * 菜单项的可见性受任务状态控制：
 * - 已归档（archived）任务：仅可查看记录
 * - 已完成（completed）任务：不显示暂停/启用项
 */

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui';

import IconEdit from '../../assets/icons/edit.svg?react';
import IconListBulleted from '../../assets/icons/list-bulleted.svg?react';
import IconMore from '../../assets/icons/more.svg?react';
import IconPause from '../../assets/icons/pause.svg?react';
import IconPlay from '../../assets/icons/play.svg?react';
import IconTrash from '../../assets/icons/trash.svg?react';
import type { ScheduledTaskRead } from '../../types';

/** 普通菜单项样式 */
const MENU_ITEM_CLASS =
  'w-[110px] cursor-pointer gap-[4px] rounded-[10px] px-[12px] py-[6px] text-[12px] text-[#858b9c] focus:text-[#18181a] [&_svg]:size-[14px]';
/** 危险操作（删除）菜单项样式，使用红色警示色 */
const MENU_ITEM_DANGER_CLASS =
  'w-[110px] cursor-pointer gap-[4px] rounded-[10px] px-[12px] py-[6px] text-[12px] text-[#d20b0b] focus:bg-[#fce7e7] focus:text-[#d20b0b] focus:[&_svg]:text-[#d20b0b]! [&_svg]:size-[14px]';

export type TaskActionsMenuProps = {
  /** 当前行对应的任务数据 */
  task: ScheduledTaskRead;
  /** 点击"查看记录"回调 */
  onViewRuns: (task: ScheduledTaskRead) => void;
  /** 点击"编辑"回调 */
  onEdit: (task: ScheduledTaskRead) => void;
  /** 点击"立即执行"回调 */
  onRunNow: (task: ScheduledTaskRead) => void;
  /** 点击"暂停/启用"回调 */
  onToggleStatus: (task: ScheduledTaskRead) => void;
  /** 点击"删除"回调 */
  onDelete: (task: ScheduledTaskRead) => void;
};

/**
 * 任务行操作下拉菜单。
 * 由父级列表传入当前行任务及各操作回调，组件内部根据任务状态控制菜单项的显示。
 * @param props 组件属性，见 TaskActionsMenuProps
 * @returns 下拉菜单组件
 */
export function TaskActionsMenu({
  task,
  onViewRuns,
  onEdit,
  onRunNow,
  onToggleStatus,
  onDelete,
}: TaskActionsMenuProps) {
  // 已归档任务仅允许查看记录，其余操作隐藏
  const isArchived = task.status === 'archived';
  // 已完成任务无需再暂停/启用
  const isCompleted = task.status === 'completed';
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        aria-label="操作"
        className="grid size-7 place-items-center rounded-[8px] text-[#1a71ff] transition-colors outline-none hover:bg-black/5 hover:text-[#4a8dff] focus-visible:bg-black/5"
      >
        <IconMore className="size-3.5" />
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="flex w-auto min-w-0 flex-col gap-[4px] rounded-[14px] border-0 bg-white p-[4px] shadow-[0px_0px_8px_rgba(0,0,0,0.1)] ring-0 [--accent:#F6F6F6] [--accent-foreground:#18181A]"
      >
        <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => onViewRuns(task)}>
          <IconListBulleted />
          查看记录
        </DropdownMenuItem>
        {!isArchived && (
          <>
            <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => onEdit(task)}>
              <IconEdit />
              编辑
            </DropdownMenuItem>
            <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => onRunNow(task)}>
              <IconPlay />
              立即执行
            </DropdownMenuItem>
            {!isCompleted && (
              <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => onToggleStatus(task)}>
                {task.status === 'active' ? <IconPause /> : <IconPlay />}
                {task.status === 'active' ? '暂停' : '启用'}
              </DropdownMenuItem>
            )}
            <DropdownMenuSeparator className="my-[2px] bg-[#eef0f4]" />
            <DropdownMenuItem
              variant="destructive"
              className={MENU_ITEM_DANGER_CLASS}
              onSelect={() => onDelete(task)}
            >
              <IconTrash />
              删除
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

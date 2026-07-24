/**
 * @file QuickStartGuide.tsx
 * @module components/QuickStartGuide
 * @description 快速入门引导组件。以 Popover 气泡形式逐步高亮页面上的关键功能入口，
 *              引导用户完成模型配置、员工创建、广场浏览、档案查看等核心流程。
 *              支持通过 data-guide-target 属性定位目标元素。
 */

import { useEffect, useLayoutEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  Popover,
  PopoverAnchor,
  PopoverArrow,
  PopoverContent,
  PopoverDescription,
  PopoverTitle,
} from "@/components/ui/popover";
import { Button } from "@/components/ui/button";
import { XIcon } from "lucide-react";
import { EnterpriseRoute } from "@/enums/routes";
import { OPEN_QUICK_START_EVENT } from "./OnboardingGuide";

const ONBOARDING_SEEN_KEY = "staffdeck_onboarding_guide_seen";
/** localStorage 键：标记是否已看过快速入门 */
export const QUICK_START_SEEN_KEY = "staffdeck_quick_start_guide_seen";
/** 自定义事件：快速入门完成 */
export const QUICK_START_COMPLETED_EVENT = "staffdeck-quick-start-completed";
/** 自定义事件：打开模型创建对话框 */
export const OPEN_MODEL_CREATE_EVENT = "staffdeck-open-model-create";

/** 快速入门步骤类型 */
type QuickStartStep = {
  title: string;
  description: string;
  route: EnterpriseRoute;
  /** data-guide-target 目标元素标识 */
  target: string;
  /** 备选目标（当 target 不可见时使用） */
  fallbackTarget?: string;
  /** 操作按钮目标 */
  actionTarget?: string;
  /** Popover 显示方位 */
  side?: "top" | "right" | "bottom" | "left";
  /** 下一步按钮文案 */
  nextLabel?: string;
  /** 下一步跳转路由 */
  nextRoute?: EnterpriseRoute;
  /** 操作事件名（优先于点击目标） */
  eventName?: string;
};

/** 快速入门步骤数据 */
const STEPS: QuickStartStep[] = [
  {
    title: "配置模型 API Key",
    description: "模型是数字员工的大脑。点击「新建模型」，填写 Base URL、Model 和 API Key 即可接入。",
    route: EnterpriseRoute.Models,
    target: "models-create",
    eventName: OPEN_MODEL_CREATE_EVENT,
  },
  {
    title: "创建你的数字员工",
    description: "在这里新建数字员工，绑定模型、知识库、技能与 SOP，它就正式上岗了。",
    route: EnterpriseRoute.Agents,
    target: "route-/enterprise/agents",
    side: "right",
  },
  {
    title: "开放广场 · 共享与复用",
    description: "汇集可共享的 SOP、知识库、技能和工具，新建数字员工时可以直接复制作为起点。",
    route: EnterpriseRoute.Platform,
    target: "route-/enterprise/platform",
    side: "right",
  },
  {
    title: "员工档案 · 一目了然",
    description: "查看数字员工的基本信息、能力配置和工作情况，像翻真人员工的档案一样。",
    route: EnterpriseRoute.Dashboard,
    target: "route-/enterprise/dashboard",
    side: "right",
  },
  {
    title: "定时任务 · 主动干活",
    description: "设置周期任务，数字员工到点自动执行，比如每天早上生成日报。",
    route: EnterpriseRoute.ScheduledTasks,
    target: "route-/enterprise/scheduled-tasks",
    side: "right",
  },
  {
    title: "记忆 · 越用越懂你",
    description: "数字员工会记住对话中的关键信息，越用越了解你的业务和习惯。",
    route: EnterpriseRoute.Memories,
    target: "route-/enterprise/memories",
    side: "right",
  },
  {
    title: "知识库 · 沉淀业务知识",
    description: "上传文档自动沉淀为结构化知识，回答自带出处，业务口径始终一致。",
    route: EnterpriseRoute.Knowledge,
    target: "route-/enterprise/knowledge",
    side: "right",
  },
  {
    title: "技能 · 扩展它会做的事",
    description: "这里是通用技能（Skills），扩展数字员工会做的事，支持从 GitHub 等开源社区直接导入。",
    route: EnterpriseRoute.GeneralSkills,
    target: "route-/enterprise/general-skills",
    side: "right",
  },
  {
    title: "SOP · 流程型技能",
    description: "SOP 是流程型技能：数字员工按步骤执行业务流程，可打断、可恢复，比固定的 workflow 更灵活。一句话描述需求，即可快速生成 SOP。",
    route: EnterpriseRoute.Skills,
    target: "route-/enterprise/skills",
    side: "right",
  },
  {
    title: "去对话端开始协作",
    description: "一切就绪！去对话端选择数字员工开始对话",
    route: EnterpriseRoute.Skills,
    target: "open-chat",
    side: "right",
    nextLabel: "开始对话",
    nextRoute: EnterpriseRoute.Gallery,
  },
];

/**
 * 在页面中查找可见的引导目标元素。
 *
 * @param targetName - data-guide-target 属性值
 * @returns 第一个可见的目标元素，未找到时返回 undefined
 */
function findVisibleTarget(targetName: string) {
  return Array.from(document.querySelectorAll<HTMLElement>(`[data-guide-target="${targetName}"]`)).find((element) => {
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  });
}

/**
 * 查找步骤的目标元素（优先主目标，备选回退目标）。
 *
 * @param step - 快速入门步骤配置
 * @returns 匹配到的目标元素
 */
function findStepTarget(step: QuickStartStep) {
  return findVisibleTarget(step.target) || (step.fallbackTarget ? findVisibleTarget(step.fallbackTarget) : undefined);
}

/**
 * 快速入门引导组件。以 Popover 气泡逐步引导用户完成核心操作流程。
 *
 * @param props - 组件属性
 * @param props.isAdmin - 是否为管理员（管理员多一步模型配置）
 * @returns 渲染好的 Popover 引导组件
 */
export default function QuickStartGuide({ isAdmin }: { isAdmin: boolean }) {
  const navigate = useNavigate();
  const location = useLocation();
  // 管理员包含全部步骤，普通用户跳过第一步（模型配置）
  const steps = useMemo(() => (isAdmin ? STEPS : STEPS.slice(1)), [isAdmin]);
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState(0);
  const [anchorRect, setAnchorRect] = useState({ top: 0, left: 0, width: 1, height: 1 });
  const [anchorReady, setAnchorReady] = useState(false);

  // 新手引导已看过但快速入门未看过时自动打开
  useEffect(() => {
    const welcomeSeen = window.localStorage.getItem(ONBOARDING_SEEN_KEY);
    const quickStartSeen = window.localStorage.getItem(QUICK_START_SEEN_KEY);
    if (welcomeSeen && !quickStartSeen) setOpen(true);
  }, []);

  // 监听重新打开快速入门的事件
  useEffect(() => {
    const reopen = () => {
      if (window.localStorage.getItem(QUICK_START_SEEN_KEY)) return;
      setStep(0);
      setOpen(true);
    };
    window.addEventListener(OPEN_QUICK_START_EVENT, reopen);
    return () => window.removeEventListener(OPEN_QUICK_START_EVENT, reopen);
  }, []);

  /** 完成快速入门：标记已看、关闭、派发完成事件 */
  function finish() {
    window.localStorage.setItem(QUICK_START_SEEN_KEY, "1");
    setOpen(false);
    window.dispatchEvent(new Event(QUICK_START_COMPLETED_EVENT));
  }

  /** 下一步：最后一步时跳转路由并完成 */
  function goNext() {
    if (step === steps.length - 1) {
      if (current.nextRoute) navigate(current.nextRoute);
      finish();
    } else setStep((current) => current + 1);
  }

  /** 上一步 */
  function goPrev() {
    setStep((current) => Math.max(0, current - 1));
  }

  /** 执行步骤操作：导航到对应路由并触发操作事件或点击目标 */
  function runAction() {
    const current = steps[step];
    navigate(current.route);
    const eventName = current.eventName;
    if (eventName) {
      // 有事件名时派发自定义事件
      window.setTimeout(() => window.dispatchEvent(new Event(eventName)), 0);
    } else {
      // 否则点击目标元素
      window.setTimeout(() => findStepTarget(current)?.click(), 50);
    }
  }

  const current = steps[step];
  const isLast = step === steps.length - 1;

  // 当前步骤路由与当前路由不一致时自动导航
  useEffect(() => {
    if (open && location.pathname !== current.route) navigate(current.route);
  }, [current.route, location.pathname, navigate, open]);

  // 布局效果：追踪目标元素位置，更新锚点矩形
  useLayoutEffect(() => {
    if (!open) return undefined;

    let frame = 0;
    let fallbackAllowed = false;
    setAnchorReady(false);
    /** 更新锚点位置 */
    const updateAnchor = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        const target = findVisibleTarget(current.target)
          || (fallbackAllowed && current.fallbackTarget
            ? findVisibleTarget(current.fallbackTarget)
            : undefined);
        if (!target) return;
        const rect = target.getBoundingClientRect();
        setAnchorRect({ top: rect.top, left: rect.left, width: rect.width, height: rect.height });
        setAnchorReady(true);
      });
    };

    updateAnchor();
    // 延迟更新以等待页面渲染完成
    const delayed = window.setTimeout(updateAnchor, 120);
    // 更长延迟后允许使用备选目标
    const fallbackDelay = window.setTimeout(() => {
      fallbackAllowed = true;
      updateAnchor();
    }, 700);
    // 监听 DOM 变化以更新位置
    const observer = new MutationObserver(updateAnchor);
    observer.observe(document.body, { childList: true, subtree: true });
    window.addEventListener("resize", updateAnchor);
    window.addEventListener("scroll", updateAnchor, true);
    return () => {
      window.clearTimeout(delayed);
      window.clearTimeout(fallbackDelay);
      window.cancelAnimationFrame(frame);
      observer.disconnect();
      window.removeEventListener("resize", updateAnchor);
      window.removeEventListener("scroll", updateAnchor, true);
    };
  }, [current.fallbackTarget, current.target, location.pathname, open]);

  return (
    <Popover open={open} onOpenChange={(next) => !next && finish()} modal>
      {/* 透明遮罩层，阻止背景交互 */}
      {open && <div aria-hidden="true" className="fixed inset-0 z-40 cursor-default bg-transparent" />}
      <PopoverAnchor asChild>
        <span aria-hidden="true" className="pointer-events-none fixed z-40" style={anchorRect} />
      </PopoverAnchor>
      <PopoverContent
        side={current.side || "bottom"}
        align="center"
        sideOffset={16}
        collisionPadding={12}
        avoidCollisions
        onInteractOutside={(event) => event.preventDefault()}
        className={`z-50 flex w-[434px] max-w-[calc(100vw-24px)] flex-col gap-[16px] rounded-[20px] border-0 bg-[rgba(24,24,26,0.8)] p-[24px] text-white shadow-[0_18px_60px_rgba(0,0,0,0.24)] ring-0 ${anchorReady ? "visible" : "invisible pointer-events-none"}`}
      >
        <PopoverArrow width={22} height={11} className="fill-[rgba(24,24,26,0.8)]" />
        {/* 关闭按钮 */}
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="关闭引导"
          onClick={finish}
          className="absolute top-[18px] right-[18px] text-white hover:bg-white/10 hover:text-white"
        >
          <XIcon className="size-[18px]" />
        </Button>
        {/* 标题和描述 */}
        <div className="flex min-h-[86px] flex-col gap-[4px] pb-[32px]">
          <PopoverTitle className="text-[14px] leading-[22px] font-medium text-white">
            {current.title}
          </PopoverTitle>
          <PopoverDescription className="text-[14px] leading-[22px] font-normal text-[#f6f6f6]">
            {current.description}
          </PopoverDescription>
        </div>

        {/* 底部导航：步骤指示 + 操作按钮 */}
        <div className="flex items-center justify-between gap-[16px]">
          <span className="shrink-0 py-[3px] text-[14px] leading-normal text-[#858b9c]">
            {step + 1} / {steps.length}
          </span>
          <div className="flex min-w-0 items-center gap-[16px] max-[420px]:gap-[8px]">
            {/* 第一步显示"立即添加"，其余步骤显示"上一步" */}
            <Button
              variant="outline"
              onClick={step === 0 ? runAction : goPrev}
              className="h-[34px] min-w-[100px] rounded-[10px] border-[0.5px] border-[#6d6d6d] bg-black/20 px-[20px] text-[14px] leading-[22px] font-normal whitespace-nowrap text-white hover:bg-white/10 hover:text-white"
            >
              {step === 0 ? "立即添加" : "上一步"}
            </Button>
            <Button
              onClick={goNext}
              className="h-[34px] min-w-[100px] rounded-[8px] bg-white px-[16px] text-[14px] leading-[22px] font-normal whitespace-nowrap text-[#29282d] hover:bg-[#f0f0f0] hover:text-[#29282d]"
            >
              {current.nextLabel || (isLast ? "完成" : "下一步")}
            </Button>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}

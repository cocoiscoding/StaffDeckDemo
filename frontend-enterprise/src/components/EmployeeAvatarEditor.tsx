/**
 * @file EmployeeAvatarEditor.tsx
 * @module components/EmployeeAvatarEditor
 * @description 数字员工头像编辑弹窗组件。提供预设头像选择和自定义图片上传两种模式，
 *              上传图片会自动居中裁剪为方形。保存时调用 API 更新头像配置。
 */

import { CheckOutlined, UploadOutlined } from '../icons';
import {
  Button,
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  notify,
} from '@/components/ui';
import { useEffect, useRef, useState } from 'react';
import { api, TENANT_ID } from '../api/client';
import {
  EMPLOYEE_AVATAR_PRESETS,
  employeeDisplayName,
  employeeProfile,
  type EmployeeProfile,
} from '../employee';
import type { AgentProfileRead } from '../types';
import EmployeeAvatar from './EmployeeAvatar';

/** 上传图片最大字节数（5MB） */
const MAX_INPUT_IMAGE_BYTES = 5 * 1024 * 1024;
/** 头像画布尺寸（正方形边长，px） */
const AVATAR_CANVAS_SIZE = 360;

/** 头像草稿配置类型 */
type AvatarDraft = Pick<EmployeeProfile, 'avatarKind' | 'avatarImage' | 'avatarPreset' | 'avatarText' | 'avatarTone'>;

/**
 * 数字员工头像编辑弹窗组件。
 *
 * @param props - 组件属性
 * @param props.agent - 当前编辑的数字员工数据
 * @param props.open - 弹窗是否打开
 * @param props.onClose - 关闭弹窗回调
 * @param props.onSaved - 保存成功回调，传入更新后的员工数据
 * @returns 渲染好的头像编辑弹窗
 */
export default function EmployeeAvatarEditor({
  agent,
  open,
  onClose,
  onSaved,
}: {
  agent?: AgentProfileRead | null;
  open: boolean;
  onClose: () => void;
  onSaved?: (agent: AgentProfileRead) => void;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [mode, setMode] = useState<'preset' | 'upload'>('preset');
  const [selectedPreset, setSelectedPreset] = useState(EMPLOYEE_AVATAR_PRESETS[0].key);
  const [uploadedImage, setUploadedImage] = useState('');
  const [saving, setSaving] = useState(false);

  // 弹窗打开时，根据员工当前头像配置初始化表单状态
  useEffect(() => {
    if (!open || !agent) return;
    const profile = employeeProfile(agent);
    setMode(profile.avatarKind);
    setSelectedPreset(profile.avatarPreset || EMPLOYEE_AVATAR_PRESETS[0].key);
    setUploadedImage(profile.avatarImage || '');
  }, [agent, open]);

  const selected = EMPLOYEE_AVATAR_PRESETS.find((item) => item.key === selectedPreset) || EMPLOYEE_AVATAR_PRESETS[0];
  // 根据当前模式组装头像草稿配置
  const profile: AvatarDraft = mode === 'upload' && uploadedImage
    ? {
      avatarKind: 'upload',
      avatarImage: uploadedImage,
      avatarPreset: selected.key,
      avatarText: selected.text,
      avatarTone: selected.tone,
    }
    : {
      avatarKind: 'preset',
      avatarImage: '',
      avatarPreset: selected.key,
      avatarText: selected.text,
      avatarTone: selected.tone,
    };

  /**
   * 处理图片上传。将用户选择的图片文件裁剪为方形 Data URL。
   *
   * @param file - 用户选择的图片文件
   */
  async function handleUpload(file: File | undefined) {
    if (!file) return;
    try {
      const dataUrl = await fileToAvatarDataUrl(file);
      setUploadedImage(dataUrl);
      setMode('upload');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '头像读取失败');
    } finally {
      // 重置文件输入，以便可重复选择同一文件
      if (inputRef.current) inputRef.current.value = '';
    }
  }

  /**
   * 保存头像配置。组装 metadata 并调用 PUT API 更新。
   * 上传模式下保存图片 Data URL，预设模式下清除图片字段。
   */
  async function save() {
    if (!agent) return;
    setSaving(true);
    try {
      const metadata = { ...(agent.metadata || {}) };
      metadata.avatar_kind = profile.avatarKind;
      metadata.avatar_preset = profile.avatarPreset;
      metadata.avatar_text = profile.avatarText;
      metadata.avatar_tone = profile.avatarTone;
      // 上传模式保存图片，预设模式清除图片
      if (profile.avatarKind === 'upload' && profile.avatarImage) {
        metadata.avatar_image = profile.avatarImage;
      } else {
        delete metadata.avatar_image;
      }

      const saved = await api.put<AgentProfileRead>(`/api/enterprise/agents/${agent.id}`, {
        tenant_id: TENANT_ID,
        metadata,
      });
      notify.success('员工头像已更新');
      onSaved?.(saved);
      onClose();
      // 触发全局事件，通知其他组件刷新员工列表
      window.dispatchEvent(new Event('ultrarag-enterprise-agent-scope-refresh'));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存头像失败');
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next && !saving) onClose(); }}>
      <DialogContent
        aria-describedby={undefined}
        className="flex max-h-[calc(100dvh-4rem)] w-[calc(100%-2rem)] flex-col gap-4 overflow-hidden rounded-[14px] px-5 py-4 sm:max-w-[680px]"
      >
        <DialogHeader className="px-3">
          <DialogTitle className="text-sm font-normal leading-none text-[#757f9c]">
            {agent ? `设置头像：${employeeDisplayName(agent)}` : '设置头像'}
          </DialogTitle>
        </DialogHeader>

        <div className="min-h-0 flex-1 space-y-[18px] overflow-y-auto px-3">
          {/* 预览区：当前选择的头像效果 */}
          <div className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-[18px] rounded-2xl border border-border bg-[linear-gradient(135deg,color-mix(in_srgb,var(--accent-soft)_34%,transparent),transparent_58%),var(--surface-subtle)] p-[18px]">
            <EmployeeAvatar profile={profile} width={104} height={122} />
            <div>
              <strong className="block text-sm text-foreground">{mode === 'upload' ? '自定义头像' : selected.label}</strong>
              <p className="mt-1 text-xs text-muted-foreground">
                头像会显示在我的数字员工、数字员工档案页和对话端的员工选择中。
              </p>
            </div>
          </div>

          {/* 预设头像选择区 */}
          <section className="space-y-3">
            <div className="flex items-baseline justify-between gap-3">
              <strong className="text-[13px] text-foreground">默认头像</strong>
              <span className="text-xs text-muted-foreground">选择一个适合岗位的默认头像。</span>
            </div>
            <div className="grid grid-cols-3 gap-2.5">
              {EMPLOYEE_AVATAR_PRESETS.map((preset) => {
                const active = mode === 'preset' && selectedPreset === preset.key;
                return (
                  <button
                    key={preset.key}
                    type="button"
                    data-active={active}
                    onClick={() => {
                      setSelectedPreset(preset.key);
                      setMode('preset');
                    }}
                    className="grid min-h-[88px] min-w-0 grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2.5 rounded-[14px] border border-border bg-(--surface) p-3 text-left text-foreground transition-all hover:-translate-y-px hover:border-[color-mix(in_srgb,var(--accent)_45%,var(--border))] hover:shadow-[0_14px_30px_rgba(30,24,16,0.08)] data-[active=true]:-translate-y-px data-[active=true]:border-[color-mix(in_srgb,var(--accent)_45%,var(--border))] data-[active=true]:shadow-[0_14px_30px_rgba(30,24,16,0.08)]"
                  >
                    <EmployeeAvatar
                      profile={{
                        avatarKind: 'preset',
                        avatarImage: '',
                        avatarPreset: preset.key,
                        avatarText: preset.text,
                        avatarTone: preset.tone,
                      }}
                      size={52}
                    />
                    <span className="min-w-0 truncate text-left font-[760]">{preset.label}</span>
                    {active && <CheckOutlined className="text-accent" />}
                  </button>
                );
              })}
            </div>
          </section>

          {/* 自定义头像上传区 */}
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            className="flex w-full items-center gap-3.5 rounded-2xl border border-dashed border-[color-mix(in_srgb,var(--accent)_30%,var(--border))] bg-[linear-gradient(135deg,color-mix(in_srgb,var(--accent-soft)_26%,transparent),transparent_62%),var(--surface-subtle)] px-4 py-3.5 text-left transition-all hover:border-[color-mix(in_srgb,var(--accent)_52%,var(--border))] hover:shadow-[0_12px_28px_rgba(30,24,16,0.07)] focus-visible:border-[color-mix(in_srgb,var(--accent)_60%,var(--border))] focus-visible:shadow-[0_0_0_3px_color-mix(in_srgb,var(--accent)_20%,transparent)] focus-visible:outline-none"
          >
            <input
              ref={inputRef}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(event) => void handleUpload(event.target.files?.[0])}
            />
            <span className="inline-flex size-10 shrink-0 items-center justify-center rounded-xl bg-[color-mix(in_srgb,var(--accent)_12%,var(--surface))] text-lg text-accent">
              <UploadOutlined />
            </span>
            <span className="grid min-w-0 gap-0.5">
              <span className="text-sm font-semibold text-foreground">上传自定义头像</span>
              <span className="text-xs text-muted-foreground">支持常见图片格式，会自动裁剪为方形头像。</span>
            </span>
          </button>
        </div>

        {/* 底部操作按钮 */}
        <DialogFooter className="gap-2 px-3 py-0 sm:justify-end">
          <Button
            variant="outline"
            disabled={saving}
            onClick={onClose}
            className="h-8 w-[92px] rounded-[10px] border-[#e3e7f1] bg-white px-3 text-sm font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
          >
            取消
          </Button>
          <Button
            disabled={saving}
            onClick={() => void save()}
            className="h-8 w-[92px] rounded-[10px] bg-[#18181a] px-3 text-sm font-normal text-white hover:bg-[#303030]"
          >
            保存头像
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * 将文件读取为 Data URL 字符串。
 *
 * @param file - 待读取的文件
 * @returns 文件的 Data URL 字符串
 */
function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('头像读取失败'));
    reader.onload = () => resolve(String(reader.result || ''));
    reader.readAsDataURL(file);
  });
}

/**
 * 从 Data URL 加载 HTMLImageElement。
 *
 * @param dataUrl - 图片的 Data URL
 * @returns 加载完成的 HTMLImageElement
 */
function loadImage(dataUrl: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onerror = () => reject(new Error('无法解析头像图片'));
    image.onload = () => resolve(image);
    image.src = dataUrl;
  });
}

/**
 * 将用户上传的图片文件处理为方形头像 Data URL。
 * 执行类型检查、大小限制、居中裁剪、画布缩放等操作。
 *
 * @param file - 用户选择的图片文件
 * @returns 处理后的方形头像 Data URL（PNG 或 JPEG）
 */
async function fileToAvatarDataUrl(file: File): Promise<string> {
  // 类型校验
  if (!file.type.startsWith('image/')) {
    throw new Error('请选择图片文件');
  }
  // 大小限制
  if (file.size > MAX_INPUT_IMAGE_BYTES) {
    throw new Error('头像图片不能超过 5MB');
  }

  const image = await loadImage(await readFileAsDataUrl(file));
  // 创建正方形画布
  const canvas = document.createElement('canvas');
  canvas.width = AVATAR_CANVAS_SIZE;
  canvas.height = AVATAR_CANVAS_SIZE;
  const context = canvas.getContext('2d');
  if (!context) throw new Error('当前浏览器无法处理头像图片');

  // 居中裁剪：取图片中心正方形区域
  const side = Math.min(image.width, image.height);
  const sx = Math.max(0, (image.width - side) / 2);
  const sy = Math.max(0, (image.height - side) / 2);
  // 填充背景色后绘制裁剪后的图片
  context.fillStyle = '#f7f4ee';
  context.fillRect(0, 0, AVATAR_CANVAS_SIZE, AVATAR_CANVAS_SIZE);
  context.drawImage(image, sx, sy, side, side, 0, 0, AVATAR_CANVAS_SIZE, AVATAR_CANVAS_SIZE);

  // 优先使用 PNG，体积过大时回退到 JPEG
  const png = canvas.toDataURL('image/png');
  return png.length < 650_000 ? png : canvas.toDataURL('image/jpeg', 0.86);
}

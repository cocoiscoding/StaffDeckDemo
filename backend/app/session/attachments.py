"""聊天附件解析模块。

负责将用户上传的附件文件（图片、PDF、文本等）解析为结构化的 ``ChatAttachmentRead`` 对象，
并提供附件上下文注入、图片 payload 提取等辅助功能。

主要能力：

1. **格式检测与解析**：根据文件扩展名和 Content-Type 自动判断文件类型，
   分别走图片、PDF、文本或二进制处理路径。
2. **文本提取**：从 PDF（pypdf）、CSV/TSV、JSON、Markdown 等格式中提取可读文本。
3. **图片处理**：将支持的图片格式编码为 data URL，供视觉模型直接使用。
4. **Python 摘要**：为每个附件生成结构化摘要（行数、词数、表格列数、JSON 字段等），
   供 LLM 理解文件内容。
5. **上下文注入**：将附件信息格式化为可拼接到 LLM 提示词中的文本行。
"""

from __future__ import annotations

import base64
import csv
import io
import json
import mimetypes
import re
from collections.abc import Iterable
from typing import Any

from app.db.models import new_id
from app.session.session_schema import ChatAttachmentRead


MAX_EXTRACTED_TEXT_CHARS = 24_000
MAX_PREVIEW_CHARS = 600
IMAGE_DATA_URL_LIMIT_BYTES = 4 * 1024 * 1024
SUPPORTED_IMAGE_EXTENSIONS = {".gif", ".png", ".svg", ".jpg", ".jpeg", ".webp", ".bmp"}
SUPPORTED_IMAGE_CONTENT_TYPES = {
    "image/gif",
    "image/png",
    "image/svg+xml",
    "image/jpeg",
    "image/webp",
    "image/bmp",
}
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".log",
    ".xml",
    ".html",
    ".htm",
    ".yaml",
    ".yml",
}


def parse_chat_attachment(filename: str, content_type: str | None, data: bytes) -> ChatAttachmentRead:
    """将上传的原始附件数据解析为结构化的 ``ChatAttachmentRead`` 对象。

    根据文件扩展名和 Content-Type 自动路由到对应的解析路径：
    图片 → data URL 编码；PDF → 文本提取；文本 → UTF-8 解码；
    其他 → 标记为 binary 并返回预览提示。

    Args:
        filename: 原始文件名。
        content_type: 上传时声明的 MIME 类型（可为 None）。
        data: 文件二进制内容。

    Returns:
        包含解析结果的 ``ChatAttachmentRead`` 实例。
    """
    safe_name = _safe_filename(filename)
    detected_type = content_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    lower_name = safe_name.lower()
    if _is_supported_image_file(lower_name, detected_type):
        detected_type = _image_content_type_for(lower_name, detected_type)
        return _image_attachment(safe_name, detected_type, data)
    if lower_name.endswith(".pdf") or detected_type == "application/pdf":
        return _pdf_attachment(safe_name, detected_type, data)
    if _is_text_file(lower_name, detected_type):
        return _text_attachment(safe_name, detected_type, data)
    return ChatAttachmentRead(
        id=new_id("file"),
        filename=safe_name,
        content_type=detected_type,
        size=len(data),
        kind="binary",
        preview="暂不支持直接读取该二进制文件内容。",
        python_summary=_python_file_summary(safe_name, detected_type, data, ""),
    )


def attachment_context_lines(attachments: Iterable[ChatAttachmentRead | dict[str, Any]]) -> list[str]:
    """将附件列表格式化为可拼接到 LLM 提示词中的上下文文本行。

    每个附件生成文件名、类型、大小、Python 摘要和可读正文（或预览）。

    Args:
        attachments: 附件对象或字典的可迭代集合。

    Returns:
        list[str]: 格式化的上下文文本行列表，无附件时返回空列表。
    """
    lines: list[str] = []
    normalized = [_coerce_attachment(item) for item in attachments]
    normalized = [item for item in normalized if item]
    if not normalized:
        return lines
    lines.append("上传附件上下文：")
    for index, attachment in enumerate(normalized, start=1):
        lines.append(
            f"{index}. 文件名：{attachment.filename}；类型：{attachment.kind}/{attachment.content_type}；"
            f"大小：{attachment.size} bytes"
        )
        if attachment.python_summary:
            lines.append(f"Python理解摘要：{attachment.python_summary}")
        if attachment.text:
            lines.append("可读取正文：")
            lines.append(_trim_text(attachment.text, MAX_EXTRACTED_TEXT_CHARS))
        elif attachment.preview:
            lines.append(f"预览：{attachment.preview}")
        elif attachment.kind == "image":
            lines.append("图片附件已上传，可在前端消息中查看；如当前模型支持视觉输入，请结合图片内容回答。")
    return lines


def message_content_with_attachment_context(content: str, metadata: dict[str, Any] | None) -> str:
    """将消息内容与附件上下文拼接为完整的 LLM 输入文本。

    从 metadata 中提取附件列表，生成上下文行后拼接到原始内容后面。

    Args:
        content: 原始消息文本。
        metadata: 消息元数据，可能包含 attachments 字段。

    Returns:
        str: 拼接了附件上下文的消息文本，无附件时返回原内容。
    """
    attachments = []
    if isinstance(metadata, dict):
        raw = metadata.get("attachments")
        if isinstance(raw, list):
            attachments = raw
    lines = attachment_context_lines(attachments)
    if not lines:
        return content
    return "\n\n".join([content.strip() or "（用户仅上传了附件）", "\n".join(lines)])


def image_payloads_from_attachments(attachments: Iterable[ChatAttachmentRead | dict[str, Any]]) -> list[dict[str, Any]]:
    """从附件列表中提取支持视觉模型的图片 payload。

    过滤出图片类型且有 data URL 的附件，转换为 OpenAI 风格的
    ``image_url`` payload 格式。

    Args:
        attachments: 附件列表。

    Returns:
        ``[{"type": "image_url", "image_url": {"url": "...", "detail": "auto"}}]``
        格式的 payload 列表。
    """
    payloads: list[dict[str, Any]] = []
    normalized = [_coerce_attachment(item) for item in attachments]
    for attachment in normalized:
        if not attachment or not _attachment_is_supported_image(attachment) or not attachment.data_url:
            continue
        payloads.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": attachment.data_url,
                    "detail": "auto",
                },
            }
        )
    return payloads


def message_images_from_metadata(metadata: dict[str, Any] | None) -> list[dict[str, Any]]:
    """从消息元数据中提取图片 payload 列表。

    Args:
        metadata: 消息元数据，可能包含 attachments 字段。

    Returns:
        list[dict[str, Any]]: 图片 payload 列表，无图片时返回空列表。
    """
    if not isinstance(metadata, dict):
        return []
    attachments = metadata.get("attachments")
    if not isinstance(attachments, list):
        return []
    return image_payloads_from_attachments(attachments)


def request_has_image_attachments(attachments: Iterable[ChatAttachmentRead | dict[str, Any]]) -> bool:
    """检查附件列表中是否包含支持的图片附件。

    Args:
        attachments: 附件对象或字典的可迭代集合。

    Returns:
        bool: 含有支持的图片附件返回 True。
    """
    normalized = [_coerce_attachment(item) for item in attachments]
    return any(bool(item and _attachment_is_supported_image(item)) for item in normalized)


def _text_attachment(filename: str, content_type: str, data: bytes) -> ChatAttachmentRead:
    """解析文本类附件，提取文本内容并生成摘要。

    Args:
        filename: 文件名。
        content_type: MIME 类型。
        data: 文件二进制内容。

    Returns:
        ChatAttachmentRead: 文本类附件对象，含提取的文本和预览。
    """
    text = _decode_text(data)
    trimmed = _trim_text(text, MAX_EXTRACTED_TEXT_CHARS)
    return ChatAttachmentRead(
        id=new_id("file"),
        filename=filename,
        content_type=content_type,
        size=len(data),
        kind="text",
        text=trimmed,
        preview=_trim_text(trimmed, MAX_PREVIEW_CHARS),
        python_summary=_python_file_summary(filename, content_type, data, trimmed),
    )


def _pdf_attachment(filename: str, content_type: str, data: bytes) -> ChatAttachmentRead:
    """解析 PDF 附件，使用 pypdf 提取文本（最多前 30 页）。

    Args:
        filename: 文件名。
        content_type: MIME 类型。
        data: PDF 文件二进制内容。

    Returns:
        ChatAttachmentRead: PDF 附件对象，含提取的文本或解析错误信息。
    """
    text = ""
    error: str | None = None
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = []
        for page in reader.pages[:30]:
            pages.append(page.extract_text() or "")
        text = "\n\n".join(page.strip() for page in pages if page.strip())
        if len(reader.pages) > 30:
            text += f"\n\n... PDF 共 {len(reader.pages)} 页，仅提取前 30 页。"
    except Exception as exc:  # noqa: BLE001 - return readable parse error to caller.
        error = f"PDF 解析失败：{exc}"
    trimmed = _trim_text(text, MAX_EXTRACTED_TEXT_CHARS)
    return ChatAttachmentRead(
        id=new_id("file"),
        filename=filename,
        content_type=content_type or "application/pdf",
        size=len(data),
        kind="pdf",
        text=trimmed or None,
        preview=_trim_text(trimmed, MAX_PREVIEW_CHARS) if trimmed else None,
        python_summary=_python_file_summary(filename, content_type, data, trimmed),
        error=error,
    )


def _image_attachment(filename: str, content_type: str, data: bytes) -> ChatAttachmentRead:
    """解析图片附件，编码为 data URL（不超过大小限制时）。

    Args:
        filename: 文件名。
        content_type: 图片 MIME 类型。
        data: 图片二进制内容。

    Returns:
        ChatAttachmentRead: 图片附件对象，含 data URL（超限时为 None）。
    """
    data_url = None
    if len(data) <= IMAGE_DATA_URL_LIMIT_BYTES:
        encoded = base64.b64encode(data).decode("ascii")
        data_url = f"data:{content_type};base64,{encoded}"
    return ChatAttachmentRead(
        id=new_id("file"),
        filename=filename,
        content_type=content_type,
        size=len(data),
        kind="image",
        data_url=data_url,
        preview="图片附件",
        python_summary=_python_file_summary(filename, content_type, data, ""),
    )


def _python_file_summary(filename: str, content_type: str, data: bytes, text: str) -> str:
    """为附件生成结构化的 Python 理解摘要。

    包含文件大小、字符数、行数、词数，以及检测到的表格结构或 JSON 结构信息。

    Args:
        filename: 文件名。
        content_type: MIME 类型。
        data: 原始二进制数据。
        text: 已提取的文本内容。

    Returns:
        str: 摘要文本。
    """
    parts = [f"文件 {filename}，{len(data)} bytes，MIME {content_type}。"]
    if text:
        lines = text.splitlines()
        words = re.findall(r"\S+", text)
        parts.append(f"解析得到 {len(text)} 个字符、{len(lines)} 行、约 {len(words)} 个词。")
        tabular = _tabular_summary(text)
        if tabular:
            parts.append(tabular)
        json_summary = _json_summary(text)
        if json_summary:
            parts.append(json_summary)
    else:
        parts.append("未抽取到可直接阅读的文本正文。")
    return " ".join(parts)


def _tabular_summary(text: str) -> str:
    """检测文本中的表格结构（CSV/TSV 等）并生成摘要。

    取前 20 行用 csv.Sniffer 自动检测分隔符，识别成功后返回列数和前几列名。

    Args:
        text: 待检测的文本内容。

    Returns:
        str: 表格摘要文本，未检测到表格结构时返回空字符串。
    """
    sample = "\n".join(text.splitlines()[:20])
    if not sample.strip():
        return ""
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        return ""
    rows = list(csv.reader(io.StringIO(sample), dialect))
    if not rows:
        return ""
    columns = rows[0]
    return f"检测到表格结构，约 {len(columns)} 列；前几列：{', '.join(columns[:6])}。"


def _json_summary(text: str) -> str:
    """检测文本是否为 JSON 并生成结构摘要。

    尝试解析 JSON，成功后返回对象顶层字段或数组元素数量。

    Args:
        text: 待检测的文本内容。

    Returns:
        str: JSON 结构摘要文本，非 JSON 时返回空字符串。
    """
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return ""
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return ""
    if isinstance(parsed, dict):
        keys = list(parsed.keys())[:8]
        return f"检测到 JSON 对象，顶层字段：{', '.join(map(str, keys))}。"
    if isinstance(parsed, list):
        return f"检测到 JSON 数组，元素数量：{len(parsed)}。"
    return "检测到 JSON 标量。"


def _decode_text(data: bytes) -> str:
    """尝试多种编码解码二进制数据为文本。

    依次尝试 utf-8-sig、utf-8、gb18030、latin-1，全部失败时用 replace 模式。

    Args:
        data: 二进制数据。

    Returns:
        str: 解码后的文本字符串。
    """
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _safe_filename(filename: str) -> str:
    """从路径中提取安全的文件名，去除目录分隔符。

    Args:
        filename: 原始文件路径或文件名。

    Returns:
        str: 安全的文件名，为空时回退为 "uploaded-file"。
    """
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip()
    return name or "uploaded-file"


def _trim_text(text: str, max_chars: int) -> str:
    """截断文本到指定长度，去除空字符并添加截断标记。

    Args:
        text: 原始文本。
        max_chars: 最大字符数。

    Returns:
        str: 截断后的文本，超出长度时末尾添加截断提示。
    """
    normalized = text.replace("\x00", "").strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rstrip() + "\n...（内容已截断）"


def _is_text_file(lower_name: str, content_type: str) -> bool:
    """判断文件是否为可解析的文本类型。

    基于文件扩展名和 Content-Type 综合判断。

    Args:
        lower_name: 小写文件名。
        content_type: MIME 类型。

    Returns:
        bool: 是文本类型返回 True。
    """
    extension = "." + lower_name.rsplit(".", 1)[-1] if "." in lower_name else ""
    return (
        extension in TEXT_EXTENSIONS
        or content_type.startswith("text/")
        or content_type
        in {
            "application/json",
            "application/xml",
            "application/x-yaml",
            "application/yaml",
        }
    )


def _is_supported_image_file(lower_name: str, content_type: str) -> bool:
    """判断文件是否为支持的图片格式。

    基于 Content-Type 和文件扩展名综合判断。

    Args:
        lower_name: 小写文件名。
        content_type: MIME 类型。

    Returns:
        bool: 是支持的图片格式返回 True。
    """
    extension = "." + lower_name.rsplit(".", 1)[-1] if "." in lower_name else ""
    return content_type.lower() in SUPPORTED_IMAGE_CONTENT_TYPES or extension in SUPPORTED_IMAGE_EXTENSIONS


def _attachment_is_supported_image(attachment: ChatAttachmentRead) -> bool:
    """判断附件对象是否为支持的图片类型。

    Args:
        attachment: 附件读取对象。

    Returns:
        bool: 是支持的图片附件返回 True。
    """
    return attachment.kind == "image" and _is_supported_image_file(attachment.filename.lower(), attachment.content_type)


def _image_content_type_for(lower_name: str, content_type: str) -> str:
    """推断图片的正确 MIME 类型。

    优先使用已知 Content-Type，其次通过扩展名猜测，最后按扩展名硬编码映射。

    Args:
        lower_name: 小写文件名。
        content_type: 原始 MIME 类型。

    Returns:
        str: 推断出的图片 MIME 类型。
    """
    normalized = content_type.lower()
    if normalized in SUPPORTED_IMAGE_CONTENT_TYPES:
        return content_type
    guessed = mimetypes.guess_type(lower_name)[0]
    if guessed and guessed.lower() in SUPPORTED_IMAGE_CONTENT_TYPES:
        return guessed
    if lower_name.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower_name.endswith(".png"):
        return "image/png"
    if lower_name.endswith(".gif"):
        return "image/gif"
    if lower_name.endswith(".svg"):
        return "image/svg+xml"
    if lower_name.endswith(".webp"):
        return "image/webp"
    if lower_name.endswith(".bmp"):
        return "image/bmp"
    return content_type


def _coerce_attachment(value: ChatAttachmentRead | dict[str, Any]) -> ChatAttachmentRead | None:
    """将附件字典或对象统一转换为 ChatAttachmentRead 对象。

    Args:
        value: ChatAttachmentRead 对象或字典。

    Returns:
        ChatAttachmentRead | None: 转换后的对象，类型不符或验证失败返回 None。
    """
    if isinstance(value, ChatAttachmentRead):
        return value
    if not isinstance(value, dict):
        return None
    try:
        return ChatAttachmentRead.model_validate(value)
    except Exception:
        return None

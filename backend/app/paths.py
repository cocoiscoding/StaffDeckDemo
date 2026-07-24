"""文件系统路径解析模块。

本模块负责确定应用在不同运行环境（开发态 / PyInstaller 打包态）下的
各类文件系统路径，包括应用根目录、资源目录和用户数据目录。

核心概念
---------
- **frozen 状态**：当应用通过 PyInstaller 打包为单文件或单目录时，
  ``sys.frozen`` 为 ``True``，资源路径需从 ``sys._MEIPASS`` 读取。
- **三类路径**：
    - ``app_root()``：源代码根目录（开发态用于定位项目内文件）；
    - ``resource_dir()``：只读资源目录（打包态指向临时解压目录）；
    - ``user_data_dir()``：用户可写数据目录（数据库、配置等持久化数据）。

与其他模块的关系
-----------------
- 被 :mod:`app.db`（定位数据库文件）、:mod:`app.config` 等模块用于确定文件路径。
- 用户数据目录名使用对外品牌 ``StaffDeck``，环境变量前缀保留内部标识 ``ULTRARAG``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """判断当前是否运行在 PyInstaller 打包环境中。

    Returns:
        打包运行返回 ``True``，开发态运行返回 ``False``。
    """
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """获取应用根目录路径。

    开发态下返回 ``backend/`` 目录（即 ``app/paths.py`` 的上上级），
    打包态下也应正确返回（因为 ``__file__`` 仍指向打包后的路径）。

    Returns:
        应用根目录的 :class:`Path` 对象。
    """
    # 开发态：backend/ 目录（app/paths.py 的上两级）
    return Path(__file__).resolve().parents[1]


def resource_dir() -> Path:
    """获取只读资源目录路径。

    打包态下返回 PyInstaller 的临时解压目录（``_MEIPASS``），
    开发态下返回应用根目录。

    Returns:
        资源目录的 :class:`Path` 对象。
    """
    if is_frozen():
        # PyInstaller 打包态：资源解压在 _MEIPASS 临时目录中
        return Path(getattr(sys, "_MEIPASS"))
    return app_root()


def user_data_dir() -> Path:
    """获取用户可写数据目录路径，如不存在则自动创建。

    目录确定规则（优先级从高到低）：
      1. 环境变量 ``ULTRARAG_DATA_DIR`` 指定的路径；
      2. 平台默认用户数据目录下的 ``StaffDeck`` 子目录：
         - macOS: ``~/Library/Application Support/StaffDeck``
         - Windows: ``%APPDATA%/StaffDeck``
         - Linux: ``~/.local/share/StaffDeck``

    Returns:
        用户数据目录的 :class:`Path` 对象（已确保目录存在）。
    """
    # 环境变量前缀保留 ULTRARAG_（内部标识，不改）；目录名用对外品牌 StaffDeck
    override = os.environ.get("ULTRARAG_DATA_DIR", "").strip()
    if override:
        # 支持环境变量覆盖数据目录
        base = Path(override).expanduser()
    elif sys.platform == "darwin":
        # macOS 标准应用数据目录
        base = Path.home() / "Library" / "Application Support" / "StaffDeck"
    elif sys.platform == "win32":
        # Windows AppData 目录
        base = Path(os.environ.get("APPDATA", Path.home())) / "StaffDeck"
    else:
        # Linux XDG 标准数据目录
        base = Path.home() / ".local" / "share" / "StaffDeck"
    base.mkdir(parents=True, exist_ok=True)  # 确保目录存在
    return base

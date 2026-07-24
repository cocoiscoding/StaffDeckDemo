"""通用技能运行时 Python 环境管理。

本模块负责为通用技能（General Skill）的沙箱执行准备一个独立的 Python 环境，
包括：
- 解析运行时 Python 解释器路径（支持显式配置、打包态附带、虚拟环境等）
- 自动创建虚拟环境（如不存在）
- 自动检测并安装缺失的第三方依赖包
- 构建带正确 PATH / VIRTUAL_ENV 的子进程环境变量
"""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

from app.config import get_settings


# 包名 → import 名的映射表：某些 PyPI 包名与其 import 名不同
IMPORT_NAMES = {
    "beautifulsoup4": "bs4",
    "python-docx": "docx",
    "python-dateutil": "dateutil",
}


class GeneralSkillRuntimeError(RuntimeError):
    """通用技能运行环境准备过程中发生的错误。"""
    pass


def ensure_runtime_python() -> Path:
    """确保通用技能运行时 Python 解释器存在，返回其路径。

    依次执行：
    1. 解析运行时 Python 路径（根据配置或默认策略）。
    2. 若解释器不存在则自动创建虚拟环境。
    3. 若启用了自动安装且允许网络安装，则检查并安装缺失依赖。

    Returns:
        运行时 Python 解释器的完整路径。

    Raises:
        GeneralSkillRuntimeError: 虚拟环境创建或依赖安装失败时抛出。
    """
    settings = get_settings()
    python_path = _resolve_runtime_python(settings.general_skill_runtime_python, settings.general_skill_runtime_venv)
    if not python_path.exists():
        _create_runtime_venv(python_path)
    if settings.general_skill_runtime_auto_install and settings.general_skill_network_install:
        _ensure_packages(python_path, settings.general_skill_runtime_package_list)
    return python_path


def runtime_environment(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """构建通用技能子进程使用的环境变量字典。

    在继承父进程环境变量的基础上，将运行时 venv 的 bin 目录前置到 PATH，
    设置 VIRTUAL_ENV 指向 venv 根目录，并确保输出无缓冲。

    Args:
        base_env: 基础环境变量，默认使用 ``os.environ``。

    Returns:
        构建好的环境变量字典。
    """
    python_path = ensure_runtime_python()
    env = dict(base_env or os.environ)
    bin_dir = python_path.parent
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["VIRTUAL_ENV"] = str(bin_dir.parent)
    env["GENERAL_SKILL_RUNTIME_PYTHON"] = str(python_path)
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _backend_dir() -> Path:
    """返回 backend 目录的绝对路径。"""
    return Path(__file__).resolve().parents[2]


def _bundled_python() -> Path:
    """返回打包态（frozen）附带的 python-build-standalone 解释器路径。

    打包态下附带的 python-build-standalone 位置随平台不同：
    - macOS .app：runtime 在 Contents/Resources/runtime（放 Resources 是为了 codesign 密封通过），
      sys.executable 在 Contents/MacOS/staffdeck，需跳到同级的 Resources。
    - Linux/Windows onedir：runtime 在可执行文件同级 runtime/。
    不使用 resource_dir() == sys._MEIPASS（onedir 下指向 _internal/）。
    """
    exe_dir = Path(sys.executable).resolve().parent
    if sys.platform == "darwin" and exe_dir.name == "MacOS" and exe_dir.parent.name == "Contents":
        root = exe_dir.parent / "Resources" / "runtime"
    else:
        root = exe_dir / "runtime"
    return (root / "python.exe") if sys.platform == "win32" else (root / "bin" / "python3")


def _resolve_runtime_python(explicit_python: str, explicit_venv: str) -> Path:
    """按优先级解析运行时 Python 解释器路径。

    解析优先级：
    1. 显式指定的 Python 解释器路径（settings 中配置）。
    2. 显式指定的虚拟环境路径。
    3. 打包态下附带的 python-build-standalone（若存在）。
    4. backend 目录下的 ``.venv``（若存在）。
    5. backend 目录下的 ``.runtime_venv``。

    Args:
        explicit_python: 配置中显式指定的 Python 解释器路径。
        explicit_venv: 配置中显式指定的虚拟环境路径。

    Returns:
        解析后的 Python 解释器 Path 对象。
    """
    if explicit_python.strip():
        return Path(explicit_python).expanduser()
    if explicit_venv.strip():
        return _python_in_venv(Path(explicit_venv).expanduser())
    from app import paths
    if paths.is_frozen() and _bundled_python().exists():
        return _bundled_python()
    backend_venv = _backend_dir() / ".venv"
    if _python_in_venv(backend_venv).exists():
        return _python_in_venv(backend_venv)
    return _python_in_venv(_backend_dir() / ".runtime_venv")


def _python_in_venv(venv_dir: Path) -> Path:
    """根据平台返回虚拟环境中 Python 解释器的标准路径。

    Args:
        venv_dir: 虚拟环境根目录。

    Returns:
        Python 解释器的完整路径（Windows 下为 Scripts/python.exe，其他为 bin/python）。
    """
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _create_runtime_venv(python_path: Path) -> None:
    """在 python_path 对应的虚拟环境目录中创建新的 venv。

    Args:
        python_path: 目标 Python 解释器路径（其父目录的父目录即为 venv 根目录）。

    Raises:
        GeneralSkillRuntimeError: 创建 venv 后解释器仍不存在时抛出。
    """
    venv_dir = python_path.parent.parent
    venv_dir.mkdir(parents=True, exist_ok=True)
    venv.EnvBuilder(with_pip=True, clear=False).create(venv_dir)
    if not python_path.exists():
        raise GeneralSkillRuntimeError(f"通用技能运行环境创建失败：{python_path}")


def _ensure_packages(python_path: Path, packages: list[str]) -> None:
    """检测并安装缺失的第三方依赖包。

    首先逐个检查每个包是否已可 import，仅对缺失的包执行 pip install。
    若配置了 pip 镜像源，则自动追加 ``-i`` 参数。

    Args:
        python_path: 运行时 Python 解释器路径。
        packages: 需要确保安装的包名列表。

    Raises:
        GeneralSkillRuntimeError: pip install 返回非零退出码时抛出，附带 stderr。
    """
    settings = get_settings()
    missing = [package for package in packages if not _can_import(python_path, _import_name(package))]
    if not missing:
        return
    args = [str(python_path), "-m", "pip", "install", *missing]
    if settings.general_skill_pip_index_url.strip():
        args += ["-i", settings.general_skill_pip_index_url.strip()]
    env = os.environ.copy()
    # 尝试使用 certifi 提供的 CA 证书，避免 SSL 校验失败
    try:
        import certifi
        env["SSL_CERT_FILE"] = certifi.where()
        env["PIP_CERT"] = certifi.where()
    except Exception:
        pass
    result = subprocess.run(
        args,
        cwd=str(_backend_dir()),
        text=True,
        capture_output=True,
        timeout=settings.general_skill_pip_timeout_seconds,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        raise GeneralSkillRuntimeError(
            "通用技能运行环境依赖安装失败："
            + ", ".join(missing)
            + "\n"
            + (result.stderr or result.stdout or "").strip()
        )


def _can_import(python_path: Path, import_name: str) -> bool:
    """检查指定的 Python 解释器能否成功 import 某个模块。

    Args:
        python_path: Python 解释器路径。
        import_name: 要检查的 import 名。

    Returns:
        能成功 import 返回 True，否则返回 False。
    """
    result = subprocess.run(
        [str(python_path), "-c", f"import {import_name}"],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    return result.returncode == 0


def _import_name(package: str) -> str:
    """将 PyPI 包名转换为 Python import 名。

    处理版本限定符（``==`` / ``>=`` / ``<``）和包名到 import 名的映射。

    Args:
        package: 原始包名字符串，可能含版本限定。

    Returns:
        转换后的 import 名。
    """
    normalized = package.strip()
    return IMPORT_NAMES.get(normalized, normalized.split("==", 1)[0].split(">=", 1)[0].split("<", 1)[0].replace("-", "_"))

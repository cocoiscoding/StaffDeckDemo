"""全局配置模块。

本模块定义应用的全部可配置参数，通过 Pydantic Settings 从环境变量或
``.env`` 文件加载配置，并使用 ``lru_cache`` 确保单例语义。

核心概念
---------
- **配置来源优先级**：环境变量 > ``.env`` 文件 > 代码默认值。
- **单例缓存**：:func:`get_settings` 使用 ``@lru_cache`` 装饰，
  全应用共享同一 :class:`Settings` 实例，避免重复 IO。
- **派生属性**：CORS 源列表、工具基址 URL、技能运行时依赖列表等通过
  ``@property`` 从原始字符串配置实时派生，保持单一数据源。

与其他模块的关系
-----------------
- 几乎被所有其他模块依赖（安全模块取 ``app_secret``、API 模块取
  CORS 源和模型配置、技能模块取运行时参数等）。
- ``.env`` 文件路径可通过环境变量 ``ULTRARAG_DOTENV`` 覆盖。
"""

import os as _os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用全局配置类。

    所有字段均可通过同名（大小写不敏感）环境变量或 ``.env`` 文件覆盖。
    使用 ``extra="ignore"`` 忽略未定义的配置项，避免启动报错。

    设计意图
    ---------
    将所有配置集中管理，避免散落在各模块的硬编码值，便于部署时统一调整。
    """

    # --- 应用基础配置 ---
    app_name: str = "Skill Agent Loop Service"  # 应用名称，用于 FastAPI 实例和健康检查
    database_url: str = "sqlite:///./skill_agent_loop.db"  # 数据库连接字符串
    app_secret: str = "change-me-in-development"  # 加密/签名主密钥，生产环境必须通过环境变量覆盖

    # --- 初始账号密码 ---
    # 优先使用哈希（更安全），未配置时降级到明文再哈希
    admin_password_hash: str = "pbkdf2_sha256$7ac2f3616cb0218a08410df862047d17$8sMY1iL9cm2av3gL6PP86lTvZV73LsMMe58-02Yzt54="
    user_demo_password_hash: str = "pbkdf2_sha256$29d459b5f286f3497d1bbe410c67468c$E483X5Z_x_3-QBYHNRG52rqEfiOvLqFR-bpfSpGpdOI="
    admin_password: str = ""  # 明文密码（已弃用，仅为兼容保留；设置后优先于哈希）
    user_demo_password: str = ""  # 明文密码（已弃用，仅为兼容保留）

    # --- 演示模型配置 ---
    # 注意：真实 API Key 必须通过 backend/.env 的 DEMO_MODEL_API_KEY 注入，切勿硬编码到此处
    demo_model_base_url: str = "https://api.deepseek.com/v1"  # 演示用 LLM API 地址
    demo_model_name: str = "deepseek-v4-pro"  # 演示用模型名称
    demo_model_api_key: str = ""  # 演示用模型 API Key（留空，由 .env 注入）

    # --- 模型调用超时与行为 ---
    model_api_timeout_seconds: float = 600.0  # 模型 API 单次请求超时（秒）
    model_thinking_mode: str = ""  # 模型思考模式开关（如 "enabled" / "disabled"）
    model_thinking_models: str = ""  # 支持思考模式的模型名称列表（逗号分隔）

    # --- 工具服务配置 ---
    tool_timeout_seconds: float = 8.0  # 单个工具调用超时（秒）
    tool_base_url: str = "http://localhost:5173"  # 工具服务（前端）基址 URL

    # --- CORS 跨域配置 ---
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"  # 允许的前端来源（逗号分隔）

    # --- 通用技能运行时配置 ---
    general_skill_runtime_python: str = ""  # 技能运行时使用的 Python 解释器路径（留空则用当前解释器）
    general_skill_runtime_venv: str = ""  # 技能运行时使用的虚拟环境路径
    general_skill_runtime_packages: str = "requests,httpx"  # 技能运行时预安装的依赖包（逗号分隔）
    general_skill_runtime_auto_install: bool = True  # 是否自动安装技能缺失的依赖
    general_skill_pip_index_url: str = ""  # pip 安装源 URL（留空则用默认源）
    general_skill_pip_timeout_seconds: int = 180  # pip 安装超时（秒）
    general_skill_network_install: bool = False  # 是否允许联网安装依赖（离线部署时设为 False）

    model_config = SettingsConfigDict(
        env_file=_os.environ.get("ULTRARAG_DOTENV", ".env"),  # 支持 ULTRARAG_DOTENV 环境变量指定 .env 路径
        env_file_encoding="utf-8",
        extra="ignore",  # 忽略 .env / 环境中存在但 Settings 未定义的变量
    )

    @property
    def cors_origin_list(self) -> list[str]:
        """将逗号分隔的 ``cors_origins`` 字符串解析为去空白后的列表。

        Returns:
            CORS 允许来源的列表，已过滤空字符串。
        """
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def normalized_tool_base_url(self) -> str:
        """去除尾部斜杠的工具服务基址 URL。

        Returns:
            规范化后的 URL（无尾部 ``/``），避免拼接路径时出现双斜杠。
        """
        return self.tool_base_url.rstrip("/")

    @property
    def general_skill_runtime_package_list(self) -> list[str]:
        """将逗号分隔的 ``general_skill_runtime_packages`` 解析为包名列表。

        Returns:
            依赖包名列表，已过滤空字符串。
        """
        return [item.strip() for item in self.general_skill_runtime_packages.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    """获取全局唯一的 :class:`Settings` 实例（单例模式）。

    使用 ``lru_cache`` 确保应用生命周期内只读取一次配置，后续调用直接返回缓存。

    Returns:
        :class:`Settings` 实例。
    """
    return Settings()

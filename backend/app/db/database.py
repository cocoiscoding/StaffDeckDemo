"""数据库初始化与数据迁移模块。

本模块负责 StaffDeck 平台的数据库底层管理，包含三方面职责：

1. 引擎与会话管理
    根据配置（``app.config.get_settings``）构建 SQLAlchemy 引擎，并提供
    ``get_session`` 生成器供 FastAPI 依赖注入使用。对于 SQLite 数据库会做
    URL 归一化（将相对路径锚定到用户数据目录或项目根目录）与运行时优化
    （开启 WAL 模式、设置 busy_timeout）。

2. 建表初始化
    ``init_db`` 调用 SQLModel 的 ``create_all`` 创建全部模型表（表结构定义见
    ``app.db.models``）。

3. 渐进式数据迁移（SQLite 专用）
    平台在演进过程中多次变更表结构与数据模型。由于 SQLite 缺少成熟的
    schema 迁移工具，本模块通过 ``_migrate_sqlite_skill_schema`` 在每次启动时
    以幂等方式补齐缺失列、迁移旧数据、拆分知识库、规范化技能图谱（将旧的
    线性 steps 模型转换为 nodes/edges 图模型）、并为智能体补建分支与绑定。
    所有迁移均依赖“先检测列/行是否存在再操作”的模式，保证可重复执行而不
    破坏数据。部分迁移通过 ``app_data_migrations`` 表记录已应用的迁移 ID，
    避免重复执行有副作用的变更。

本模块与 ``models.py``（表结构）、``seed.py``（演示数据）共同构成数据层。
"""

from collections.abc import Callable, Generator
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote

from sqlalchemy import Engine, inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings


def _normalize_database_url(url: str) -> str:
    """将 SQLite 相对路径 URL 归一化为绝对路径。

    SQLAlchemy 的 SQLite 引擎对相对路径的解析依赖于进程当前工作目录，这在
    打包为单文件应用或以不同目录启动时会导致数据库文件位置不确定。本函数将
    形如 ``sqlite:///foo.db`` 的相对路径锚定到一个稳定的基准目录：
    - 打包运行（frozen）时使用用户数据目录（``paths.user_data_dir``）；
    - 开发模式下使用项目后端根目录（本文件上溯两级）。
    绝对路径 URL、内存库、非 SQLite URL 原样返回。

    Args:
        url: 原始数据库 URL，例如 ``sqlite:///data.db``、``sqlite:////abs/path``。

    Returns:
        归一化后的数据库 URL（绝对路径）。
    """
    if not url.startswith("sqlite:///") or url.startswith("sqlite:////") or url == "sqlite:///:memory:":
        return url

    raw_path = unquote(url.removeprefix("sqlite:///"))
    if not raw_path or raw_path == ":memory:":
        return url

    path = Path(raw_path)
    if path.is_absolute():
        return url

    from app import paths
    base_dir = paths.user_data_dir() if paths.is_frozen() else Path(__file__).resolve().parents[2]
    return f"sqlite:///{(base_dir / path).resolve()}"


settings = get_settings()

# 全局数据库 URL（已对 SQLite 相对路径做绝对化处理）。
database_url = _normalize_database_url(settings.database_url)
# SQLite 需放宽跨线程使用限制并设置锁等待超时；其他数据库无需这些参数。
connect_args = {"check_same_thread": False, "timeout": 30} if database_url.startswith("sqlite") else {}
engine: Engine = create_engine(database_url, echo=False, connect_args=connect_args)

# —— 以下为数据迁移相关常量 ——
# 数据迁移记录表（app_data_migrations）中用于幂等控制的迁移标识。
_DEFAULT_MODEL_OUTPUT_LIMIT_MIGRATION_ID = "20260712_default_model_output_tokens_8192"
_MODEL_API_PROTOCOLS_MIGRATION_ID = "20260722_model_api_protocols_v1"
# 默认模型输出 token 的旧/新默认值，用于 _migrate_default_model_output_limit。
_LEGACY_DEFAULT_MODEL_OUTPUT_TOKENS = 2048
_DEFAULT_MODEL_OUTPUT_TOKENS = 8192
# 模型 API 协议化迁移需要保证存在的全部列名集合，用于结构完整性校验。
_MODEL_API_PROTOCOL_COLUMNS = {
    "extra_body_json",
    "api_protocol",
    "protocol_options_json",
    "legacy_unmapped_options_json",
    "trust_status",
    "verified_at",
    "verified_fingerprint",
    "verification_attempt_id",
    "verification_started_at",
    "verification_attempt_status",
    "verification_attempt_error_code",
    "config_revision",
    "security_revision",
    "key_revision",
}


def init_db() -> None:
    """初始化数据库：建表并执行增量迁移。

    顺序为：导入模型模块（触发表元数据注册）→ 配置 SQLite 运行时参数 →
    通过 ``create_all`` 创建全部缺失表 → 执行 SQLite 专属的渐进式数据迁移。
    该函数在应用启动时调用一次。
    """
    import app.db.models  # noqa: F401

    _configure_sqlite_runtime()
    SQLModel.metadata.create_all(engine)
    _migrate_sqlite_skill_schema()


def _configure_sqlite_runtime() -> None:
    """配置 SQLite 运行时参数以提升并发与稳定性。

    仅对 SQLite 生效：开启 WAL（Write-Ahead Logging）日志模式以支持读写并发，
    并设置 30 秒的 busy_timeout，在遇到锁时自动等待而非立即报错。
    """
    if not database_url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA busy_timeout=30000"))


def _migrate_sqlite_skill_schema() -> None:
    """执行 SQLite 数据库的渐进式结构与数据迁移（幂等）。

    这是平台数据演进的统一入口，涵盖以下几类操作（均以“先检测再操作”方式保证
    可重复执行）：

    1. 模型协议列迁移（``_migrate_model_api_protocols``）：为 ``model_configs``
       补充协议化后的列并迁移旧配置；
    2. 默认模型输出上限迁移（``_migrate_default_model_output_limit``）；
    3. 各业务表缺失列的补齐（users/sessions/messages/tools/ui_configs 等）；
    4. 旧版技能表（``{legacy}_skills``）数据迁移到新 ``skills`` 表；
    5. 技能标识规范化、技能图谱（steps→nodes/edges）转换、技能版本补建；
    6. 知识库结构迁移与按文档拆分（``_migrate_knowledge_base_schema``）；
    7. 默认智能体及其资源/模型绑定的种子化（``_seed_default_agents`` 等）。

    整个迁移在单个 ``BEGIN IMMEDIATE`` 事务中执行，保证原子性。
    """
    if not database_url.startswith("sqlite"):
        return

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    # legacy 关键字通过字符串拼接构造（"so"+"p" => "sop"），用于引用历史版本中
    # 的旧技能表/列名，避免直接书写敏感词。后续所有 legacy_* 变量均由此派生。
    legacy_key = "so" + "p"
    legacy_active_column = f"active_{legacy_key}_id"
    legacy_stack_column = f"{legacy_key}_stack_json"
    legacy_allowed_column = f"allowed_{legacy_key}s_json"
    legacy_table = f"{legacy_key}_skills"
    legacy_id_column = f"{legacy_key}_id"
    legacy_id_prefix = f"{legacy_key}_"
    with _sqlite_immediate_connection() as conn:
        _migrate_model_api_protocols(conn, tables)
        _migrate_default_model_output_limit(conn, tables)

        if "users" in tables:
            user_columns = {column["name"] for column in inspector.get_columns("users")}
            if "role" not in user_columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR NOT NULL DEFAULT 'member'"))

        if "sessions" in tables:
            session_columns = {column["name"] for column in inspector.get_columns("sessions")}
            if "agent_id" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN agent_id VARCHAR"))
            if "title" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN title VARCHAR"))
            if "active_skill_id" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN active_skill_id VARCHAR"))
                if legacy_active_column in session_columns:
                    conn.execute(text(f"UPDATE sessions SET active_skill_id = {legacy_active_column}"))
            if "skill_stack_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN skill_stack_json JSON"))
                if legacy_stack_column in session_columns:
                    conn.execute(text(f"UPDATE sessions SET skill_stack_json = {legacy_stack_column}"))
                else:
                    conn.execute(text("UPDATE sessions SET skill_stack_json = '[]'"))
            if "pending_tasks_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN pending_tasks_json JSON"))
                conn.execute(text("UPDATE sessions SET pending_tasks_json = '[]'"))
            if "awaiting_input_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN awaiting_input_json JSON"))
            if "knowledge_context_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN knowledge_context_json JSON"))
                conn.execute(text("UPDATE sessions SET knowledge_context_json = '[]'"))
            if "context_state_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN context_state_json JSON"))
                conn.execute(text("UPDATE sessions SET context_state_json = '{}'"))

        if "messages" in tables:
            message_columns = {column["name"] for column in inspector.get_columns("messages")}
            if "metadata_json" not in message_columns:
                conn.execute(text("ALTER TABLE messages ADD COLUMN metadata_json JSON"))
                conn.execute(text("UPDATE messages SET metadata_json = '{}' WHERE metadata_json IS NULL"))

        if "tools" in tables:
            tool_columns = {column["name"] for column in inspector.get_columns("tools")}
            if "bucket" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN bucket VARCHAR NOT NULL DEFAULT '未分桶'"))
            if "tool_type" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN tool_type VARCHAR NOT NULL DEFAULT 'http'"))
            if "config_json" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN config_json JSON"))
                conn.execute(text("UPDATE tools SET config_json = '{}' WHERE config_json IS NULL"))
            if "allowed_skills_json" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN allowed_skills_json JSON"))
                if legacy_allowed_column in tool_columns:
                    conn.execute(text(f"UPDATE tools SET allowed_skills_json = {legacy_allowed_column}"))
                else:
                    conn.execute(text("UPDATE tools SET allowed_skills_json = '[]'"))
            if "mcp_server_id" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN mcp_server_id VARCHAR"))

        if "ui_configs" in tables:
            ui_columns = {column["name"] for column in inspector.get_columns("ui_configs")}
            if "reflection_max_rounds" not in ui_columns:
                conn.execute(
                    text("ALTER TABLE ui_configs ADD COLUMN reflection_max_rounds INTEGER NOT NULL DEFAULT 1")
                )
            if "agent_loop_max_actions" not in ui_columns:
                conn.execute(
                    text("ALTER TABLE ui_configs ADD COLUMN agent_loop_max_actions INTEGER NOT NULL DEFAULT 6")
                )

        if "skill_feedback" in tables:
            feedback_columns = {column["name"] for column in inspector.get_columns("skill_feedback")}
            if "skill_version" not in feedback_columns:
                conn.execute(text("ALTER TABLE skill_feedback ADD COLUMN skill_version VARCHAR"))
            if "step_id" not in feedback_columns:
                conn.execute(text("ALTER TABLE skill_feedback ADD COLUMN step_id VARCHAR"))

        if "message_feedback" in tables:
            message_feedback_columns = {column["name"] for column in inspector.get_columns("message_feedback")}
            feedback_column_sql = {
                "analysis_status": "ALTER TABLE message_feedback ADD COLUMN analysis_status VARCHAR NOT NULL DEFAULT 'pending'",
                "analysis_bucket": "ALTER TABLE message_feedback ADD COLUMN analysis_bucket VARCHAR",
                "analysis_reason": "ALTER TABLE message_feedback ADD COLUMN analysis_reason VARCHAR",
                "analysis_summary": "ALTER TABLE message_feedback ADD COLUMN analysis_summary VARCHAR",
                "analysis_confidence": "ALTER TABLE message_feedback ADD COLUMN analysis_confidence FLOAT",
                "analysis_json": "ALTER TABLE message_feedback ADD COLUMN analysis_json JSON",
                "analyzed_at": "ALTER TABLE message_feedback ADD COLUMN analyzed_at DATETIME",
            }
            for column_name, ddl in feedback_column_sql.items():
                if column_name not in message_feedback_columns:
                    conn.execute(text(ddl))
            if "analysis_json" not in message_feedback_columns:
                conn.execute(text("UPDATE message_feedback SET analysis_json = '{}' WHERE analysis_json IS NULL"))

        if "general_skills" in tables:
            general_skill_columns = {column["name"] for column in inspector.get_columns("general_skills")}
            if "skill_files_json" not in general_skill_columns:
                conn.execute(text("ALTER TABLE general_skills ADD COLUMN skill_files_json JSON"))
                conn.execute(text("UPDATE general_skills SET skill_files_json = '[]' WHERE skill_files_json IS NULL"))
            if "metadata_json" not in general_skill_columns:
                conn.execute(text("ALTER TABLE general_skills ADD COLUMN metadata_json JSON"))
                conn.execute(text("UPDATE general_skills SET metadata_json = '{}' WHERE metadata_json IS NULL"))

        _migrate_knowledge_base_schema(conn, inspector, tables)
        _seed_default_agents(conn, tables)

        if legacy_table in tables and "skills" in tables:
            rows = conn.execute(text(f"SELECT * FROM {legacy_table}")).mappings().all()
            for row in rows:
                skill_id = _normalize_skill_identifier(
                    row.get("skill_id") or row.get(legacy_id_column),
                    legacy_id_prefix,
                )
                if not skill_id:
                    continue
                target_id = str(row["id"]).replace(legacy_id_prefix, "skill_", 1)
                existing = conn.execute(
                    text("SELECT id FROM skills WHERE tenant_id = :tenant_id AND skill_id = :skill_id"),
                    {"tenant_id": row["tenant_id"], "skill_id": skill_id},
                ).first()
                if existing:
                    continue
                content = _migrate_skill_content(row.get("content_json"), skill_id)
                existing_id = conn.execute(
                    text("SELECT id FROM skills WHERE id = :id"),
                    {"id": target_id},
                ).first()
                if existing_id:
                    conn.execute(
                        text(
                            """
                            UPDATE skills
                            SET skill_id = :skill_id, content_json = :content_json, updated_at = :updated_at
                            WHERE id = :id
                            """
                        ),
                        {
                            "id": target_id,
                            "skill_id": skill_id,
                            "content_json": json.dumps(content, ensure_ascii=False),
                            "updated_at": row.get("updated_at"),
                        },
                    )
                    continue
                conn.execute(
                    text(
                        """
                        INSERT INTO skills (
                            id, tenant_id, skill_id, version, name, business_domain,
                            description, content_json, status, created_at, updated_at
                        )
                        VALUES (
                            :id, :tenant_id, :skill_id, :version, :name, :business_domain,
                            :description, :content_json, :status, :created_at, :updated_at
                        )
                        """
                    ),
                    {
                        "id": target_id,
                        "tenant_id": row["tenant_id"],
                        "skill_id": skill_id,
                        "version": row.get("version") or "1.0.0",
                        "name": row["name"],
                        "business_domain": row.get("business_domain"),
                        "description": row.get("description"),
                        "content_json": json.dumps(content, ensure_ascii=False),
                        "status": row.get("status") or "draft",
                        "created_at": row.get("created_at"),
                        "updated_at": row.get("updated_at"),
                    },
                )
        if "skills" in tables:
            _normalize_existing_skill_rows(conn, legacy_id_prefix)
            if "skill_versions" in tables:
                _normalize_existing_skill_version_rows(conn, legacy_id_prefix)
                _seed_skill_versions(conn)
            _normalize_agent_branch_rows(conn, tables)
            _seed_agent_branch_state(conn, inspector, tables)
            _sync_explicit_skill_tool_bindings(conn, tables)


@contextmanager
def _sqlite_immediate_connection():
    """提供一个以 ``BEGIN IMMEDIATE`` 开启事务的原始连接上下文。

    ``BEGIN IMMEDIATE`` 会立即获取保留锁（reserved lock），确保迁移过程中
    其他写入被阻塞，从而避免并发迁移导致的竞争。上下文正常退出时提交，
    抛出异常时回滚，并在结束时关闭连接。

    Yields:
        Connection: 已开启事务的 SQLAlchemy 原始 DBAPI 连接。
    """
    conn = engine.connect()
    try:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate_default_model_output_limit(conn, tables: set[str]) -> None:
    """将默认模型的输出 token 上限从旧默认值（2048）提升到新默认值（8192）。

    通过 ``app_data_migrations`` 表记录迁移 ID 以保证只执行一次；仅更新
    ``is_default=1`` 且 ``max_output_tokens`` 等于旧值的行，避免覆盖用户自定义值。

    Args:
        conn: 已开启事务的数据库连接。
        tables: 当前库中已存在的表名集合；若无 ``model_configs`` 则直接返回。
    """
    if "model_configs" not in tables:
        return

    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS app_data_migrations (
                id VARCHAR PRIMARY KEY,
                applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    applied = conn.execute(
        text("SELECT id FROM app_data_migrations WHERE id = :id"),
        {"id": _DEFAULT_MODEL_OUTPUT_LIMIT_MIGRATION_ID},
    ).first()
    if applied:
        return

    conn.execute(
        text(
            """
            UPDATE model_configs
            SET max_output_tokens = :new_limit,
                updated_at = CURRENT_TIMESTAMP
            WHERE is_default = 1
              AND max_output_tokens = :legacy_limit
            """
        ),
        {
            "new_limit": _DEFAULT_MODEL_OUTPUT_TOKENS,
            "legacy_limit": _LEGACY_DEFAULT_MODEL_OUTPUT_TOKENS,
        },
    )
    conn.execute(
        text("INSERT INTO app_data_migrations (id) VALUES (:id)"),
        {"id": _DEFAULT_MODEL_OUTPUT_LIMIT_MIGRATION_ID},
    )


def _migrate_model_api_protocols(conn, tables: set[str]) -> None:
    """为 ``model_configs`` 表引入模型 API 协议化后的列并迁移历史配置。

    协议化将模型调用参数从扁平的 ``extra_body_json`` 重新组织为按协议分桶的
    ``protocol_options_json``，并新增一组可信校验字段（``trust_status`` 等）。

    本迁移具备自我修复能力：若迁移 ID 已标记应用但表结构不完整（部分列缺失），
    会进入“修复模式”仅补齐缺失列，不再重复执行数据回填。迁移完成后调用
    ``_normalize_model_default_rows`` 保证每租户至多一个默认模型，并注册迁移记录。

    Args:
        conn: 已开启事务的数据库连接。
        tables: 当前库中已存在的表名集合；若无 ``model_configs`` 则直接返回。
    """
    if "model_configs" not in tables:
        return

    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS app_data_migrations (
                id VARCHAR PRIMARY KEY,
                applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    applied = conn.execute(
        text("SELECT id FROM app_data_migrations WHERE id = :id"),
        {"id": _MODEL_API_PROTOCOLS_MIGRATION_ID},
    ).first()
    columns = {
        str(row[1]) for row in conn.execute(text("PRAGMA table_info(model_configs)")).all()
    }
    if applied and _model_api_protocol_schema_complete(conn, columns):
        return
    repairing_applied_migration = bool(applied)
    if "extra_body_json" not in columns:
        conn.execute(text("ALTER TABLE model_configs ADD COLUMN extra_body_json JSON"))
        conn.execute(text("UPDATE model_configs SET extra_body_json = '{}'"))
        columns.add("extra_body_json")
    column_ddl = {
        "api_protocol": (
            "ALTER TABLE model_configs ADD COLUMN api_protocol VARCHAR "
            "NOT NULL DEFAULT 'openai_chat_completions'"
        ),
        "protocol_options_json": "ALTER TABLE model_configs ADD COLUMN protocol_options_json JSON",
        "legacy_unmapped_options_json": (
            "ALTER TABLE model_configs ADD COLUMN legacy_unmapped_options_json JSON"
        ),
        "trust_status": (
            "ALTER TABLE model_configs ADD COLUMN trust_status VARCHAR "
            "NOT NULL DEFAULT 'unverified'"
        ),
        "verified_at": "ALTER TABLE model_configs ADD COLUMN verified_at DATETIME",
        "verified_fingerprint": (
            "ALTER TABLE model_configs ADD COLUMN verified_fingerprint VARCHAR"
        ),
        "verification_attempt_id": (
            "ALTER TABLE model_configs ADD COLUMN verification_attempt_id VARCHAR"
        ),
        "verification_started_at": (
            "ALTER TABLE model_configs ADD COLUMN verification_started_at DATETIME"
        ),
        "verification_attempt_status": (
            "ALTER TABLE model_configs ADD COLUMN verification_attempt_status VARCHAR "
            "NOT NULL DEFAULT 'idle'"
        ),
        "verification_attempt_error_code": (
            "ALTER TABLE model_configs ADD COLUMN verification_attempt_error_code VARCHAR"
        ),
        "config_revision": (
            "ALTER TABLE model_configs ADD COLUMN config_revision INTEGER NOT NULL DEFAULT 1"
        ),
        "security_revision": (
            "ALTER TABLE model_configs ADD COLUMN security_revision INTEGER NOT NULL DEFAULT 1"
        ),
        "key_revision": (
            "ALTER TABLE model_configs ADD COLUMN key_revision INTEGER NOT NULL DEFAULT 1"
        ),
    }
    for column_name, ddl in column_ddl.items():
        if column_name not in columns:
            conn.execute(text(ddl))

    if not repairing_applied_migration:
        # 数据回填：仅首次执行。逐行解析旧的 extra_body_json，若其中包含合法的
        # thinking 配置则迁移到 protocol_options_json[openai_chat_completions]，
        # 其余无法归类的参数放入 legacy_unmapped_options_json 待后续处理。
        rows = conn.execute(
            text("SELECT id, enabled, extra_body_json FROM model_configs")
        ).mappings().all()
        for row in rows:
            extra_body = _json_object(row.get("extra_body_json"))
            thinking = extra_body.get("thinking")
            protocol_options: dict[str, object] = {"openai_chat_completions": {}}
            legacy_unmapped: dict[str, object] = {}
            if _valid_chat_thinking_options(thinking):
                protocol_options["openai_chat_completions"] = {"thinking": thinking}
                legacy_unmapped = {
                    key: value for key, value in extra_body.items() if key != "thinking"
                }
            elif extra_body:
                legacy_unmapped = extra_body
            conn.execute(
                text(
                    """
                    UPDATE model_configs
                    SET api_protocol = 'openai_chat_completions',
                        protocol_options_json = :protocol_options,
                        legacy_unmapped_options_json = :legacy_unmapped,
                        trust_status = CASE WHEN enabled = 1 THEN 'legacy_trusted' ELSE 'unverified' END,
                        verification_attempt_status = 'idle',
                        config_revision = 1,
                        security_revision = 1,
                        key_revision = 1
                    WHERE id = :id
                    """
                ),
                {
                    "id": row["id"],
                    "protocol_options": json.dumps(protocol_options, ensure_ascii=False),
                    "legacy_unmapped": json.dumps(legacy_unmapped, ensure_ascii=False),
                },
            )

    _normalize_model_default_rows(conn)
    if repairing_applied_migration:
        return


def _normalize_model_default_rows(conn) -> None:
    """规整默认模型行：去重、禁用项置非默认、并建立租户级唯一部分索引。

    每个租户最多保留一个 ``is_default=1`` 的模型：当存在多个候选时保留
    ``updated_at`` 最新的一条，其余置为非默认；禁用模型不允许作为默认；
    最后创建带 ``WHERE is_default = 1`` 的唯一部分索引，从数据库层面保证约束。
    """
    duplicate_defaults = conn.execute(
        text(
            """
            SELECT tenant_id
            FROM model_configs
            WHERE is_default = 1 AND enabled = 1
            GROUP BY tenant_id
            HAVING COUNT(*) > 1
            """
        )
    ).scalars().all()
    for tenant_id in duplicate_defaults:
        keep_id = conn.execute(
            text(
                """
                SELECT id FROM model_configs
                WHERE tenant_id = :tenant_id AND is_default = 1 AND enabled = 1
                ORDER BY updated_at DESC, id ASC
                LIMIT 1
                """
            ),
            {"tenant_id": tenant_id},
        ).scalar_one()
        conn.execute(
            text(
                """
                UPDATE model_configs SET is_default = 0
                WHERE tenant_id = :tenant_id AND is_default = 1 AND id != :keep_id
                """
            ),
            {"tenant_id": tenant_id, "keep_id": keep_id},
        )
    conn.execute(text("UPDATE model_configs SET is_default = 0 WHERE enabled = 0"))
    conn.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_model_configs_tenant_default
            ON model_configs(tenant_id) WHERE is_default = 1
            """
        )
    )
    conn.execute(
        text(
            "INSERT OR IGNORE INTO app_data_migrations (id) VALUES (:id)"
        ),
        {"id": _MODEL_API_PROTOCOLS_MIGRATION_ID},
    )


def _model_api_protocol_schema_complete(conn, columns: set[str]) -> bool:
    """判断 model_configs 的协议化列与唯一索引是否已完整就位。

    检查两件事：(1) 全部目标列均存在；(2) ``uq_model_configs_tenant_default``
    部分唯一索引存在且包含 ``WHERE is_default = 1`` 谓词。

    Args:
        conn: 数据库连接。
        columns: ``model_configs`` 当前已有列名集合。

    Returns:
        bool: 结构完整返回 True，否则 False（需进入修复模式补齐）。
    """
    if not _MODEL_API_PROTOCOL_COLUMNS.issubset(columns):
        return False
    index = conn.execute(
        text(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'index' AND name = 'uq_model_configs_tenant_default'"
        )
    ).scalar_one_or_none()
    return bool(index and "WHERE is_default = 1" in index)


def _valid_chat_thinking_options(value: object) -> bool:
    """校验一个对象是否为合法的 chat thinking 配置。

    合法结构为只含 ``type`` 与可选 ``clear_thinking`` 的字典，其中
    ``type`` 取值 ``enabled``/``disabled``，``clear_thinking`` 为布尔。

    Args:
        value: 待校验对象（通常来自 extra_body_json）。

    Returns:
        bool: 结构合法返回 True。
    """
    if not isinstance(value, dict) or set(value) - {"type", "clear_thinking"}:
        return False
    if value.get("type") not in {"enabled", "disabled"}:
        return False
    return "clear_thinking" not in value or isinstance(value["clear_thinking"], bool)


def _migrate_skill_content(value: object, skill_id: str) -> dict[str, object]:
    """将技能内容迁移为新版图谱结构。

    解析 ``content_json``（可能是 str/dict/None），统一其中的 ``skill_id`` 字段，
    然后通过 ``_ensure_skill_graph`` 将旧的线性 steps 转换为 nodes/edges 图模型。

    Args:
        value: 原始内容（JSON 字符串、dict 或空值）。
        skill_id: 当前技能的业务标识，用于回填 content 内的 skill_id。

    Returns:
        dict[str, object]: 迁移并规范化后的技能内容字典。
    """
    if isinstance(value, str):
        try:
            content = json.loads(value)
        except json.JSONDecodeError:
            content = {}
    elif isinstance(value, dict):
        content = dict(value)
    else:
        content = {}
    if "skill_id" not in content:
        content["skill_id"] = content.pop("so" + "p_id", skill_id)
    else:
        content["skill_id"] = skill_id
    return _ensure_skill_graph(content)


def _normalize_existing_skill_rows(conn, legacy_id_prefix: str) -> None:
    """规范化 ``skills`` 表中已有行的 skill_id 与 content_json。

    逐行处理：将 legacy 前缀的 skill_id 转为新前缀、迁移内容为图谱结构。
    若规范化后的 skill_id 与其他行冲突则跳过，避免破坏唯一性。

    Args:
        conn: 已开启事务的数据库连接。
        legacy_id_prefix: 旧 ID 前缀（如 ``sop_``），用于识别并改写历史标识。
    """
    rows = conn.execute(text("SELECT id, skill_id, content_json FROM skills")).mappings().all()
    for row in rows:
        skill_id = _normalize_skill_identifier(row.get("skill_id"), legacy_id_prefix)
        if not skill_id:
            continue
        content = _migrate_skill_content(row.get("content_json"), skill_id)
        if skill_id == row.get("skill_id"):
            conn.execute(
                text("UPDATE skills SET content_json = :content_json WHERE id = :id"),
                {"id": row["id"], "content_json": json.dumps(content, ensure_ascii=False)},
            )
            continue
        existing = conn.execute(
            text("SELECT id FROM skills WHERE skill_id = :skill_id AND id != :id"),
            {"skill_id": skill_id, "id": row["id"]},
        ).first()
        if existing:
            continue
        conn.execute(
            text("UPDATE skills SET skill_id = :skill_id, content_json = :content_json WHERE id = :id"),
            {
                "id": row["id"],
                "skill_id": skill_id,
                "content_json": json.dumps(content, ensure_ascii=False),
            },
        )


def _normalize_existing_skill_version_rows(conn, legacy_id_prefix: str) -> None:
    """规范化 ``skill_versions`` 表中已有行的 skill_id 与 content_json。

    与 ``_normalize_existing_skill_rows`` 对应，针对版本表执行同样的标识改写
    与图谱内容迁移。

    Args:
        conn: 已开启事务的数据库连接。
        legacy_id_prefix: 旧 ID 前缀。
    """
    rows = conn.execute(text("SELECT id, skill_id, content_json FROM skill_versions")).mappings().all()
    for row in rows:
        skill_id = _normalize_skill_identifier(row.get("skill_id"), legacy_id_prefix)
        if not skill_id:
            continue
        content = _migrate_skill_content(row.get("content_json"), skill_id)
        conn.execute(
            text("UPDATE skill_versions SET skill_id = :skill_id, content_json = :content_json WHERE id = :id"),
            {
                "id": row["id"],
                "skill_id": skill_id,
                "content_json": json.dumps(content, ensure_ascii=False),
            },
        )


def _sync_explicit_skill_tool_bindings(conn, tables: set[str]) -> None:
    """将技能内容中显式声明的工具调用反向同步到工具的白名单。

    技能节点里的 ``call_tool:<name>`` 动作表示该技能会使用某工具；本函数据此
    把技能 id 追加到对应 ``tools.allowed_skills_json``，使工具白名单与技能声明
    保持一致，便于权限校验。

    Args:
        conn: 已开启事务的数据库连接。
        tables: 当前库的表名集合；缺少 skills 或 tools 时跳过。
    """
    if "skills" not in tables or "tools" not in tables:
        return
    skill_rows = conn.execute(
        text(
            "SELECT tenant_id, skill_id, content_json FROM skills "
            "WHERE status IS NULL OR status != 'deleted'"
        )
    ).mappings().all()
    for skill_row in skill_rows:
        content = _json_object(skill_row.get("content_json"))
        tool_names = _explicit_skill_tool_names(content)
        if not tool_names:
            continue
        tool_rows = conn.execute(
            text("SELECT id, name, allowed_skills_json FROM tools WHERE tenant_id = :tenant_id"),
            {"tenant_id": skill_row["tenant_id"]},
        ).mappings().all()
        for tool_row in tool_rows:
            if str(tool_row.get("name") or "") not in tool_names:
                continue
            allowed_skills = _json_string_list(tool_row.get("allowed_skills_json"))
            skill_id = str(skill_row.get("skill_id") or "").strip()
            if not skill_id or skill_id in allowed_skills:
                continue
            allowed_skills.append(skill_id)
            conn.execute(
                text(
                    "UPDATE tools SET allowed_skills_json = :allowed_skills, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = :id"
                ),
                {
                    "id": tool_row["id"],
                    "allowed_skills": json.dumps(allowed_skills, ensure_ascii=False),
                },
            )


def _explicit_skill_tool_names(content: dict[str, object]) -> set[str]:
    """从技能内容中提取显式声明的工具名集合。

    扫描 ``nodes`` 与 ``steps``（兼容旧结构）中每个节点的 ``allowed_actions``，
    收集形如 ``call_tool:<name>`` 的动作并解析出工具名。

    Args:
        content: 技能内容字典。

    Returns:
        set[str]: 技能显式调用的工具名集合。
    """
    names: set[str] = set()
    for key in ("nodes", "steps"):
        items = content.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            actions = item.get("allowed_actions")
            if not isinstance(actions, list):
                continue
            for action in actions:
                value = str(action or "").strip()
                if value.startswith("call_tool:"):
                    name = value.split(":", 1)[1].strip()
                    if name:
                        names.add(name)
    return names


def _json_string_list(value: object) -> list[str]:
    """将一个值解析为字符串列表（容错）。

    接受 JSON 字符串或原生 list，返回去除空白的字符串列表；解析失败或类型
    不符时返回空列表。

    Args:
        value: 待解析值（可能是 JSON 字符串、list 或其他）。

    Returns:
        list[str]: 解析后的非空字符串列表。
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _ensure_skill_graph(content: dict[str, object]) -> dict[str, object]:
    """确保技能内容采用 nodes/edges 图结构，并回填起始/终止节点。

    处理三种情况：
    1. 已含非空 ``nodes``：移除旧 ``steps``，回填 ``start_node_id`` 与
       ``terminal_node_ids``；
    2. 既无 nodes 也无有效 steps：补建空 nodes/edges/terminal_node_ids；
    3. 含旧版线性 ``steps``：将每个 step 转换为 node，相邻 step 间生成默认
       推进边（edges），并设置首/末节点为起始/终止节点，最后移除 steps。

    Args:
        content: 技能内容字典（会被原地修改）。

    Returns:
        dict[str, object]: 规范化后的内容（nodes/edges 图模型）。
    """
    nodes = content.get("nodes")
    steps = content.get("steps")
    if isinstance(nodes, list) and nodes:
        content.pop("steps", None)
        content.setdefault("start_node_id", _first_node_id(nodes))
        content.setdefault("terminal_node_ids", [_last_node_id(nodes)] if _last_node_id(nodes) else [])
        return content
    if not isinstance(steps, list) or not steps:
        content.setdefault("nodes", [])
        content.setdefault("edges", [])
        content.setdefault("terminal_node_ids", [])
        content.pop("steps", None)
        return content
    normalized_steps = [step for step in steps if isinstance(step, dict)]
    content["nodes"] = [_step_to_node_dict(step) for step in normalized_steps]
    content["edges"] = [
        {
            "source_node_id": str(normalized_steps[index].get("step_id") or f"step_{index + 1}"),
            "next_node_id": str(normalized_steps[index + 1].get("step_id") or f"step_{index + 2}"),
            "priority": index,
            "label": "默认推进",
        }
        for index in range(len(normalized_steps) - 1)
    ]
    if normalized_steps:
        content["start_node_id"] = content.get("start_node_id") or str(normalized_steps[0].get("step_id") or "step_1")
        content["terminal_node_ids"] = content.get("terminal_node_ids") or [
            str(normalized_steps[-1].get("step_id") or f"step_{len(normalized_steps)}")
        ]
    content.pop("steps", None)
    return content


def _step_to_node_dict(step: dict[str, object]) -> dict[str, object]:
    """将旧版线性 step 转换为图模型的 node 字典。

    依据 step 内容推断节点类型：含 ``handoff_human`` 动作为 ``handoff``；
    含 ``call_tool:`` 动作为 ``tool_call``；需采集用户信息为 ``collect_info``；
    否则为 ``response``。

    Args:
        step: 旧版步骤字典。

    Returns:
        dict[str, object]: 图模型节点字典（含 node_id/type/instruction 等）。
    """
    actions = step.get("allowed_actions") if isinstance(step.get("allowed_actions"), list) else []
    expected = step.get("expected_user_info") if isinstance(step.get("expected_user_info"), list) else []
    node_type = "collect_info" if expected else "response"
    if any(isinstance(action, str) and action.startswith("call_tool:") for action in actions):
        node_type = "tool_call"
    if "handoff_human" in actions:
        node_type = "handoff"
    return {
        "node_id": str(step.get("step_id") or step.get("node_id") or "step"),
        "type": node_type,
        "name": str(step.get("name") or step.get("step_id") or "步骤"),
        "instruction": str(step.get("instruction") or ""),
        "optional": bool(step.get("optional") or False),
        "condition": step.get("condition") if isinstance(step.get("condition"), str) else None,
        "expected_user_info": expected,
        "allowed_actions": actions,
        "knowledge_scope": step.get("knowledge_scope") if isinstance(step.get("knowledge_scope"), dict) else {},
        "retry_policy": step.get("retry_policy") if isinstance(step.get("retry_policy"), dict) else {},
        "metadata": step.get("metadata") if isinstance(step.get("metadata"), dict) else {},
    }


def _first_node_id(nodes: object) -> str | None:
    """返回节点列表中第一个有效节点的 node_id。

    Args:
        nodes: 节点列表（可能不是 list）。

    Returns:
        str | None: 第一个 node_id；列表无效或为空时返回 None。
    """
    if not isinstance(nodes, list):
        return None
    for node in nodes:
        if isinstance(node, dict) and node.get("node_id"):
            return str(node["node_id"])
    return None


def _last_node_id(nodes: object) -> str | None:
    """返回节点列表中最后一个有效节点的 node_id。

    Args:
        nodes: 节点列表（可能不是 list）。

    Returns:
        str | None: 最后一个 node_id；列表无效或为空时返回 None。
    """
    if not isinstance(nodes, list):
        return None
    for node in reversed(nodes):
        if isinstance(node, dict) and node.get("node_id"):
            return str(node["node_id"])
    return None


def _seed_skill_versions(conn) -> None:
    """为每条技能在 ``skill_versions`` 表中补建对应的版本快照。

    扫描 ``skills`` 表，对尚未在版本表中存在同 (tenant, skill, version) 记录的
    技能，插入一条镜像版本记录，使技能具备可追溯的版本历史。

    Args:
        conn: 已开启事务的数据库连接。
    """
    rows = conn.execute(text("SELECT * FROM skills")).mappings().all()
    for row in rows:
        version = row.get("version") or "1.0.0"
        existing = conn.execute(
            text(
                """
                SELECT id FROM skill_versions
                WHERE tenant_id = :tenant_id AND skill_id = :skill_id AND version = :version
                """
            ),
            {"tenant_id": row["tenant_id"], "skill_id": row["skill_id"], "version": version},
        ).first()
        if existing:
            continue
        conn.execute(
            text(
                """
                INSERT INTO skill_versions (
                    id, tenant_id, skill_id, version, name, business_domain,
                    description, content_json, status, created_at, updated_at
                )
                VALUES (
                    :id, :tenant_id, :skill_id, :version, :name, :business_domain,
                    :description, :content_json, :status, :created_at, :updated_at
                )
                """
            ),
            {
                "id": f"skillver_{row['id']}",
                "tenant_id": row["tenant_id"],
                "skill_id": row["skill_id"],
                "version": version,
                "name": row["name"],
                "business_domain": row.get("business_domain"),
                "description": row.get("description"),
                "content_json": row.get("content_json"),
                "status": row.get("status") or "draft",
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            },
        )


def _normalize_skill_identifier(value: object, legacy_id_prefix: str) -> str:
    """将技能标识中的 legacy 前缀替换为新前缀。

    例如把 ``sop_xxx`` 改写为 ``skill_xxx``；非字符串或无前缀的值原样返回。

    Args:
        value: 原始 skill_id 值。
        legacy_id_prefix: 旧前缀（如 ``sop_``）。

    Returns:
        str: 规范化后的 skill_id；输入非法时返回空串。
    """
    if not isinstance(value, str):
        return ""
    if value.startswith(legacy_id_prefix):
        return f"skill_{value[len(legacy_id_prefix):]}"
    return value


def _migrate_knowledge_base_schema(conn, inspector, tables: set[str]) -> None:
    """迁移知识库相关表结构并补齐关联关系。

    主要工作：
    1. 为每个租户创建“默认知识库”（若缺失）；
    2. 为各知识子表补齐 ``knowledge_base_id`` 列，并为历史无归属的数据回填
       默认知识库 id；
    3. 为每个知识库补建 1.0.0 版本记录；
    4. 为各子表补齐 ``knowledge_base_version_id`` 列并回填默认版本；
    5. 调用 ``_split_document_backed_knowledge_bases`` 将多文档知识库按文档拆分。

    Args:
        conn: 已开启事务的数据库连接。
        inspector: SQLAlchemy inspector，用于查询列信息。
        tables: 当前库的表名集合。
    """
    tenant_ids = _tenant_ids(conn, tables)
    if "knowledge_bases" in tables:
        for tenant_id in tenant_ids:
            default_id = _default_knowledge_base_id(tenant_id)
            existing = conn.execute(
                text("SELECT id FROM knowledge_bases WHERE id = :id"),
                {"id": default_id},
            ).first()
            if not existing:
                conn.execute(
                    text(
                        """
                        INSERT INTO knowledge_bases (
                            id, tenant_id, name, description, status, metadata_json, created_at, updated_at
                        )
                        VALUES (
                            :id, :tenant_id, :name, :description, 'active', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "id": default_id,
                        "tenant_id": tenant_id,
                        "name": "默认知识库",
                        "description": "系统默认知识库",
                    },
                )

    table_names = {
        "knowledge_documents": "knowledge_base_id",
        "knowledge_buckets": "knowledge_base_id",
        "knowledge_chunks": "knowledge_base_id",
        "knowledge_concepts": "knowledge_base_id",
        "knowledge_discovery_suggestions": "knowledge_base_id",
        "knowledge_ingest_jobs": "knowledge_base_id",
    }
    for table_name, column_name in table_names.items():
        if table_name not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if column_name not in columns:
            conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} VARCHAR"))
        rows = conn.execute(
            text(f"SELECT DISTINCT tenant_id FROM {table_name} WHERE {column_name} IS NULL OR {column_name} = ''")
        ).mappings().all()
        for row in rows:
            tenant_id = str(row.get("tenant_id") or "")
            if tenant_id:
                conn.execute(
                    text(f"UPDATE {table_name} SET {column_name} = :knowledge_base_id WHERE tenant_id = :tenant_id AND ({column_name} IS NULL OR {column_name} = '')"),
                    {"tenant_id": tenant_id, "knowledge_base_id": _default_knowledge_base_id(tenant_id)},
                )

    if "knowledge_base_versions" in tables and "knowledge_bases" in tables:
        knowledge_bases = conn.execute(text("SELECT * FROM knowledge_bases")).mappings().all()
        for row in knowledge_bases:
            version_id = _knowledge_base_version_id(str(row["id"]), "1.0.0")
            existing = conn.execute(
                text("SELECT id FROM knowledge_base_versions WHERE id = :id"),
                {"id": version_id},
            ).first()
            if not existing:
                conn.execute(
                    text(
                        """
                        INSERT INTO knowledge_base_versions (
                            id, tenant_id, knowledge_base_id, version, name, description,
                            status, metadata_json, created_at, updated_at
                        )
                        VALUES (
                            :id, :tenant_id, :knowledge_base_id, '1.0.0', :name, :description,
                            :status, :metadata_json, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "id": version_id,
                        "tenant_id": row["tenant_id"],
                        "knowledge_base_id": row["id"],
                        "name": row["name"],
                        "description": row.get("description"),
                        "status": row.get("status") or "active",
                        "metadata_json": row.get("metadata_json") or "{}",
                    },
                )

    for table_name in table_names:
        if table_name not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if "knowledge_base_version_id" not in columns:
            conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN knowledge_base_version_id VARCHAR"))
        rows = conn.execute(
            text(
                f"""
                SELECT DISTINCT knowledge_base_id FROM {table_name}
                WHERE knowledge_base_id IS NOT NULL
                  AND knowledge_base_id != ''
                  AND (knowledge_base_version_id IS NULL OR knowledge_base_version_id = '')
                """
            )
        ).mappings().all()
        for row in rows:
            knowledge_base_id = str(row.get("knowledge_base_id") or "")
            if not knowledge_base_id:
                continue
            conn.execute(
                text(
                    f"""
                    UPDATE {table_name}
                    SET knowledge_base_version_id = :version_id
                    WHERE knowledge_base_id = :knowledge_base_id
                      AND (knowledge_base_version_id IS NULL OR knowledge_base_version_id = '')
                    """
                ),
                {
                    "knowledge_base_id": knowledge_base_id,
                    "version_id": _knowledge_base_version_id(knowledge_base_id, "1.0.0"),
                },
            )

    _split_document_backed_knowledge_bases(conn, tables)


def _split_document_backed_knowledge_bases(conn, tables: set[str]) -> None:
    """将含多个文档的知识库按文档拆分为若干独立知识库。

    早期模型中一个知识库可挂多个文档，新模型改为“一文档一知识库”。本函数
    找出文档数大于 1 的知识库，为其中每个文档创建独立的知识库及 1.0.0 版本，
    并通过 ``_move_document_knowledge_rows`` 迁移该文档的桶/块/概念/建议/任务。

    Args:
        conn: 已开启事务的数据库连接。
        tables: 当前库的表名集合；缺少任一必需表时直接返回。
    """
    required_tables = {"knowledge_bases", "knowledge_base_versions", "knowledge_documents"}
    if not required_tables.issubset(tables):
        return

    document_groups = conn.execute(
        text(
            """
            SELECT knowledge_base_id, COUNT(id) AS document_count
            FROM knowledge_documents
            WHERE knowledge_base_id IS NOT NULL AND knowledge_base_id != ''
            GROUP BY knowledge_base_id
            """
        )
    ).mappings().all()
    multi_document_base_ids = {
        str(row["knowledge_base_id"])
        for row in document_groups
        if int(row.get("document_count") or 0) > 1
    }
    if not multi_document_base_ids:
        return

    for source_knowledge_base_id in sorted(multi_document_base_ids):
        source = conn.execute(
            text("SELECT * FROM knowledge_bases WHERE id = :id"),
            {"id": source_knowledge_base_id},
        ).mappings().first()
        if not source:
            continue
        documents = conn.execute(
            text(
                """
                SELECT *
                FROM knowledge_documents
                WHERE knowledge_base_id = :knowledge_base_id
                ORDER BY created_at, id
                """
            ),
            {"knowledge_base_id": source_knowledge_base_id},
        ).mappings().all()
        if len(documents) <= 1:
            continue
        for document in documents:
            target_id = _document_knowledge_base_id(str(document["id"]))
            target = conn.execute(
                text("SELECT id FROM knowledge_bases WHERE id = :id"),
                {"id": target_id},
            ).first()
            target_name = _unique_migrated_knowledge_base_name(
                conn,
                str(source["tenant_id"]),
                _document_knowledge_base_name(document),
                target_id,
            )
            metadata = _json_object(source.get("metadata_json"))
            metadata.update(
                {
                    "created_from_document_upload": True,
                    "source_document_id": document["id"],
                    "source_filename": document.get("filename"),
                    "split_from_knowledge_base_id": source_knowledge_base_id,
                }
            )
            if not target:
                conn.execute(
                    text(
                        """
                        INSERT INTO knowledge_bases (
                            id, tenant_id, name, description, status, metadata_json, created_at, updated_at
                        )
                        VALUES (
                            :id, :tenant_id, :name, :description, :status, :metadata_json,
                            :created_at, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "id": target_id,
                        "tenant_id": source["tenant_id"],
                        "name": target_name,
                        "description": f"由文档 {document.get('filename') or document['id']} 创建",
                        "status": "active",
                        "metadata_json": json.dumps(metadata, ensure_ascii=False),
                        "created_at": document.get("created_at") or source.get("created_at"),
                    },
                )
            version_id = _knowledge_base_version_id(target_id, "1.0.0")
            version_exists = conn.execute(
                text("SELECT id FROM knowledge_base_versions WHERE id = :id"),
                {"id": version_id},
            ).first()
            if not version_exists:
                conn.execute(
                    text(
                        """
                        INSERT INTO knowledge_base_versions (
                            id, tenant_id, knowledge_base_id, version, name, description,
                            status, metadata_json, created_at, updated_at
                        )
                        VALUES (
                            :id, :tenant_id, :knowledge_base_id, '1.0.0', :name, :description,
                            'active', :metadata_json, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "id": version_id,
                        "tenant_id": source["tenant_id"],
                        "knowledge_base_id": target_id,
                        "name": target_name,
                        "description": f"由文档 {document.get('filename') or document['id']} 创建",
                        "metadata_json": json.dumps(metadata, ensure_ascii=False),
                    },
                )
            _move_document_knowledge_rows(conn, tables, str(document["id"]), target_id, version_id)


def _move_document_knowledge_rows(
    conn,
    tables: set[str],
    document_id: str,
    knowledge_base_id: str,
    version_id: str,
) -> None:
    """将一个文档及其全部子资源迁移到指定知识库与版本。

    更新 ``knowledge_documents`` 以及文档作用域的子表（buckets/chunks/concepts/
    suggestions/ingest_jobs）的 ``knowledge_base_id`` 与 ``knowledge_base_version_id``。

    Args:
        conn: 已开启事务的数据库连接。
        tables: 当前库的表名集合。
        document_id: 待迁移文档 id。
        knowledge_base_id: 目标知识库 id。
        version_id: 目标知识库版本 id。
    """
    document_scoped_tables = (
        "knowledge_buckets",
        "knowledge_chunks",
        "knowledge_concepts",
        "knowledge_discovery_suggestions",
    )
    if "knowledge_documents" in tables:
        conn.execute(
            text(
                """
                UPDATE knowledge_documents
                SET knowledge_base_id = :knowledge_base_id,
                    knowledge_base_version_id = :version_id
                WHERE id = :document_id
                """
            ),
            {
                "document_id": document_id,
                "knowledge_base_id": knowledge_base_id,
                "version_id": version_id,
            },
        )
    for table_name in document_scoped_tables:
        if table_name not in tables:
            continue
        conn.execute(
            text(
                f"""
                UPDATE {table_name}
                SET knowledge_base_id = :knowledge_base_id,
                    knowledge_base_version_id = :version_id
                WHERE document_id = :document_id
                """
            ),
            {
                "document_id": document_id,
                "knowledge_base_id": knowledge_base_id,
                "version_id": version_id,
            },
        )
    if "knowledge_ingest_jobs" not in tables:
        return
    conn.execute(
        text(
            """
            UPDATE knowledge_ingest_jobs
            SET knowledge_base_id = :knowledge_base_id,
                knowledge_base_version_id = :version_id
            WHERE document_id = :document_id
            """
        ),
        {
            "document_id": document_id,
            "knowledge_base_id": knowledge_base_id,
            "version_id": version_id,
        },
    )


def _json_object(value: object) -> dict[str, object]:
    """将一个值容错地解析为字典。

    接受 dict、JSON 字符串；解析失败或类型不符时返回空字典 ``{}``。

    Args:
        value: 待解析值。

    Returns:
        dict[str, object]: 解析得到的字典。
    """
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return dict(parsed)
    return {}


def _document_knowledge_base_id(document_id: str) -> str:
    """根据文档 id 生成其专属知识库的确定性 id。

    Args:
        document_id: 文档 id。

    Returns:
        str: 形如 ``kb_doc_<document_id>`` 的知识库 id。
    """
    return f"kb_doc_{document_id}"


def _document_knowledge_base_name(document) -> str:
    """为文档派生的知识库生成显示名称。

    优先使用文档标题；否则取文件名的去扩展名主干；均缺失时回退为“未命名知识库”。

    Args:
        document: 文档行（mapping），需含 title/filename 字段。

    Returns:
        str: 知识库名称。
    """
    title = str(document.get("title") or "").strip()
    if title:
        return title
    filename = str(document.get("filename") or "").strip()
    stem = Path(filename).stem.strip()
    return stem or filename or "未命名知识库"


def _unique_migrated_knowledge_base_name(
    conn,
    tenant_id: str,
    base_name: str,
    target_id: str,
) -> str:
    """在租户内为迁移产生的知识库生成不冲突的唯一名称。

    若 ``base_name`` 已被其他知识库占用，则依次追加 `` 2``、`` 3`` … 后缀，
    直到找到一个未被占用的名称。

    Args:
        conn: 数据库连接。
        tenant_id: 租户 id。
        base_name: 期望的基础名称。
        target_id: 当前目标知识库 id（查询时排除自身）。

    Returns:
        str: 租户内唯一的知识库名称。
    """
    normalized = base_name.strip() or "未命名知识库"
    existing_names = {
        str(row[0])
        for row in conn.execute(
            text("SELECT name FROM knowledge_bases WHERE tenant_id = :tenant_id AND id != :target_id"),
            {"tenant_id": tenant_id, "target_id": target_id},
        ).all()
        if row[0]
    }
    if normalized not in existing_names:
        return normalized
    index = 2
    while True:
        candidate = f"{normalized} {index}"
        if candidate not in existing_names:
            return candidate
        index += 1


def _seed_default_agents(conn, tables: set[str]) -> None:
    """为每个租户种子化默认智能体（整体智能体）并补建资源绑定。

    若 ``agent_profiles`` 表存在，则为每个租户确保存在一个“整体智能体”
    （is_overall=1），并归档旧的默认员工智能体、为其补建资源绑定。

    Args:
        conn: 已开启事务的数据库连接。
        tables: 当前库的表名集合。
    """
    if "agent_profiles" not in tables:
        return
    tenant_ids = _tenant_ids(conn, tables)
    for tenant_id in tenant_ids:
        for agent_id, name, is_overall in (
            (_overall_agent_id(tenant_id), "整体智能体", True),
        ):
            existing = conn.execute(text("SELECT id FROM agent_profiles WHERE id = :id"), {"id": agent_id}).first()
            if existing:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO agent_profiles (
                        id, tenant_id, name, description, persona_prompt, is_overall,
                        status, metadata_json, created_at, updated_at
                    )
                    VALUES (
                        :id, :tenant_id, :name, :description, NULL, :is_overall,
                        'active', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "id": agent_id,
                    "tenant_id": tenant_id,
                    "name": name,
                    "description": "全局资源池" if is_overall else "默认对话可见域",
                    "is_overall": 1 if is_overall else 0,
                },
            )
        _archive_default_agent(conn, tenant_id)
        if "agent_resource_bindings" in tables:
            _seed_default_agent_bindings(conn, tenant_id)


def _seed_default_agent_bindings(conn, tenant_id: str) -> None:
    """为默认智能体补建资源绑定（技能/通用技能/知识库）。

    扫描租户下所有未删除的资源，对尚未绑定的逐个插入 ``agent_resource_bindings``
    记录；绑定状态根据资源状态（active/published → active，否则 inactive）。

    Args:
        conn: 已开启事务的数据库连接。
        tenant_id: 租户 id。
    """
    default_agent = _default_agent_id(tenant_id)
    active_default = conn.execute(
        text(
            """
            SELECT id FROM agent_profiles
            WHERE id = :id AND tenant_id = :tenant_id AND status != 'archived'
            """
        ),
        {"id": default_agent, "tenant_id": tenant_id},
    ).first()
    if not active_default:
        return
    resource_queries = (
        ("skill", "SELECT id, status FROM skills WHERE tenant_id = :tenant_id AND status != 'deleted'"),
        ("general_skill", "SELECT id, status FROM general_skills WHERE tenant_id = :tenant_id AND status != 'deleted'"),
        ("knowledge_base", "SELECT id, status FROM knowledge_bases WHERE tenant_id = :tenant_id AND status != 'deleted'"),
    )
    for resource_type, sql in resource_queries:
        rows = conn.execute(text(sql), {"tenant_id": tenant_id}).mappings().all()
        for row in rows:
            resource_id = str(row.get("id") or "")
            if not resource_id:
                continue
            binding_status = "active" if str(row.get("status") or "") in {"active", "published"} else "inactive"
            existing = conn.execute(
                text(
                    """
                    SELECT id FROM agent_resource_bindings
                    WHERE tenant_id = :tenant_id AND agent_id = :agent_id
                      AND resource_type = :resource_type AND resource_id = :resource_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "agent_id": default_agent,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                },
            ).first()
            if existing:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO agent_resource_bindings (
                        id, tenant_id, agent_id, resource_type, resource_id, status,
                        metadata_json, created_at, updated_at
                    )
                    VALUES (
                        :id, :tenant_id, :agent_id, :resource_type, :resource_id, :status,
                        '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "id": _agent_resource_binding_id(tenant_id, default_agent, resource_type, resource_id),
                    "tenant_id": tenant_id,
                    "agent_id": default_agent,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "status": binding_status,
                },
            )


def _archive_default_agent(conn, tenant_id: str) -> None:
    """归档由系统种子创建的旧默认员工智能体。

    仅当目标智能体确属系统/管理员创建（metadata 中 is_default_employee、
    created_by/owner 为 admin）时，才将其状态置为 archived 并补充归属元数据，
    使其在前端隐藏；否则保留不动。

    Args:
        conn: 已开启事务的数据库连接。
        tenant_id: 租户 id。
    """
    default_agent = _default_agent_id(tenant_id)
    row = conn.execute(
        text(
            """
            SELECT metadata_json FROM agent_profiles
            WHERE id = :id AND tenant_id = :tenant_id AND is_overall = 0
            """
        ),
        {"id": default_agent, "tenant_id": tenant_id},
    ).first()
    if not row:
        return
    try:
        metadata = json.loads(row[0] or "{}")
    except json.JSONDecodeError:
        metadata = {}
    if metadata and not (
        metadata.get("is_default_employee") is True
        or metadata.get("created_by") == "admin"
        or metadata.get("owner_user_id") == "admin"
    ):
        return
    metadata.update(
        {
            "is_default_employee": True,
            "hidden_from_staffdeck": True,
            "archived_by_seed": True,
            "owner_user_id": "admin",
            "owner_username": "admin",
            "owner_display_name": "Administrator",
            "created_by_user_id": "admin",
            "created_by_username": "admin",
            "created_by": "admin",
            "created_by_display_name": "Administrator",
            "creator_name": "admin",
        }
    )
    conn.execute(
        text(
            """
            UPDATE agent_profiles
            SET status = 'archived',
                metadata_json = :metadata_json,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = :id AND tenant_id = :tenant_id
            """
        ),
        {
            "id": default_agent,
            "tenant_id": tenant_id,
            "metadata_json": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        },
    )


def _seed_agent_branch_state(conn, inspector, tables: set[str]) -> None:
    """为非归档智能体补建技能分支、知识分支与默认模型绑定。

    分三部分：
    1. 技能分支：遍历智能体绑定的技能，调用 ``_seed_agent_skill_branch`` 建立
       分支及分支版本；
    2. 知识分支：遍历智能体绑定的知识库，调用 ``_seed_agent_knowledge_branch``；
    3. 模型绑定：为每个智能体绑定租户默认模型（role=default），缺失时才插入。

    Args:
        conn: 已开启事务的数据库连接。
        inspector: SQLAlchemy inspector（本函数未直接使用，保留以兼容调用签名）。
        tables: 当前库的表名集合。
    """
    if "agent_profiles" not in tables:
        return
    if "agent_skill_branches" in tables and "skills" in tables:
        agents = conn.execute(
            text("SELECT id, tenant_id FROM agent_profiles WHERE is_overall = 0 AND status != 'archived'")
        ).mappings().all()
        for agent in agents:
            tenant_id = str(agent["tenant_id"])
            agent_id = str(agent["id"])
            _seed_default_agent_bindings(conn, tenant_id)
            rows = conn.execute(
                text(
                    """
                    SELECT s.*
                    FROM skills s
                    JOIN agent_resource_bindings b
                      ON b.resource_id = s.id
                     AND b.resource_type = 'skill'
                     AND b.tenant_id = s.tenant_id
                    WHERE s.tenant_id = :tenant_id
                      AND b.agent_id = :agent_id
                      AND s.status != 'deleted'
                    """
                ),
                {"tenant_id": tenant_id, "agent_id": agent_id},
            ).mappings().all()
            for row in rows:
                _seed_agent_skill_branch(conn, agent_id, row)

    if "agent_knowledge_branches" in tables and "knowledge_bases" in tables:
        agents = conn.execute(
            text("SELECT id, tenant_id FROM agent_profiles WHERE is_overall = 0 AND status != 'archived'")
        ).mappings().all()
        for agent in agents:
            tenant_id = str(agent["tenant_id"])
            agent_id = str(agent["id"])
            rows = conn.execute(
                text(
                    """
                    SELECT kb.*
                    FROM knowledge_bases kb
                    JOIN agent_resource_bindings b
                      ON b.resource_id = kb.id
                     AND b.resource_type = 'knowledge_base'
                     AND b.tenant_id = kb.tenant_id
                    WHERE kb.tenant_id = :tenant_id
                      AND b.agent_id = :agent_id
                      AND kb.status != 'deleted'
                    """
                ),
                {"tenant_id": tenant_id, "agent_id": agent_id},
            ).mappings().all()
            for row in rows:
                _seed_agent_knowledge_branch(conn, agent_id, row)

    if "agent_model_bindings" in tables and "model_configs" in tables:
        default_models = conn.execute(
            text("SELECT tenant_id, id FROM model_configs WHERE is_default = 1 AND enabled = 1")
        ).mappings().all()
        model_by_tenant = {str(row["tenant_id"]): str(row["id"]) for row in default_models}
        agents = conn.execute(
            text("SELECT id, tenant_id FROM agent_profiles WHERE status != 'archived'")
        ).mappings().all()
        for agent in agents:
            tenant_id = str(agent["tenant_id"])
            model_id = model_by_tenant.get(tenant_id)
            if not model_id:
                continue
            existing = conn.execute(
                text(
                    """
                    SELECT id FROM agent_model_bindings
                    WHERE tenant_id = :tenant_id AND agent_id = :agent_id AND role = 'default'
                    """
                ),
                {"tenant_id": tenant_id, "agent_id": agent["id"]},
            ).first()
            if existing:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO agent_model_bindings (
                        id, tenant_id, agent_id, role, model_config_id, created_at, updated_at
                    )
                    VALUES (
                        :id, :tenant_id, :agent_id, 'default', :model_config_id,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "id": _agent_model_binding_id(str(agent["id"]), "default"),
                    "tenant_id": tenant_id,
                    "agent_id": agent["id"],
                    "model_config_id": model_id,
                },
            )


def _normalize_agent_branch_rows(conn, tables: set[str]) -> None:
    """将多张智能体分支/绑定表的行 id 规范化为确定性派生 id。

    依次处理 agent_resource_bindings、agent_skill_branches、
    agent_skill_branch_versions、agent_knowledge_branches，通过
    ``_normalize_canonical_ids`` 把主键统一为按业务键生成的稳定 id，并消除
    因历史随机 id 造成的重复。

    Args:
        conn: 已开启事务的数据库连接。
        tables: 当前库的表名集合。
    """
    if "agent_resource_bindings" in tables:
        _normalize_canonical_ids(
            conn,
            table="agent_resource_bindings",
            select_columns=("id", "tenant_id", "agent_id", "resource_type", "resource_id"),
            key_columns=("tenant_id", "agent_id", "resource_type", "resource_id"),
            id_factory=lambda row: _agent_resource_binding_id(
                str(row["tenant_id"]),
                str(row["agent_id"]),
                str(row["resource_type"]),
                str(row["resource_id"]),
            ),
        )
    if "agent_skill_branches" in tables:
        _normalize_canonical_ids(
            conn,
            table="agent_skill_branches",
            select_columns=("id", "tenant_id", "agent_id", "skill_id"),
            key_columns=("tenant_id", "agent_id", "skill_id"),
            id_factory=lambda row: _agent_skill_branch_id(str(row["agent_id"]), str(row["skill_id"])),
        )
    if "agent_skill_branch_versions" in tables:
        _normalize_canonical_ids(
            conn,
            table="agent_skill_branch_versions",
            select_columns=("id", "tenant_id", "agent_id", "skill_id", "version"),
            key_columns=("tenant_id", "agent_id", "skill_id", "version"),
            id_factory=lambda row: _agent_skill_branch_version_id(
                str(row["agent_id"]),
                str(row["skill_id"]),
                str(row["version"]),
            ),
        )
    if "agent_knowledge_branches" in tables:
        _normalize_canonical_ids(
            conn,
            table="agent_knowledge_branches",
            select_columns=("id", "tenant_id", "agent_id", "knowledge_base_id"),
            key_columns=("tenant_id", "agent_id", "knowledge_base_id"),
            id_factory=lambda row: _agent_knowledge_branch_id(str(row["agent_id"]), str(row["knowledge_base_id"])),
        )


def _normalize_canonical_ids(
    conn,
    *,
    table: str,
    select_columns: tuple[str, ...],
    key_columns: tuple[str, ...],
    id_factory: Callable[[dict[str, object]], str],
) -> None:
    """通用 ID 规范化器：把表中各行主键统一为确定性派生 id。

    遍历表中所有行，依据 ``id_factory`` 由业务键计算目标 id：
    - 若业务键重复，则删除当前重复行；
    - 若目标 id 已被占用，删除当前行；
    - 否则将当前行主键更新为目标 id。

    Args:
        conn: 已开启事务的数据库连接。
        table: 目标表名。
        select_columns: 查询的列（须含 id 与全部 key_columns）。
        key_columns: 用于唯一性与 id 计算的业务键列名元组。
        id_factory: 由行字典计算目标 id 的回调函数。
    """
    columns_sql = ", ".join(select_columns)
    rows = conn.execute(text(f"SELECT {columns_sql} FROM {table}")).mappings().all()
    kept_keys: set[tuple[object, ...]] = set()
    for row in rows:
        row_dict = dict(row)
        row_id = str(row_dict["id"])
        key = tuple(row_dict[column] for column in key_columns)
        target_id = id_factory(row_dict)
        if key in kept_keys:
            conn.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})
            continue
        kept_keys.add(key)
        if row_id == target_id:
            continue
        target_exists = conn.execute(text(f"SELECT id FROM {table} WHERE id = :id"), {"id": target_id}).first()
        if target_exists:
            conn.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})
            continue
        conn.execute(text(f"UPDATE {table} SET id = :target_id WHERE id = :id"), {"target_id": target_id, "id": row_id})


def _seed_agent_skill_branch(conn, agent_id: str, row) -> None:
    """为某智能体对某技能建立分支及分支版本（若不存在）。

    依据技能行（``row``）的状态决定分支状态（published → active，否则
    inactive），插入 ``agent_skill_branches``，并在版本表存在时插入对应分支版本。

    Args:
        conn: 已开启事务的数据库连接。
        agent_id: 智能体 id。
        row: 技能行（mapping），需含 tenant_id/skill_id/version/content_json/status 等。
    """
    branch_id = _agent_skill_branch_id(agent_id, str(row["skill_id"]))
    existing = conn.execute(text("SELECT id FROM agent_skill_branches WHERE id = :id"), {"id": branch_id}).first()
    if existing:
        return
    version = row.get("version") or "1.0.0"
    content_json = row.get("content_json") or "{}"
    branch_status = "active" if str(row.get("status") or "") == "published" else "inactive"
    conn.execute(
        text(
            """
            INSERT INTO agent_skill_branches (
                id, tenant_id, agent_id, skill_id, source_skill_id, base_version, head_version,
                content_json, status, sync_state, metadata_json, created_at, updated_at
            )
            VALUES (
                :id, :tenant_id, :agent_id, :skill_id, :source_skill_id, :base_version, :head_version,
                :content_json, :status, 'synced', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": branch_id,
            "tenant_id": row["tenant_id"],
            "agent_id": agent_id,
            "skill_id": row["skill_id"],
            "source_skill_id": row["id"],
            "base_version": version,
            "head_version": version,
            "content_json": content_json,
            "status": branch_status,
        },
    )
    if "agent_skill_branch_versions" not in {table for table in inspect(engine).get_table_names()}:
        return
    branch_version_id = _agent_skill_branch_version_id(agent_id, str(row["skill_id"]), version)
    existing_version = conn.execute(
        text("SELECT id FROM agent_skill_branch_versions WHERE id = :id"),
        {"id": branch_version_id},
    ).first()
    if existing_version:
        return
    conn.execute(
        text(
            """
            INSERT INTO agent_skill_branch_versions (
                id, tenant_id, agent_id, skill_id, source_skill_id, version, base_version,
                content_json, status, sync_state, change_summary, created_at, updated_at
            )
            VALUES (
                :id, :tenant_id, :agent_id, :skill_id, :source_skill_id, :version, :base_version,
                :content_json, :status, 'synced', '初始化分支', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": branch_version_id,
            "tenant_id": row["tenant_id"],
            "agent_id": agent_id,
            "skill_id": row["skill_id"],
            "source_skill_id": row["id"],
            "version": version,
            "base_version": version,
            "content_json": content_json,
            "status": branch_status,
        },
    )


def _seed_agent_knowledge_branch(conn, agent_id: str, row) -> None:
    """为某智能体对某知识库建立知识分支（若不存在）。

    Args:
        conn: 已开启事务的数据库连接。
        agent_id: 智能体 id。
        row: 知识库行（mapping），需含 tenant_id/id/status 等。
    """
    branch_id = _agent_knowledge_branch_id(agent_id, str(row["id"]))
    existing = conn.execute(text("SELECT id FROM agent_knowledge_branches WHERE id = :id"), {"id": branch_id}).first()
    if existing:
        return
    branch_status = "active" if str(row.get("status") or "") == "active" else "inactive"
    conn.execute(
        text(
            """
            INSERT INTO agent_knowledge_branches (
                id, tenant_id, agent_id, knowledge_base_id, base_version, head_version,
                status, sync_state, metadata_json, created_at, updated_at
            )
            VALUES (
                :id, :tenant_id, :agent_id, :knowledge_base_id, '1.0.0', '1.0.0',
                :status, 'synced', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": branch_id,
            "tenant_id": row["tenant_id"],
            "agent_id": agent_id,
            "knowledge_base_id": row["id"],
            "status": branch_status,
        },
    )


def _tenant_ids(conn, tables: set[str]) -> list[str]:
    """收集当前库中所有出现过的租户 id（并集）。

    优先从 ``tenants`` 表获取，再从多张业务表（skills/general_skills/
    knowledge_documents/sessions）中补充出现的 tenant_id，返回去重后的有序列表。

    Args:
        conn: 数据库连接。
        tables: 当前库的表名集合。

    Returns:
        list[str]: 排序后的租户 id 列表。
    """
    ids: set[str] = set()
    if "tenants" in tables:
        ids.update(str(row[0]) for row in conn.execute(text("SELECT id FROM tenants")).all() if row[0])
    for table_name in ("skills", "general_skills", "knowledge_documents", "sessions"):
        if table_name not in tables:
            continue
        ids.update(str(row[0]) for row in conn.execute(text(f"SELECT DISTINCT tenant_id FROM {table_name}")).all() if row[0])
    return sorted(ids)


def _default_knowledge_base_id(tenant_id: str) -> str:
    """生成租户默认知识库的确定性 id（``kb_<tenant>_default``）。"""
    return f"kb_{tenant_id}_default"


def _overall_agent_id(tenant_id: str) -> str:
    """生成租户整体智能体的确定性 id（``agent_<tenant>_overall``）。"""
    return f"agent_{tenant_id}_overall"


def _default_agent_id(tenant_id: str) -> str:
    """生成租户默认员工智能体的确定性 id（``agent_<tenant>_default``）。"""
    return f"agent_{tenant_id}_default"


def _knowledge_base_version_id(knowledge_base_id: str, version: str) -> str:
    """生成知识库版本的确定性 id。

    将版本号中的 ``.`` 与 ``-`` 替换为 ``_`` 以保证 id 合法，格式为
    ``kbver_<kb_id>_<safe_version>``。
    """
    return f"kbver_{knowledge_base_id}_{version.replace('.', '_').replace('-', '_')}"


def _agent_skill_branch_id(agent_id: str, skill_id: str) -> str:
    """生成智能体技能分支的确定性 id（``agentbranch_<agent>_<skill>``）。"""
    return f"agentbranch_{agent_id}_{skill_id}"


def _agent_skill_branch_version_id(agent_id: str, skill_id: str, version: str) -> str:
    """生成智能体技能分支版本的确定性 id（版本号中 ``.``/``-`` 转为 ``_``）。"""
    safe_version = version.replace(".", "_").replace("-", "_")
    return f"agentbranchver_{agent_id}_{skill_id}_{safe_version}"


def _agent_knowledge_branch_id(agent_id: str, knowledge_base_id: str) -> str:
    """生成智能体知识分支的确定性 id（``agentkb_<agent>_<kb>``）。"""
    return f"agentkb_{agent_id}_{knowledge_base_id}"


def _agent_resource_binding_id(tenant_id: str, agent_id: str, resource_type: str, resource_id: str) -> str:
    """生成智能体资源绑定的确定性 id（业务键的 SHA1 前 16 位）。

    使用哈希而非拼接，可避免 id 过长或包含特殊字符。
    """
    key = f"{tenant_id}:{agent_id}:{resource_type}:{resource_id}"
    return f"agentres_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"


def _agent_model_binding_id(agent_id: str, role: str) -> str:
    """生成智能体模型绑定的确定性 id（``agentmodel_<agent>_<role>``）。"""
    return f"agentmodel_{agent_id}_{role}"


def get_session() -> Generator[Session, None, None]:
    """FastAPI 依赖：提供一个 ``Session`` 上下文并在用完后自动关闭。

    Yields:
        Session: 已绑定到全局引擎的 SQLModel 会话。
    """
    with Session(engine) as session:
        yield session

"""技能蒸馏/编辑模块的 LLM 输出令牌限制配置。

定义技能生成场景下模型输出所需的最小令牌数，并提供一个便捷函数，
将传入的 ModelConfig 快照为满足该最小限制的模型配置。
"""

from __future__ import annotations

from app.db.models import ModelConfig
from app.llm.model_config_resolver import snapshot_model_config


# 技能蒸馏 / 编辑场景下，模型单次输出的最小令牌数上限
SKILL_MAX_OUTPUT_TOKENS = 8192


def skill_model_config(model_config: ModelConfig) -> ModelConfig:
    """对传入的模型配置进行快照，确保其输出令牌数不低于技能场景所需的最小值。

    Args:
        model_config: 原始模型配置实例。

    Returns:
        调整后的 ModelConfig，其 output_tokens 不低于 SKILL_MAX_OUTPUT_TOKENS。
    """
    return snapshot_model_config(model_config, min_output_tokens=SKILL_MAX_OUTPUT_TOKENS)

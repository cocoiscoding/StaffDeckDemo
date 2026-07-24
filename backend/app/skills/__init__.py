"""技能（Skill）包。

提供技能的蒸馏（distill）与编辑（edit）能力，支持从对话历史中提炼技能定义，
以及对已有技能进行结构化修改与版本管理。

导出对象:
    SkillDistiller: 技能蒸馏器，负责从交互记录中提炼、生成技能内容。
    SkillEditor: 技能编辑器，负责技能内容的修改与版本迭代。
"""

from app.skills.skill_distiller import SkillDistiller
from app.skills.skill_editor import SkillEditor

__all__ = ["SkillDistiller", "SkillEditor"]

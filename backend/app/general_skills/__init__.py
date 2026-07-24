"""通用技能（General Skill）包。

封装通用技能的运行时执行、技能选择（selector）以及导入/打包/运行的
数据模型（schema）。通用技能是独立于特定代理的可复用技能单元，
支持从外部仓库导入并以 Markdown / 文件包的形式管理。

导出对象:
    GeneralSkillClawHubImportRequest: 从 ClawHub 导入通用技能的请求模型。
    GeneralSkillImportRequest: 通用技能导入请求模型。
    GeneralSkillPackageUploadRequest: 通用技能包上传请求模型。
    GeneralSkillRead: 通用技能的只读响应模型。
    GeneralSkillRunRequest: 通用技能运行请求模型。
    GeneralSkillRunResponse: 通用技能运行响应模型。
    GeneralSkillRunner: 通用技能执行器，负责实际运行技能逻辑。
    GeneralSkillSelector: 通用技能选择器，负责根据上下文匹配最佳技能。
"""

from app.general_skills.runner import GeneralSkillRunner, GeneralSkillSelector
from app.general_skills.schema import (
    GeneralSkillClawHubImportRequest,
    GeneralSkillImportRequest,
    GeneralSkillPackageUploadRequest,
    GeneralSkillRead,
    GeneralSkillRunRequest,
    GeneralSkillRunResponse,
)

__all__ = [
    "GeneralSkillClawHubImportRequest",
    "GeneralSkillImportRequest",
    "GeneralSkillPackageUploadRequest",
    "GeneralSkillRead",
    "GeneralSkillRunRequest",
    "GeneralSkillRunResponse",
    "GeneralSkillRunner",
    "GeneralSkillSelector",
]

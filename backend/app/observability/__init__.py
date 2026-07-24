"""可观测性（Observability）包。

提供系统运行时事件的记录、追踪与审计能力，用于诊断问题、监控系统健康度
以及回溯代理决策链路。

导出对象:
    EventLog: 事件日志记录器，负责持久化与查询系统运行时事件。
"""

from app.observability.event_log import EventLog

__all__ = ["EventLog"]

"""定时任务调度 Worker 模块。

提供两种运行模式：

1. **独立进程模式**：通过 ``main()`` 入口以命令行方式启动，
   循环扫描到期任务并执行，适合在生产环境中单独部署。
2. **后台线程模式**：通过 ``start_background_worker`` 在应用进程内
   启动一个守护线程运行 Worker，适合开发环境或单进程部署。

Worker 使用乐观锁（``lease_until``）机制实现多实例安全竞争。
"""

from __future__ import annotations

import argparse
import signal
import threading
from time import sleep

from sqlmodel import Session

from app.db import engine, init_db
from app.db.seed import seed_demo_data
from app.scheduled_tasks.service import WORKER_SLEEP_SECONDS, due_scheduled_tasks, execute_scheduled_task


# 全局停止标志，由信号处理函数设置
_stopped = False
# 后台线程引用，用于避免重复启动
_background_thread: threading.Thread | None = None


def _handle_stop(_signum: int, _frame: object) -> None:
    """信号处理函数：收到 SIGTERM/SIGINT 时设置停止标志。

    Args:
        _signum: 信号编号（未使用）。
        _frame: 当前栈帧（未使用）。
    """
    global _stopped
    _stopped = True


def run_worker(*, once: bool = False, poll_seconds: float = WORKER_SLEEP_SECONDS) -> None:
    """运行 Worker 主循环。

    初始化数据库后循环执行：扫描到期任务 → 逐个执行 → 休眠。
    当 ``once=True`` 时仅扫描一轮后退出。

    Args:
        once: 是否仅执行一轮后退出。
        poll_seconds: 扫描间隔秒数。
    """
    init_db()
    with Session(engine) as db:
        seed_demo_data(db)
    while not _stopped:
        with Session(engine) as db:
            due = due_scheduled_tasks(db)
            for task in due:
                execute_scheduled_task(db, task)
        if once:
            return
        sleep(max(1.0, poll_seconds))


def start_background_worker(*, poll_seconds: float = WORKER_SLEEP_SECONDS) -> None:
    """在应用进程内启动后台守护线程运行 Worker。

    如果已有线程在运行则直接返回，不重复创建。

    Args:
        poll_seconds: 扫描间隔秒数。
    """
    global _background_thread, _stopped
    if _background_thread and _background_thread.is_alive():
        return
    _stopped = False
    _background_thread = threading.Thread(
        target=run_worker,
        kwargs={"once": False, "poll_seconds": poll_seconds},
        name="ultrarag-scheduled-task-worker",
        daemon=True,
    )
    _background_thread.start()


def stop_background_worker() -> None:
    """请求停止后台 Worker 线程。

    设置全局停止标志，Worker 在下一次循环检查时退出。
    """
    global _stopped
    _stopped = True


def main() -> None:
    """命令行入口：解析参数并运行 Worker。

    支持 ``--once``（仅执行一轮）和 ``--poll-seconds``（扫描间隔）参数，
    注册 SIGTERM/SIGINT 信号处理以实现优雅退出。
    """
    parser = argparse.ArgumentParser(description="Run StaffDeck scheduled task worker")
    parser.add_argument("--once", action="store_true", help="scan and execute due tasks once, then exit")
    parser.add_argument("--poll-seconds", type=float, default=WORKER_SLEEP_SECONDS)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    run_worker(once=args.once, poll_seconds=args.poll_seconds)


if __name__ == "__main__":
    main()

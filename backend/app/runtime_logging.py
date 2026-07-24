"""运行时日志配置模块。

为 StaffDeck 桌面应用配置隐私作用域的日志系统：

- 使用非阻塞队列（``QueueHandler``）确保日志写入不影响请求线程。
- 日志按大小滚动（默认 5MB × 5 份），防止磁盘占用无限增长。
- 队列满时自动丢弃新日志记录（``_DroppingQueueHandler``），避免线程阻塞。
- 安装进程级和线程级异常钩子，捕获未处理异常写入崩溃日志。
- 在 PyInstaller 窗口模式下提供空标准流替代（``_NullTextStream``）。

日志名称空间：``staffdeck.runtime``（运行时）、``staffdeck.static``（静态文件）、
``staffdeck.crash``（崩溃）。
"""

from __future__ import annotations

import atexit
import logging
import os
import platform
import queue
import sys
import threading
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path

from app import paths


LOG_FILE_NAME = "staffdeck.log"
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5
LOG_QUEUE_CAPACITY = 10_000
RUNTIME_LOGGER_NAMES = (
    "staffdeck.runtime",
    "staffdeck.static",
    "staffdeck.crash",
)

_configured_path: Path | None = None
_queue_handler: _DroppingQueueHandler | None = None
_file_handler: _RuntimeFileHandler | None = None
_listener: _RuntimeQueueListener | None = None
_atexit_registered = False


class _RuntimeFileHandler(RotatingFileHandler):
    """Marker subclass used to replace an existing StaffDeck file handler."""


class _DroppingQueueHandler(QueueHandler):
    """Never block application threads when the logging queue is saturated."""

    def __init__(self, record_queue: queue.Queue[logging.LogRecord]) -> None:
        super().__init__(record_queue)
        self.dropped_records = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped_records += 1


class _RuntimeQueueListener(QueueListener):
    def enqueue_sentinel(self) -> None:
        # Shutdown may wait briefly for the writer, but normal request threads never do.
        self.queue.put(self._sentinel)


class _NullTextStream:
    """Writable sink for PyInstaller windowed processes without stdio handles."""

    encoding = "utf-8"

    def write(self, message: str) -> int:
        return len(message)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


def runtime_log_path() -> Path:
    """返回运行时日志文件的完整路径。"""
    return paths.user_data_dir() / "logs" / LOG_FILE_NAME


def configure_runtime_logging() -> Path:
    """配置隐私作用域的桌面日志系统并返回活跃日志路径。

    配置流程：
    1. 创建日志目录。
    2. 如果路径变化（如首次调用或用户数据目录变更），先关闭旧配置再重新初始化。
    3. 创建 ``RotatingFileHandler``（5MB 滚动，保留 5 份）。
    4. 创建非阻塞 ``QueueHandler`` + ``QueueListener`` 管道。
    5. 为 ``RUNTIME_LOGGER_NAMES`` 中的 logger 注册 handler。
    6. 安装进程级和线程级异常钩子。
    7. 在 PyInstaller 窗口模式下替换空的 stdio。

    Returns:
        已解析的日志文件绝对路径。
    """
    global _atexit_registered, _configured_path, _file_handler, _listener, _queue_handler

    log_path = runtime_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path = log_path.resolve()

    if _configured_path != resolved_path:
        shutdown_runtime_logging()
        file_handler = _RuntimeFileHandler(
            resolved_path,
            maxBytes=MAX_LOG_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(process)d] %(name)s: %(message)s"
            )
        )

        record_queue: queue.Queue[logging.LogRecord] = queue.Queue(
            maxsize=LOG_QUEUE_CAPACITY
        )
        queue_handler = _DroppingQueueHandler(record_queue)
        listener = _RuntimeQueueListener(
            record_queue,
            file_handler,
            respect_handler_level=True,
        )
        for logger_name in RUNTIME_LOGGER_NAMES:
            runtime_logger = logging.getLogger(logger_name)
            for existing in list(runtime_logger.handlers):
                if isinstance(existing, _DroppingQueueHandler):
                    runtime_logger.removeHandler(existing)
            runtime_logger.addHandler(queue_handler)
            runtime_logger.setLevel(logging.INFO)
            runtime_logger.propagate = False

        listener.start()
        _configured_path = resolved_path
        _file_handler = file_handler
        _listener = listener
        _queue_handler = queue_handler
        if not _atexit_registered:
            atexit.register(shutdown_runtime_logging)
            _atexit_registered = True

    if sys.stdout is None:
        sys.stdout = _NullTextStream()
    if sys.stderr is None:
        sys.stderr = _NullTextStream()

    _install_exception_hooks()
    logging.getLogger("staffdeck.runtime").info(
        "Runtime started platform=%s release=%s python=%s frozen=%s pid=%s",
        platform.system(),
        platform.release(),
        platform.python_version(),
        bool(getattr(sys, "frozen", False)),
        os.getpid(),
    )
    return resolved_path


def shutdown_runtime_logging() -> None:
    """Flush queued records and close StaffDeck-owned logging resources."""
    global _configured_path, _file_handler, _listener, _queue_handler

    listener = _listener
    queue_handler = _queue_handler
    file_handler = _file_handler
    _listener = None
    _queue_handler = None
    _file_handler = None
    _configured_path = None

    if listener is not None:
        listener.stop()
    if queue_handler is not None:
        for logger_name in RUNTIME_LOGGER_NAMES:
            runtime_logger = logging.getLogger(logger_name)
            runtime_logger.removeHandler(queue_handler)
        queue_handler.close()
    if file_handler is not None:
        file_handler.close()


def _install_exception_hooks() -> None:
    def log_process_exception(exc_type, exc_value, traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, traceback)
            return
        logging.getLogger("staffdeck.crash").critical(
            "Unhandled process exception type=%s\n%s",
            exc_type.__name__,
            _format_stack(traceback),
        )

    def log_thread_exception(args: threading.ExceptHookArgs) -> None:
        logging.getLogger("staffdeck.crash").critical(
            "Unhandled thread exception type=%s\n%s",
            args.exc_type.__name__,
            _format_stack(args.exc_traceback),
        )

    sys.excepthook = log_process_exception
    threading.excepthook = log_thread_exception


def _format_stack(traceback) -> str:
    if traceback is None:
        return "<no traceback>"
    frames: list[str] = []
    while traceback is not None:
        code = traceback.tb_frame.f_code
        frames.append(
            f"  {Path(code.co_filename).name}:{traceback.tb_lineno} in {code.co_name}"
        )
        traceback = traceback.tb_next
    return "\n".join(frames)

"""通用技能运行器（General Skill Runner）模块。

本模块负责通用技能的完整执行生命周期，包括：
1. **技能路由选择**：根据用户查询从已发布的通用技能中选择最匹配的技能。
2. **执行计划生成**：调用 LLM 根据 SKILL.md 生成可执行代码（Python / Bash）。
3. **沙箱执行**：在临时目录中恢复技能文件包，以子进程方式执行生成的代码。
4. **反思与修复**：审查执行结果，若不满足预期则自动修复代码并重试（最多 N 次）。
5. **最终回复生成**：根据执行结果和结构化输出生成面向用户的自然语言回复。

核心类：
- ``GeneralSkillSelector``：技能路由选择器。
- ``GeneralSkillRunner``：技能执行器，包含生成、执行、审查、修复和回复的完整流程。
"""
from __future__ import annotations

import json
import os
import queue
import selectors
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from app import paths
from app.db.models import GeneralSkill, ModelConfig
from app.general_skills.schema import (
    GeneralSkillExecutionPlan,
    GeneralSkillExecutionReview,
    GeneralSkillReply,
    GeneralSkillRunResponse,
    GeneralSkillSelection,
)
from app.general_skills.runtime_env import GeneralSkillRuntimeError, ensure_runtime_python, runtime_environment
from app.llm import LLMClient, LLMError
from app.llm.model_config_resolver import snapshot_model_config
from app.llm.stage_protocol import stage_payload, unified_system_prompt
from app.observability.spans import llm_operation


PROMPT_DIR = paths.resource_dir() / "app" / "llm" / "prompts"
SELECTOR_PROMPT = PROMPT_DIR / "general_skill_selector_prompt.md"
RUNNER_PROMPT = PROMPT_DIR / "general_skill_runner_prompt.md"
REPAIR_PROMPT = PROMPT_DIR / "general_skill_repair_prompt.md"
REVIEW_PROMPT = PROMPT_DIR / "general_skill_review_prompt.md"
REPLY_PROMPT = PROMPT_DIR / "general_skill_reply_prompt.md"
# 子进程执行超时时间（秒）
RUN_TIMEOUT_SECONDS = 12
# stdout / stderr 最大保留字符数
MAX_OUTPUT_CHARS = 20000
# 通用技能场景下模型输出的最大令牌数
GENERAL_SKILL_MAX_TOKENS = 8192
# 最大执行/修复尝试次数
GENERAL_SKILL_MAX_ATTEMPTS = 10
# 执行轨迹事件回调函数类型
TraceSink = Callable[[dict[str, Any]], None]
GENERAL_SKILL_SELECTION_OUTPUT = {
    "use_general_skill": "boolean",
    "selected_slug": "string?",
    "use_knowledge": "boolean",
    "knowledge_query": "string?",
    "confidence": "number",
    "reason": "string?",
}
GENERAL_SKILL_PLAN_OUTPUT = {
    "code": "string",
    "runtime": "bash | python",
    "rationale": "string?",
    "expected_output": "string?",
}
GENERAL_SKILL_REVIEW_OUTPUT = {
    "result_sufficient": "boolean",
    "needs_retry": "boolean",
    "terminal": "boolean",
    "reason": "string",
    "repair_hint": "string?",
}
GENERAL_SKILL_REPLY_OUTPUT = {"reply": "string"}


class GeneralSkillSelector:
    """通用技能路由选择器——根据用户查询选择最匹配的通用技能。

    通过 LLM 分析用户查询和已发布的通用技能列表，决定是否使用通用技能
    以及选择哪个技能。若选择的 slug 不在已发布列表中，则回退为不使用。
    """
    def decide(
        self,
        query: str,
        general_skills: list[GeneralSkill],
        model_config: ModelConfig,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> GeneralSkillSelection:
        """根据用户查询选择最匹配的通用技能。

        Args:
            query: 用户查询文本。
            general_skills: 已加载的通用技能列表。
            model_config: LLM 模型配置。
            conversation_context: 对话上下文（可选）。
            memory_context: 记忆上下文（可选）。

        Returns:
            技能选择结果。若选中的 slug 无效则 use_general_skill 设为 False。
        """
        payload = stage_payload(
            phase="Router / General Skill Selector",
            user_message=query,
            conversation_context=conversation_context,
            memory_context=memory_context,
            instructions=SELECTOR_PROMPT.read_text(encoding="utf-8"),
            stage_data={
                "general_skills": [
                    {
                        "slug": skill.slug,
                        "name": skill.name,
                        "description": skill.description,
                        "homepage": skill.homepage,
                        "status": skill.status,
                    }
                    for skill in general_skills
                    if skill.status == "published"
                ],
            },
            output_contract=GENERAL_SKILL_SELECTION_OUTPUT,
        )
        with llm_operation("general_skill.select"):
            raw = LLMClient(model_config).generate_json(
                unified_system_prompt(), payload
            )
        decision = GeneralSkillSelection.model_validate(raw)
        slugs = {skill.slug for skill in general_skills if skill.status == "published"}
        if decision.use_general_skill and decision.selected_slug in slugs:
            return decision
        return decision.model_copy(update={"use_general_skill": False, "selected_slug": None})


class GeneralSkillRunner:
    """通用技能执行器——管理技能的完整执行生命周期。

    核心流程：计划生成 -> 执行 -> 审查 -> 修复（循环）-> 最终回复。
    支持多轮自动修复，在执行结果不满足预期时根据审查反馈重新生成代码。
    """
    def run(
        self,
        skill: GeneralSkill,
        query: str,
        model_config: ModelConfig,
        user_id: str = "",
        max_attempts: int = GENERAL_SKILL_MAX_ATTEMPTS,
        event_sink: TraceSink | None = None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> GeneralSkillRunResponse:
        """执行通用技能的完整流程。

        流程：
        1. 生成执行计划（带反思重试）。
        2. 循环执行：执行代码 -> 审查结果 -> 若需修复则重新生成。
        3. 根据最终结果生成面向用户的回复。

        Args:
            skill: 要执行的通用技能。
            query: 用户查询文本。
            model_config: LLM 模型配置。
            user_id: 用户 ID。
            max_attempts: 最大执行尝试次数。
            event_sink: 执行轨迹事件回调。
            conversation_context: 对话上下文。
            memory_context: 记忆上下文。

        Returns:
            包含执行轨迹、代码、输出、结构化结果和回复的 GeneralSkillRunResponse。
        """
        trace: list[dict[str, Any]] = []
        max_attempts = max(1, min(max_attempts, GENERAL_SKILL_MAX_ATTEMPTS))
        _emit(trace, {"phase": "skill_loaded", "message": f"已加载通用技能 {skill.name}", "slug": skill.slug}, event_sink)
        try:
            plan, planning_attempts = self._generate_plan_with_reflection(
                skill,
                query,
                model_config,
                trace,
                event_sink,
                max_attempts,
                conversation_context,
                memory_context,
            )
        except LLMError as exc:
            _emit(trace, {"phase": "plan_failed", "message": "模型生成 runner 失败", "error": str(exc)}, event_sink)
            return GeneralSkillRunResponse(
                skill_slug=skill.slug,
                execution_trace=trace,
                generated_code="",
                stdout="",
                stderr=str(exc),
                structured_result={"success": False, "error": "runner_plan_failed", "message": str(exc)},
                reply="抱歉，当前通用技能执行代码生成失败，暂时无法完成这次运行。",
            )

        attempts: list[dict[str, Any]] = planning_attempts
        stdout = ""
        stderr = ""
        structured_result: dict[str, Any] = {}
        for attempt in range(1, max_attempts + 1):
            _emit(
                trace,
                {"phase": "attempt_started", "message": f"开始第 {attempt} 次运行", "attempt": attempt},
                event_sink,
            )
            stdout, stderr, structured_result = self._execute_plan(
                skill,
                query,
                plan,
                user_id,
                trace,
                event_sink,
                attempt,
            )
            _normalize_failure_diagnostics(structured_result)
            review = self._review_execution_result(
                skill,
                query,
                model_config,
                plan,
                stdout,
                stderr,
                structured_result,
                trace,
                event_sink,
                attempt,
                conversation_context,
                memory_context,
            )
            attempts.append(
                {
                    "attempt": attempt,
                    "code": _truncate(plan.code),
                    "stdout": _truncate(stdout),
                    "stderr": _truncate(stderr),
                    "structured_result": structured_result,
                    "execution_review": review,
                }
            )
            needs_retry = bool(review.get("needs_retry"))
            if not needs_retry:
                if structured_result.get("success") is False or review.get("result_sufficient") is False:
                    _emit(
                        trace,
                        {
                            "phase": "reflection_stopped",
                            "message": f"第 {attempt} 次运行结果不足，但模型判断不可继续自动修复",
                            "attempt": attempt,
                            "structured_result": structured_result,
                            "review": review,
                        },
                        event_sink,
                    )
                else:
                    _emit(
                        trace,
                        {"phase": "reflection_passed", "message": f"第 {attempt} 次运行结果可用", "attempt": attempt},
                        event_sink,
                    )
                break
            if attempt >= max_attempts:
                _emit(
                    trace,
                    {
                        "phase": "reflection_stopped",
                        "message": f"已达到最多 {max_attempts} 次尝试，停止自动修复",
                        "attempt": attempt,
                    },
                    event_sink,
                )
                break
            _emit(
                trace,
                {
                    "phase": "reflection_retrying",
                    "message": f"第 {attempt} 次运行未达预期，模型正在根据结果反思修复",
                    "attempt": attempt,
                    "stdout_preview": stdout[:600],
                    "stderr_preview": stderr[:600],
                    "structured_result": structured_result,
                    "review": review,
                },
                event_sink,
            )
            try:
                plan = self._repair_plan(
                    skill,
                    query,
                    model_config,
                    trace,
                    attempts,
                    event_sink,
                    attempt + 1,
                    conversation_context,
                    memory_context,
                )
            except LLMError as exc:
                _emit(
                    trace,
                    {"phase": "repair_failed", "message": "模型反思修复代码失败", "attempt": attempt, "error": str(exc)},
                    event_sink,
                )
                break

        try:
            reply = self._generate_reply(
                skill,
                query,
                model_config,
                trace,
                stdout,
                stderr,
                structured_result,
                event_sink,
                conversation_context,
                memory_context,
            )
        except LLMError as exc:
            _emit(trace, {"phase": "reply_failed", "message": "模型生成最终回复失败", "error": str(exc)}, event_sink)
            reply = _fallback_reply(structured_result)
        return GeneralSkillRunResponse(
            skill_slug=skill.slug,
            execution_trace=trace,
            generated_code=plan.code,
            stdout=stdout,
            stderr=stderr,
            structured_result=structured_result,
            reply=reply,
        )

    def _generate_plan(
        self,
        skill: GeneralSkill,
        query: str,
        model_config: ModelConfig,
        trace: list[dict[str, Any]],
        event_sink: TraceSink | None = None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> GeneralSkillExecutionPlan:
        """调用 LLM 根据 SKILL.md 生成可执行代码计划。

        Args:
            skill: 通用技能。
            query: 用户查询。
            model_config: 模型配置。
            trace: 执行轨迹列表（会被追加事件）。
            event_sink: 事件回调。
            conversation_context: 对话上下文。
            memory_context: 记忆上下文。

        Returns:
            包含代码和运行时的 GeneralSkillExecutionPlan。

        Raises:
            LLMError: 生成的代码为空时。
        """
        _emit(trace, {"phase": "planning", "message": "正在根据 SKILL.md 生成 runner"}, event_sink)
        stage_data = {
            "skill": {
                "slug": skill.slug,
                "name": skill.name,
                "description": skill.description,
                "homepage": skill.homepage,
                "markdown": skill.skill_markdown,
                "package": _skill_package_payload(skill),
            },
            "runtime": {
                "languages": ["bash", "python"],
                "stdin_json": {
                    "query": query,
                    "skill_slug": skill.slug,
                    "skill_name": skill.name,
                    "skill_workspace": "<runtime absolute path to the restored skill folder>",
                    "skill_files": [file["path"] for file in _skill_files(skill)],
                },
                "timeout_seconds": RUN_TIMEOUT_SECONDS,
            },
        }
        payload = stage_payload(
            phase="Step Agent / General Skill Plan",
            user_message=query,
            conversation_context=conversation_context,
            memory_context=memory_context,
            instructions=RUNNER_PROMPT.read_text(encoding="utf-8"),
            stage_data=stage_data,
            output_contract=GENERAL_SKILL_PLAN_OUTPUT,
        )
        with llm_operation("general_skill.plan"):
            raw = LLMClient(_with_min_tokens(model_config, GENERAL_SKILL_MAX_TOKENS)).generate_json(
                unified_system_prompt(),
                payload,
            )
        plan = GeneralSkillExecutionPlan.model_validate(raw)
        plan.runtime = _plan_runtime(plan)
        if not plan.code.strip():
            raise LLMError("General skill runner code is empty")
        runtime_label = _runtime_label(plan.runtime)
        _emit(
            trace,
            {
                "phase": "plan_created",
                "message": f"已生成 {runtime_label} runner",
                "runtime": plan.runtime,
                "rationale": plan.rationale,
                "code": plan.code,
                "expected_output": plan.expected_output,
            },
            event_sink,
        )
        return plan

    def _generate_plan_with_reflection(
        self,
        skill: GeneralSkill,
        query: str,
        model_config: ModelConfig,
        trace: list[dict[str, Any]],
        event_sink: TraceSink | None,
        max_attempts: int,
        conversation_context: dict[str, object] | None,
        memory_context: list[dict[str, object]] | None,
    ) -> tuple[GeneralSkillExecutionPlan, list[dict[str, Any]]]:
        """生成执行计划，失败时自动反思并重试。

        首次尝试直接生成；若失败则进入修复循环，每次将上一次的失败信息
        反馈给模型重新生成。达到最大尝试次数后抛出异常。

        Returns:
            二元组 ``(plan, failures)``：成功计划和失败记录列表。

        Raises:
            LLMError: 所有尝试均失败时。
        """
        planning_failures: list[dict[str, Any]] = []
        last_error: LLMError | None = None
        for plan_attempt in range(1, max_attempts + 1):
            try:
                if plan_attempt == 1:
                    return (
                        self._generate_plan(
                            skill,
                            query,
                            model_config,
                            trace,
                            event_sink,
                            conversation_context,
                            memory_context,
                        ),
                        planning_failures,
                    )
                return (
                    self._repair_plan(
                        skill,
                        query,
                        model_config,
                        trace,
                        planning_failures,
                        event_sink,
                        plan_attempt,
                        conversation_context,
                        memory_context,
                    ),
                    planning_failures,
                )
            except LLMError as exc:
                last_error = exc
                failure = {
                    "attempt": f"planning-{plan_attempt}",
                    "code": "",
                    "stdout": "",
                    "stderr": str(exc),
                    "structured_result": {
                        "success": False,
                        "error": "plan_generation_failed",
                        "message": str(exc),
                        "retryable": True,
                    },
                    "execution_review": {
                        "result_sufficient": False,
                        "needs_retry": plan_attempt < max_attempts,
                        "terminal": False,
                        "reason": "模型未能生成可执行 runner 计划，需要重新输出合法 JSON、runtime 和完整代码。",
                        "repair_hint": "保留原始 skill 与 query，重新输出包含 runtime、code、rationale、expected_output 的合法 JSON。",
                    },
                }
                planning_failures.append(failure)
                _emit(
                    trace,
                    {
                        "phase": "plan_failed",
                        "message": f"第 {plan_attempt} 次 runner 计划生成失败",
                        "attempt": plan_attempt,
                        "error": str(exc),
                    },
                    event_sink,
                )
                if plan_attempt >= max_attempts:
                    break
                _emit(
                    trace,
                    {
                        "phase": "reflection_retrying",
                        "message": f"第 {plan_attempt} 次计划生成失败，模型正在反思并重新输出代码",
                        "attempt": plan_attempt,
                        "structured_result": failure["structured_result"],
                        "review": failure["execution_review"],
                    },
                    event_sink,
                )
        raise LLMError(str(last_error) if last_error else "General skill runner plan generation failed")

    def _repair_plan(
        self,
        skill: GeneralSkill,
        query: str,
        model_config: ModelConfig,
        trace: list[dict[str, Any]],
        attempts: list[dict[str, Any]],
        event_sink: TraceSink | None,
        next_attempt: int,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> GeneralSkillExecutionPlan:
        """根据之前的执行失败记录修复执行计划。

        将最近 3 次失败记录作为上下文提交给 LLM，要求修复代码。

        Args:
            attempts: 之前的执行尝试记录列表。
            next_attempt: 当前是第几次修复尝试。

        Returns:
            修复后的 GeneralSkillExecutionPlan。

        Raises:
            LLMError: 修复后的代码为空时。
        """
        _emit(
            trace,
            {"phase": "repair_planning", "message": f"正在生成第 {next_attempt} 次运行代码", "attempt": next_attempt},
            event_sink,
        )
        stage_data = {
            "skill": {
                "slug": skill.slug,
                "name": skill.name,
                "description": skill.description,
                "homepage": skill.homepage,
                "markdown": skill.skill_markdown,
                "package": _skill_package_payload(skill),
            },
            "runtime": {
                "languages": ["bash", "python"],
                "stdin_json": {
                    "query": query,
                    "skill_slug": skill.slug,
                    "skill_name": skill.name,
                    "skill_workspace": "<runtime absolute path to the restored skill folder>",
                    "skill_files": [file["path"] for file in _skill_files(skill)],
                },
                "timeout_seconds": RUN_TIMEOUT_SECONDS,
            },
            "previous_attempts": attempts[-3:],
        }
        payload = stage_payload(
            phase="Step Agent / General Skill Repair",
            user_message=query,
            conversation_context=conversation_context,
            memory_context=memory_context,
            instructions=REPAIR_PROMPT.read_text(encoding="utf-8"),
            stage_data=stage_data,
            output_contract=GENERAL_SKILL_PLAN_OUTPUT,
        )
        with llm_operation("general_skill.repair", attempt=next_attempt):
            raw = LLMClient(_with_min_tokens(model_config, GENERAL_SKILL_MAX_TOKENS)).generate_json(
                unified_system_prompt(),
                payload,
            )
        plan = GeneralSkillExecutionPlan.model_validate(raw)
        plan.runtime = _plan_runtime(plan)
        if not plan.code.strip():
            raise LLMError("General skill repaired runner code is empty")
        runtime_label = _runtime_label(plan.runtime)
        _emit(
            trace,
            {
                "phase": "plan_created",
                "message": f"已生成第 {next_attempt} 次 {runtime_label} runner",
                "attempt": next_attempt,
                "runtime": plan.runtime,
                "rationale": plan.rationale,
                "code": plan.code,
                "expected_output": plan.expected_output,
            },
            event_sink,
        )
        return plan

    def _execute_plan(
        self,
        skill: GeneralSkill,
        query: str,
        plan: GeneralSkillExecutionPlan,
        user_id: str,
        trace: list[dict[str, Any]],
        event_sink: TraceSink | None = None,
        attempt: int = 1,
    ) -> tuple[str, str, dict[str, Any]]:
        """在沙箱中执行技能代码计划。

        步骤：
        1. 创建临时目录并恢复技能文件包。
        2. 写入生成的 runner 脚本。
        3. 准备运行时环境和环境变量。
        4. 以子进程方式执行，通过 stdin 传入 JSON 参数。
        5. 流式读取 stdout/stderr，超时自动终止。
        6. 解析 stdout 中的 JSON 作为结构化结果。

        Returns:
            三元组 ``(stdout, stderr, structured_result)``。
        """
        run_dir = Path(mkdtemp(prefix="ultrarag_general_skill_"))
        skill_dir = run_dir / "skill"
        _materialize_skill_package(skill, skill_dir)
        runtime = _plan_runtime(plan)
        runner_path = run_dir / ("runner.sh" if runtime == "bash" else "runner.py")
        runner_path.write_text(plan.code, encoding="utf-8")
        stdin_payload = {
            "query": query,
            "skill_slug": skill.slug,
            "skill_name": skill.name,
            "user_id": user_id,
            "skill_workspace": str(skill_dir),
            "skill_files": [file["path"] for file in _skill_files(skill)],
        }
        _emit(
            trace,
            {
                "phase": "running_code",
                "message": f"正在运行第 {attempt} 次 {_runtime_label(runtime)} runner",
                "run_id": run_dir.name,
                "attempt": attempt,
                "runtime": runtime,
            },
            event_sink,
        )
        try:
            runtime_python = ensure_runtime_python()
            env = runtime_environment(os.environ.copy())
        except GeneralSkillRuntimeError as exc:
            structured = {
                "success": False,
                "error": "runtime_environment_error",
                "message": str(exc),
                "retryable": False,
            }
            _emit(
                trace,
                {
                    "phase": "runtime_environment_failed",
                    "message": "通用技能运行环境准备失败",
                    "attempt": attempt,
                    "runtime": runtime,
                    "structured_result": structured,
                },
                event_sink,
            )
            return "", str(exc), structured
        env.update(
            {
                "ARGUMENTS": query,
                "QUERY": query,
                "SKILL_WORKSPACE": str(skill_dir),
                "SKILL_SLUG": skill.slug,
                "SKILL_NAME": skill.name,
                "USER_ID": user_id,
                "SKILL_FILES_JSON": json.dumps([file["path"] for file in _skill_files(skill)], ensure_ascii=False),
            }
        )
        if runtime == "bash" and not _bash_supported():
            structured = {
                "success": False,
                "error": "bash_runtime_unsupported",
                "message": "当前运行环境不支持 bash 技能（Windows 或打包版），请改用 Python 技能。",
                "retryable": False,
            }
            _emit(trace, {"phase": "runtime_environment_failed",
                          "message": "bash runtime 不受支持", "attempt": attempt,
                          "runtime": runtime, "structured_result": structured}, event_sink)
            return "", structured["message"], structured
        command = ["/bin/bash", str(runner_path)] if runtime == "bash" else [str(runtime_python), str(runner_path)]
        cwd = str(skill_dir if runtime == "bash" else run_dir)
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            text=False,
        )
        if process.stdin:
            process.stdin.write(json.dumps(stdin_payload, ensure_ascii=False).encode("utf-8"))
            process.stdin.close()

        try:
            stdout, stderr, timed_out = _stream_process_output(process, trace, event_sink, attempt)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

        if timed_out:
            stdout = _truncate(stdout)
            stderr = _truncate(stderr)
            structured = {"success": False, "error": "runner_timeout", "message": "通用技能运行超时"}
            _emit(
                trace,
                {
                    "phase": "code_timeout",
                    "message": f"{_runtime_label(runtime)} runner 执行超时",
                    "attempt": attempt,
                    "runtime": runtime,
                    "stdout_preview": stdout[:600],
                    "stderr_preview": stderr[:600],
                    "structured_result": structured,
                },
                event_sink,
            )
            return stdout, stderr, structured

        return_code = process.wait()
        stdout = _truncate(stdout)
        stderr = _truncate(stderr)
        structured = _parse_stdout_json(stdout)
        if return_code != 0:
            structured.setdefault("success", False)
            structured.setdefault("error", f"runner exited with code {return_code}")
        _emit(
            trace,
            {
                "phase": "code_finished",
                "message": f"{_runtime_label(runtime)} runner 执行完成",
                "attempt": attempt,
                "runtime": runtime,
                "return_code": return_code,
                "stdout_preview": stdout[:600],
                "stderr_preview": stderr[:600],
                "structured_result": structured,
            },
            event_sink,
        )
        return stdout, stderr, structured

    def _generate_reply(
        self,
        skill: GeneralSkill,
        query: str,
        model_config: ModelConfig,
        trace: list[dict[str, Any]],
        stdout: str,
        stderr: str,
        structured_result: dict[str, Any],
        event_sink: TraceSink | None = None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> str:
        """根据执行结果生成面向用户的自然语言回复。

        Args:
            stdout: 执行的标准输出。
            stderr: 执行的错误输出。
            structured_result: 解析后的结构化结果。

        Returns:
            面向用户的回复文本。

        Raises:
            LLMError: 回复为空或 JSON Schema 无效时。
        """
        _emit(trace, {"phase": "replying", "message": "正在根据运行结果生成回复"}, event_sink)
        stage_data = {
            "skill": {
                "slug": skill.slug,
                "name": skill.name,
                "description": skill.description,
            },
            "execution_trace": trace,
            "stdout": stdout,
            "stderr": stderr,
            "structured_result": structured_result,
        }
        payload = stage_payload(
            phase="Response Generator / General Skill Reply",
            user_message=query,
            conversation_context=conversation_context,
            memory_context=memory_context,
            instructions=REPLY_PROMPT.read_text(encoding="utf-8"),
            stage_data=stage_data,
            output_contract=GENERAL_SKILL_REPLY_OUTPUT,
        )
        try:
            with llm_operation("general_skill.reply"):
                raw = LLMClient(model_config).generate_json(
                    unified_system_prompt(), payload
                )
            reply = GeneralSkillReply.model_validate(raw).reply.strip()
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"General skill reply returned invalid JSON schema: {exc}") from exc
        if not reply:
            raise LLMError("General skill reply is empty")
        _emit(trace, {"phase": "reply_created", "message": "已生成最终回复"}, event_sink)
        return reply

    def _review_execution_result(
        self,
        skill: GeneralSkill,
        query: str,
        model_config: ModelConfig,
        plan: GeneralSkillExecutionPlan,
        stdout: str,
        stderr: str,
        structured_result: dict[str, Any],
        trace: list[dict[str, Any]],
        event_sink: TraceSink | None,
        attempt: int,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> dict[str, Any]:
        """审查执行结果，决定是否需要重试。

        将执行计划、代码、输出和结构化结果提交给 LLM 做审查。
        若 LLM 审查失败，则使用运行信号（stderr/结构化错误）做兜底判断。

        Returns:
            审查结果字典，含 result_sufficient、needs_retry、terminal 等字段。
        """
        _emit(
            trace,
            {
                "phase": "reflection_reviewing",
                "message": f"正在校验第 {attempt} 次运行结果",
                "attempt": attempt,
            },
            event_sink,
        )
        stage_data = {
            "skill": {
                "slug": skill.slug,
                "name": skill.name,
                "description": skill.description,
                "homepage": skill.homepage,
                "markdown": _truncate(skill.skill_markdown, 6000),
                "package": _skill_package_payload(skill, preview_limit=6000),
            },
            "runner": {
                "rationale": plan.rationale,
                "expected_output": plan.expected_output,
                "code_preview": _truncate(plan.code, 6000),
            },
            "attempt": attempt,
            "stdout": _truncate(stdout),
            "stderr": _truncate(stderr),
            "structured_result": structured_result,
        }
        payload = stage_payload(
            phase="Reflection / General Skill Review",
            user_message=query,
            conversation_context=conversation_context,
            memory_context=memory_context,
            instructions=REVIEW_PROMPT.read_text(encoding="utf-8"),
            stage_data=stage_data,
            output_contract=GENERAL_SKILL_REVIEW_OUTPUT,
        )
        try:
            with llm_operation("general_skill.review", attempt=attempt):
                raw = LLMClient(model_config).generate_json(
                    unified_system_prompt(), payload
                )
            review = GeneralSkillExecutionReview.model_validate(raw).model_dump(mode="json")
        except Exception as exc:
            fallback_needs_retry = _execution_needs_retry(stdout, stderr, structured_result)
            review = {
                "result_sufficient": not fallback_needs_retry,
                "needs_retry": fallback_needs_retry,
                "terminal": False,
                "reason": f"模型校验失败，使用运行信号兜底判断：{exc}",
                "repair_hint": "补充运行诊断或调整 runner 输出结构",
            }
        if review.get("terminal") is True:
            review["needs_retry"] = False
        _emit(
            trace,
            {
                "phase": "reflection_reviewed",
                "message": "已完成运行结果校验",
                "attempt": attempt,
                "review": review,
            },
            event_sink,
        )
        return review


def _truncate(value: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """将字符串截断到指定长度，超出时追加截断标记。"""

    if len(value) <= limit:
        return value
    return value[:limit] + "\n...<truncated>"


def _plan_runtime(plan: GeneralSkillExecutionPlan) -> str:
    """从执行计划中解析运行时类型，归一化为 ``bash`` 或 ``python``。"""

    runtime = str(getattr(plan, "runtime", "") or "python").strip().lower()
    if runtime in {"bash", "shell", "sh"}:
        return "bash"
    return "python"


def _runtime_label(runtime: str) -> str:
    """返回运行时类型的中文显示标签。"""

    return "Bash" if runtime == "bash" else "Python"


def _skill_files(skill: GeneralSkill) -> list[dict[str, Any]]:
    """从通用技能中提取文件列表。

    优先使用 skill_files_json 字段，若为空则使用 skill_markdown 生成默认的 SKILL.md。
    """

    raw_files = getattr(skill, "skill_files_json", None)
    files = raw_files if isinstance(raw_files, list) else []
    normalized: list[dict[str, Any]] = []
    for raw_file in files:
        if not isinstance(raw_file, dict):
            continue
        path = _safe_package_path(str(raw_file.get("path") or ""))
        content = str(raw_file.get("content") or "")
        if not path:
            continue
        normalized.append(
            {
                "path": path,
                "content": content,
                "size": int(raw_file.get("size") or len(content.encode("utf-8"))),
                "mime_type": raw_file.get("mime_type"),
            }
        )
    if normalized:
        return normalized
    markdown = str(getattr(skill, "skill_markdown", "") or "")
    return [{"path": "SKILL.md", "content": markdown, "size": len(markdown.encode("utf-8")), "mime_type": "text/markdown"}]


def _skill_package_payload(skill: GeneralSkill, preview_limit: int = 12000) -> dict[str, Any]:
    """构建技能文件包的预览负载（含文件路径、大小、内容预览和截断标记）。

    Args:
        skill: 通用技能。
        preview_limit: 所有文件预览的字符总量上限。
    """

    files = _skill_files(skill)
    previews: list[dict[str, Any]] = []
    remaining = preview_limit
    for file in files:
        content = str(file.get("content") or "")
        preview = content[: max(0, min(len(content), remaining))]
        remaining -= len(preview)
        previews.append(
            {
                "path": file["path"],
                "size": file.get("size"),
                "mime_type": file.get("mime_type"),
                "content_preview": preview,
                "truncated": len(preview) < len(content),
            }
        )
    return {
        "entrypoint": "SKILL.md",
        "file_count": len(files),
        "files": previews,
    }


def _materialize_skill_package(skill: GeneralSkill, target_dir: Path) -> None:
    """将技能文件包恢复到目标目录中（用于沙箱执行）。"""

    target_dir.mkdir(parents=True, exist_ok=True)
    for file in _skill_files(skill):
        relative_path = _safe_package_path(str(file["path"]))
        output_path = target_dir / relative_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(str(file.get("content") or ""), encoding="utf-8")


def _safe_package_path(path: str) -> str:
    """清洗文件路径，防止目录穿越攻击。

    返回规范化后的相对路径，若包含 ``..`` 则返回空字符串。
    """

    cleaned = path.replace("\\", "/").strip().strip("/")
    parts = [part for part in cleaned.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def _parse_stdout_json(stdout: str) -> dict[str, Any]:
    """解析子进程 stdout 为结构化字典。

    尝试 JSON 解析；若不是有效 JSON 则包装为 ``{"success": True, "text": ...}``。
    """

    stripped = stdout.strip()
    if not stripped:
        return {"success": False, "message": "runner produced no stdout"}
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            return value
        return {"success": True, "data": value}
    except json.JSONDecodeError:
        return {"success": True, "text": stripped}


def _stream_process_output_selectors(
    process: subprocess.Popen[bytes],
    trace: list[dict[str, Any]],
    event_sink: TraceSink | None,
    attempt: int,
) -> tuple[str, str, bool]:
    """使用 selectors 模块流式读取子进程的 stdout 和 stderr。

    适用于 Unix 平台，通过非阻塞 I/O 和事件选择器高效读取输出。
    超过 RUN_TIMEOUT_SECONDS 后自动终止进程。

    Returns:
        三元组 ``(stdout, stderr, timed_out)``。
    """
    selector = selectors.DefaultSelector()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    streams: list[tuple[Any, str]] = []
    if process.stdout:
        streams.append((process.stdout, "stdout"))
    if process.stderr:
        streams.append((process.stderr, "stderr"))
    for stream, name in streams:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, data=name)

    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    timed_out = False
    try:
        while selector.get_map():
            if time.monotonic() > deadline:
                timed_out = True
                process.kill()
                break
            events = selector.select(timeout=0.1)
            if not events and process.poll() is not None:
                events = [(key, selectors.EVENT_READ) for key in list(selector.get_map().values())]
            for key, _ in events:
                name = str(key.data)
                try:
                    chunk = os.read(key.fileobj.fileno(), 4096)
                except BlockingIOError:
                    continue
                if not chunk:
                    try:
                        selector.unregister(key.fileobj)
                    except KeyError:
                        pass
                    continue
                text = chunk.decode("utf-8", errors="replace")
                if name == "stdout":
                    stdout_parts.append(text)
                    phase = "stdout_chunk"
                    message = "收到运行输出"
                else:
                    stderr_parts.append(text)
                    phase = "stderr_chunk"
                    message = "收到错误输出"
                _emit(
                    trace,
                    {"phase": phase, "message": message, "attempt": attempt, "text": text},
                    event_sink,
                )
    finally:
        selector.close()
    return "".join(stdout_parts), "".join(stderr_parts), timed_out


def _use_thread_reader() -> bool:
    """判断是否应使用线程读取器（Windows 平台不支持 selectors 非阻塞 I/O）。"""

    return sys.platform == "win32"


def _stream_process_output(process, trace, event_sink, attempt):
    """流式读取子进程输出，根据平台自动选择 selectors 或线程方式。"""

    if _use_thread_reader():
        return _stream_process_output_threaded(process, trace, event_sink, attempt)
    return _stream_process_output_selectors(process, trace, event_sink, attempt)


def _stream_process_output_threaded(process, trace, event_sink, attempt):
    """使用线程方式流式读取子进程的 stdout 和 stderr（Windows 平台）。

    为每个输出流创建独立的读取线程，通过队列收集数据。
    """

    q: "queue.Queue[tuple[str, bytes]]" = queue.Queue()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    def _reader(stream, name: str) -> None:
        try:
            for chunk in iter(lambda: stream.read(4096), b""):
                q.put((name, chunk))
        finally:
            q.put((name, b""))  # EOF 标记

    stream_map = [(process.stdout, "stdout"), (process.stderr, "stderr")]
    threads: list[threading.Thread] = []
    for stream, name in stream_map:
        if stream is None:
            continue
        t = threading.Thread(target=_reader, args=(stream, name), daemon=True)
        t.start()
        threads.append(t)

    open_streams = sum(1 for s, _ in stream_map if s is not None)
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    timed_out = False
    eof_count = 0
    while eof_count < open_streams:
        if time.monotonic() > deadline:
            timed_out = True
            process.kill()
            break
        try:
            name, chunk = q.get(timeout=0.1)
        except queue.Empty:
            continue
        if chunk == b"":
            eof_count += 1
            continue
        text = chunk.decode("utf-8", errors="replace")
        if name == "stdout":
            stdout_parts.append(text)
            phase, message = "stdout_chunk", "收到运行输出"
        else:
            stderr_parts.append(text)
            phase, message = "stderr_chunk", "收到错误输出"
        _emit(trace, {"phase": phase, "message": message, "attempt": attempt, "text": text}, event_sink)

    for t in threads:
        t.join(timeout=1.0)
    return "".join(stdout_parts), "".join(stderr_parts), timed_out


def _bash_supported() -> bool:
    """检查当前环境是否支持 bash 运行时（Windows 和打包版不支持）。"""

    if sys.platform == "win32":
        return False
    if paths.is_frozen():
        return False
    return Path("/bin/bash").exists()


def _emit(trace: list[dict[str, Any]], item: dict[str, Any], event_sink: TraceSink | None = None) -> None:
    """向执行轨迹追加事件，并可选地通过回调转发。"""

    trace.append(item)
    if event_sink:
        event_sink(item)


def _execution_needs_retry(stdout: str, stderr: str, structured_result: dict[str, Any]) -> bool:
    """根据运行信号判断是否需要重试（兜底逻辑，当 LLM 审查不可用时使用）。

    判断规则：
    - 结构化结果标记 success=False 且非 terminal/retryable=False -> 需要重试。
    - 存在 error 或 stderr 非空 -> 需要重试。
    - stdout 为空 -> 需要重试。
    """

    if structured_result.get("success") is False:
        if structured_result.get("retryable") is False or structured_result.get("terminal") is True:
            return False
        return True
    if structured_result.get("error") or structured_result.get("error_code"):
        return True
    if stderr.strip():
        return True
    if not stdout.strip():
        return True
    return False


def _normalize_failure_diagnostics(structured_result: dict[str, Any]) -> None:
    """为失败的结构化结果补充诊断信息要求。

    当结果标记为失败但缺少诊断字段时，追加 diagnostics_missing 标记
    和 diagnostics_required 列表，提示 LLM 在下一轮修复中补充诊断信息。
    """

    if structured_result.get("success") is not False:
        return
    diagnostic_keys = {
        "diagnostics",
        "attempted_urls",
        "status_code",
        "exception",
        "exception_type",
        "response_preview",
        "parse_strategy",
    }
    if any(key in structured_result for key in diagnostic_keys):
        return
    structured_result.setdefault("diagnostics_missing", True)
    structured_result.setdefault(
        "diagnostics_required",
        [
            "attempted_urls",
            "status_code",
            "exception_type",
            "exception_message",
            "response_preview",
            "parse_strategy",
            "retryable",
        ],
    )


def _with_min_tokens(model_config: ModelConfig, max_output_tokens: int) -> ModelConfig:
    """对模型配置做快照，确保输出令牌数不低于指定值。"""

    return snapshot_model_config(model_config, min_output_tokens=max_output_tokens)


def _fallback_reply(structured_result: dict[str, Any]) -> str:
    """当 LLM 回复生成失败时的兜底回复。根据结构化结果的 success 字段生成不同消息。"""

    if structured_result.get("success") is False:
        message = str(structured_result.get("message") or structured_result.get("error") or "").strip()
        return f"抱歉，通用技能运行失败。{message}" if message else "抱歉，通用技能运行失败。"
    return "通用技能已运行完成，结果已展示在运行输出中。"

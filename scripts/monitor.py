#!/usr/bin/env python3
"""Run persistent Xianyu tasks and emit only newly observed items by default."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if __package__:
    from .spider import (
        SearchCancelledError,
        SpiderError,
        XianyuSpider,
        cleanup_evidence,
        resolve_proxy,
        search_capability_status,
    )
    from .task_manager import (
        RecordRunProgress,
        TaskManager,
        TaskMutationInterrupted,
        TaskMutationPersistenceError,
        TaskMutationProgress,
        _raise_if_async_task_cancelling,
    )
else:
    from spider import (
        SearchCancelledError,
        SpiderError,
        XianyuSpider,
        cleanup_evidence,
        resolve_proxy,
        search_capability_status,
    )
    from task_manager import (
        RecordRunProgress,
        TaskManager,
        TaskMutationInterrupted,
        TaskMutationPersistenceError,
        TaskMutationProgress,
        _raise_if_async_task_cancelling,
    )


def _interruption_cause(error: BaseException) -> BaseException:
    cause = getattr(error, "cause_error", None)
    return cause if isinstance(cause, BaseException) else error


def _retained_mutation_result(
    progress: RecordRunProgress,
    error: BaseException | None = None,
) -> tuple[str, list[dict[str, Any]]] | None:
    for source in (progress, error):
        if source is None:
            continue
        status = getattr(source, "persistence_status", None)
        if status == "recorded":
            result = getattr(source, "result", None)
            if isinstance(result, list):
                return status, list(result)
        if status == "not-established":
            possible_result = getattr(source, "possible_result", None)
            if isinstance(possible_result, list):
                return status, list(possible_result)
    # Backward-compatible evidence for injected test doubles and older
    # TaskManager implementations.
    if progress.committed:
        return "recorded", list(progress.result)
    if error is not None and getattr(error, "committed", False):
        result = getattr(error, "result", None)
        if isinstance(result, list):
            return "recorded", list(result)
    return None


def _error_recording_status(
    progress: TaskMutationProgress,
    error: BaseException,
) -> str:
    """Resolve whether the primary error was durably attached to its task."""

    for source in (error, progress):
        status = getattr(source, "persistence_status", None)
        if status == "recorded":
            return (
                "not-recorded"
                if getattr(source, "result", None) is False
                else "recorded"
            )
        if status in {"not-recorded", "not-established"}:
            return status
    return "not-recorded"


def _retain_cleanup_failures(
    progress: MonitorRunProgress,
    error: BaseException,
) -> None:
    failures = getattr(error, "cleanup_failures", None)
    if not isinstance(failures, list):
        return
    for failure in failures:
        if failure not in progress.current_cleanup_failures:
            progress.current_cleanup_failures.append(failure)


def _committed_run_report(
    args: argparse.Namespace,
    task: dict[str, Any],
    spider: XianyuSpider,
    items: list[dict[str, Any]],
    new_items: list[dict[str, Any]],
    *,
    persistence_status: str = "recorded",
) -> dict[str, Any]:
    baseline_count = len(new_items) if args.baseline else 0
    delivered_items = [] if args.baseline else new_items
    persistence: dict[str, Any] = {"status": persistence_status}
    if persistence_status == "not-established":
        persistence["possible_duplicate"] = True
    return {
        "ok": persistence_status == "recorded",
        "task_id": task["id"],
        "keyword": task["keyword"],
        "criteria": task.get("criteria", ""),
        "pages_scraped": spider.pages_scraped,
        "matched_count": len(items),
        "new_count": len(delivered_items),
        "baseline_count": baseline_count,
        "items": items if args.include_seen else delivered_items,
        "search_capability": {"status": "passed-for-this-run"},
        "persistence": persistence,
        "authentication": {"status": "not-evaluated"},
        "identity": {"status": "not-evaluated"},
        "cleanup": cleanup_evidence(),
    }


def _effective_capability_status(
    spider: XianyuSpider | None,
    error: BaseException,
) -> str:
    status = search_capability_status(error)
    if status == "not-established" and spider is not None:
        observed = getattr(spider, "last_capability_status", "not-established")
        if observed in {
            "passed-for-this-run",
            "rejected-for-this-run",
            "not-established",
        }:
            status = observed
    return status


def _raise_if_task_cancelling() -> None:
    _raise_if_async_task_cancelling()


@dataclass
class MonitorRunProgress:
    reports: list[dict[str, Any]] = field(default_factory=list)
    current_report: dict[str, Any] | None = None
    current_capability_status: str = "not-established"
    current_cleanup_failures: list[str] = field(default_factory=list)

    def cancellation_reports(self, error: BaseException) -> list[dict[str, Any]]:
        reports = list(self.reports)
        if self.current_report is None:
            return reports
        current = dict(self.current_report)
        current["ok"] = False
        cause = _interruption_cause(error)
        if current.get("error_type") == "Interrupted":
            current["error"] = "task cancelled"
            current["error_type"] = type(cause).__name__
        else:
            current["interruption"] = {
                "status": "cancelled",
                "error_type": type(cause).__name__,
            }
        error_status = search_capability_status(error)
        current["search_capability"] = {
            "status": (
                self.current_capability_status
                if error_status == "not-established"
                else error_status
            )
        }
        failures = getattr(error, "cleanup_failures", None)
        effective_failures = (
            list(failures)
            if isinstance(failures, list) and failures
            else list(self.current_cleanup_failures)
        )
        current["cleanup"] = (
            {"status": "failed", "errors": effective_failures}
            if effective_failures
            else {"status": "complete-or-not-required"}
        )
        current.setdefault("error_recording", {"status": "not-attempted"})
        # If the exact report is already last, the task completed and was
        # persisted before the batch-level interruption. Keep that task
        # successful; the top-level cancellation still stops later tasks.
        if not reports or reports[-1] is not self.current_report:
            reports.append(current)
        return reports


class MonitorCancelledError(RuntimeError):
    """Carry completed and current-task evidence when monitoring is cancelled."""

    def __init__(
        self,
        reports: list[dict[str, Any]],
        cause: BaseException,
        *,
        capability_status: str | None = None,
        cleanup_failures: list[str] | None = None,
    ):
        super().__init__("monitor cancelled")
        self.reports = list(reports)
        self.cause_error = cause
        self.capability_status = capability_status or search_capability_status(cause)
        failures = cleanup_failures or getattr(cause, "cleanup_failures", None)
        if isinstance(failures, list):
            self.cleanup_failures = list(failures)


async def run_tasks(
    args: argparse.Namespace,
    *,
    progress: MonitorRunProgress | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    run_progress = progress if progress is not None else MonitorRunProgress()
    tasks_path = Path(args.tasks_file).expanduser()
    if not tasks_path.is_file():
        raise ValueError(f"task file does not exist: {tasks_path.resolve()}")
    proxy = resolve_proxy(args.proxy, getattr(args, "proxy_file", None))
    manager = TaskManager(args.tasks_file)
    if args.task_id:
        task = manager.get_task(args.task_id)
        if task is None:
            raise ValueError(f"task not found: {args.task_id}")
        if task.get("status") != "running":
            raise ValueError(f"task is not running: {args.task_id}")
        tasks = [task]
    else:
        tasks = manager.list_tasks(running_only=True)

    prepared_tasks: list[tuple[dict[str, Any], str | None]] = []
    for task in tasks:
        task_id = task["id"]
        state_file = args.state or task.get("state_file")
        if state_file and not Path(str(state_file)).expanduser().is_absolute():
            if args.state:
                raise ValueError("--state must be an absolute path")
            raise ValueError(
                f"task {task_id} uses a legacy relative login-state path; "
                "pass --state with an absolute path or recreate the task"
            )
        prepared_tasks.append((task, state_file))

    reports = run_progress.reports
    had_error = False
    for task, state_file in prepared_tasks:
        _raise_if_task_cancelling()
        task_id = task["id"]
        items: list[dict[str, Any]] = []
        search_passed = False
        spider: XianyuSpider | None = None
        run_progress.current_capability_status = "not-established"
        run_progress.current_cleanup_failures = []
        run_progress.current_report = {
            "ok": False,
            "task_id": task_id,
            "keyword": task["keyword"],
            "error": "task did not complete",
            "error_type": "Interrupted",
            "search_capability": {"status": "not-established"},
            "authentication": {"status": "not-evaluated"},
            "identity": {"status": "not-evaluated"},
            "cleanup": {"status": "complete-or-not-required"},
            "persistence": {"status": "not-attempted"},
            "error_recording": {"status": "not-attempted"},
        }
        record_progress = RecordRunProgress()
        try:
            spider = XianyuSpider(
                state_file=state_file,
                proxy=proxy,
                headless=not args.headed,
                browser_channel=args.browser_channel,
                verbose=not getattr(args, "quiet_if_empty", False),
            )
            items = await spider.search(
                keyword=task["keyword"],
                min_price=task.get("min_price"),
                max_price=task.get("max_price"),
                location=task.get("location"),
                pages=int(task.get("pages", 1)),
                max_retries=int(task.get("retries", 3)),
            )
            search_passed = True
            run_progress.current_capability_status = "passed-for-this-run"
            run_progress.current_report.update(
                {
                    "pages_scraped": spider.pages_scraped,
                    "matched_count": len(items),
                    "search_capability": {"status": "passed-for-this-run"},
                    "persistence": {"status": "not-recorded"},
                }
            )
            _raise_if_task_cancelling()
            new_items = manager.record_run(
                task_id,
                items,
                progress=record_progress,
            )
            run_progress.current_report["persistence"] = {"status": "recorded"}
            _raise_if_task_cancelling()
            report = _committed_run_report(args, task, spider, items, new_items)
        except TaskMutationInterrupted as exc:
            cause = _interruption_cause(exc)
            setattr(cause, "capability_status", "passed-for-this-run")
            failures = getattr(exc, "cleanup_failures", None)
            run_progress.current_capability_status = "passed-for-this-run"
            run_progress.current_cleanup_failures = (
                list(failures) if isinstance(failures, list) else []
            )
            retained = _retained_mutation_result(record_progress, exc)
            if retained is not None and spider is not None:
                persistence_status, retained_items = retained
                report = _committed_run_report(
                    args,
                    task,
                    spider,
                    items,
                    retained_items,
                    persistence_status=persistence_status,
                )
                interruption_status = (
                    "cancelled-after-task-commit"
                    if persistence_status == "recorded"
                    else "cancelled-with-task-commit-not-established"
                )
                report.update(
                    {
                        "interruption": {
                            "status": interruption_status,
                            "error_type": type(cause).__name__,
                        },
                        "cleanup": cleanup_evidence(exc),
                        "error_recording": {"status": "not-attempted"},
                    }
                )
                reports.append(report)
                run_progress.current_report = None
                cancelled_reports = list(reports)
            else:
                persistence_status = exc.task_commit_status
                run_progress.current_report["persistence"] = {
                    "status": persistence_status
                }
                cancelled_reports = run_progress.cancellation_reports(exc)
                reports[:] = cancelled_reports
                run_progress.current_report = None
            raise MonitorCancelledError(
                cancelled_reports,
                cause,
                capability_status="passed-for-this-run",
                cleanup_failures=run_progress.current_cleanup_failures,
            ) from exc
        except TaskMutationPersistenceError as exc:
            had_error = True
            cause = _interruption_cause(exc)
            retained = _retained_mutation_result(record_progress, exc)
            if retained is None or spider is None:
                raise ValueError(
                    "task persistence failure has no retained result evidence"
                ) from exc
            persistence_status, retained_items = retained
            failures = getattr(exc, "cleanup_failures", None)
            run_progress.current_capability_status = "passed-for-this-run"
            run_progress.current_cleanup_failures = (
                list(failures) if isinstance(failures, list) else []
            )
            report = _committed_run_report(
                args,
                task,
                spider,
                items,
                retained_items,
                persistence_status=persistence_status,
            )
            report.update(
                {
                    "ok": False,
                    "error": str(cause),
                    "error_type": type(cause).__name__,
                    "finalization": {
                        "status": (
                            "failed"
                            if persistence_status == "recorded"
                            else "commit-status-not-established"
                        )
                    },
                    "cleanup": cleanup_evidence(exc),
                    "error_recording": {"status": "not-attempted"},
                }
            )
            run_progress.current_report = report
            reports.append(report)
            run_progress.current_report = None
            break
        except (KeyboardInterrupt, asyncio.CancelledError, SearchCancelledError) as exc:
            retained = _retained_mutation_result(record_progress)
            if retained is not None and spider is not None:
                persistence_status, retained_items = retained
                setattr(exc, "search_passed", True)
                setattr(exc, "capability_status", "passed-for-this-run")
                report = _committed_run_report(
                    args,
                    task,
                    spider,
                    items,
                    retained_items,
                    persistence_status=persistence_status,
                )
                interruption_status = (
                    "cancelled-after-task-commit"
                    if persistence_status == "recorded"
                    else "cancelled-with-task-commit-not-established"
                )
                report.update(
                    {
                        "interruption": {
                            "status": interruption_status,
                            "error_type": type(exc).__name__,
                        },
                        "cleanup": cleanup_evidence(exc),
                        "error_recording": {"status": "not-attempted"},
                    }
                )
                reports.append(report)
                run_progress.current_report = None
                raise MonitorCancelledError(
                    list(reports),
                    exc,
                    capability_status="passed-for-this-run",
                ) from exc
            capability_status = _effective_capability_status(spider, exc)
            if search_passed:
                setattr(exc, "search_passed", True)
                setattr(exc, "capability_status", "passed-for-this-run")
                run_progress.current_capability_status = "passed-for-this-run"
            elif capability_status == "passed-for-this-run":
                setattr(exc, "capability_status", capability_status)
                run_progress.current_capability_status = capability_status
            elif capability_status != "not-established":
                setattr(exc, "capability_status", capability_status)
                run_progress.current_capability_status = capability_status
            failures = getattr(exc, "cleanup_failures", None)
            if isinstance(failures, list):
                run_progress.current_cleanup_failures = list(failures)
            cancelled_reports = run_progress.cancellation_reports(exc)
            reports[:] = cancelled_reports
            run_progress.current_report = None
            raise MonitorCancelledError(
                cancelled_reports,
                exc,
                cleanup_failures=run_progress.current_cleanup_failures,
            ) from exc
        except (KeyError, OSError, SpiderError, TimeoutError, ValueError) as exc:
            had_error = True
            if search_passed:
                capability_status = "passed-for-this-run"
            else:
                capability_status = search_capability_status(exc)
            failures = getattr(exc, "cleanup_failures", None)
            run_progress.current_capability_status = capability_status
            run_progress.current_cleanup_failures = (
                list(failures) if isinstance(failures, list) else []
            )
            persistence_status = "not-recorded" if search_passed else "not-attempted"
            run_progress.current_report.update(
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "search_capability": {"status": capability_status},
                    "persistence": {"status": persistence_status},
                    "cleanup": cleanup_evidence(exc),
                }
            )
            report = {
                "ok": False,
                "task_id": task_id,
                "keyword": task["keyword"],
                "error": str(exc),
                "error_type": type(exc).__name__,
                "search_capability": {"status": capability_status},
                "persistence": {"status": persistence_status},
                "authentication": {"status": "not-evaluated"},
                "identity": {"status": "not-evaluated"},
                "cleanup": cleanup_evidence(exc),
                "error_recording": {"status": "not-attempted"},
            }
            if search_passed and spider is not None:
                report["pages_scraped"] = spider.pages_scraped
                report["matched_count"] = len(items)
            run_progress.current_report = report
            if run_progress.current_cleanup_failures:
                report["error_recording"] = {"status": "not-attempted"}
                reports.append(report)
                run_progress.current_report = None
                break
            error_progress = TaskMutationProgress()
            try:
                error_was_recorded = manager.record_error(
                    task_id,
                    str(exc),
                    progress=error_progress,
                )
                error_recording = "recorded" if error_was_recorded else "not-recorded"
                report["error_recording"] = {"status": error_recording}
                _raise_if_task_cancelling()
            except (
                TaskMutationInterrupted,
                KeyboardInterrupt,
                asyncio.CancelledError,
            ) as recording_interruption:
                cancellation = _interruption_cause(recording_interruption)
                error_recording = _error_recording_status(
                    error_progress,
                    recording_interruption,
                )
                report["error_recording"] = {"status": error_recording}
                _retain_cleanup_failures(run_progress, recording_interruption)
                setattr(cancellation, "capability_status", capability_status)
                for failure in run_progress.current_cleanup_failures:
                    existing = getattr(cancellation, "cleanup_failures", None)
                    if not isinstance(existing, list):
                        existing = []
                        setattr(cancellation, "cleanup_failures", existing)
                    if failure not in existing:
                        existing.append(failure)
                cancelled_reports = run_progress.cancellation_reports(cancellation)
                reports[:] = cancelled_reports
                run_progress.current_report = None
                raise MonitorCancelledError(
                    cancelled_reports,
                    cancellation,
                    cleanup_failures=run_progress.current_cleanup_failures,
                ) from recording_interruption
            except (
                TaskMutationPersistenceError,
                KeyError,
                OSError,
                TimeoutError,
                ValueError,
            ) as recording_error:
                error_recording = _error_recording_status(
                    error_progress,
                    recording_error,
                )
                _retain_cleanup_failures(run_progress, recording_error)
                if run_progress.current_cleanup_failures:
                    report["cleanup"] = {
                        "status": "failed",
                        "errors": list(run_progress.current_cleanup_failures),
                    }
            report["error_recording"] = {"status": error_recording}
            if run_progress.current_cleanup_failures:
                reports.append(report)
                run_progress.current_report = None
                break
        run_progress.current_report = report
        reports.append(report)
        run_progress.current_report = None
    return reports, had_error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Xianyu monitor tasks")
    parser.add_argument("--tasks-file", default="tasks.json")
    parser.add_argument("--task-id")
    parser.add_argument("--state", help="override task login-state path")
    proxy_group = parser.add_mutually_exclusive_group()
    proxy_group.add_argument(
        "--proxy",
        help="proxy URL; may be visible in process arguments",
    )
    proxy_group.add_argument(
        "--proxy-file",
        help="read proxy URL from a user-private UTF-8 file",
    )
    parser.add_argument(
        "--browser-channel", default=os.getenv("XIANYU_BROWSER_CHANNEL")
    )
    parser.add_argument("--headed", action="store_true")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--include-seen",
        action="store_true",
        help="include all matched items instead of only newly observed items",
    )
    output_group.add_argument(
        "--baseline",
        action="store_true",
        help="record current matches as seen without reporting them as new",
    )
    output_group.add_argument(
        "--quiet-if-empty",
        action="store_true",
        help="emit no stdout after a successful run with zero new listings",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_progress = MonitorRunProgress()
    try:
        reports, had_error = asyncio.run(run_tasks(args, progress=run_progress))
        payload = {
            "ok": not had_error,
            "task_count": len(reports),
            "new_count": sum(int(report.get("new_count", 0)) for report in reports),
            "tasks": reports,
        }
        suppress_output = (
            args.quiet_if_empty
            and not args.include_seen
            and payload["ok"]
            and payload["new_count"] == 0
        )
        if not suppress_output:
            print(json.dumps(payload, ensure_ascii=True, indent=2))
        # Keep success emission inside the cancellation evidence boundary.
        return 2 if had_error else 0  # noqa: TRY300
    except MonitorCancelledError as exc:
        reports = exc.reports
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "monitor cancelled",
                    "error_type": type(exc.cause_error).__name__,
                    "task_count": len(reports),
                    "new_count": sum(
                        int(report.get("new_count", 0)) for report in reports
                    ),
                    "tasks": reports,
                    "search_capability": {"status": exc.capability_status},
                    "authentication": {"status": "not-evaluated"},
                    "identity": {"status": "not-evaluated"},
                    "cleanup": cleanup_evidence(exc),
                },
                ensure_ascii=True,
            )
        )
        return 130
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        capability_status = search_capability_status(exc)
        if capability_status == "not-established":
            capability_status = run_progress.current_capability_status
        reports = run_progress.cancellation_reports(exc)
        failures = getattr(exc, "cleanup_failures", None)
        effective_failures = (
            list(failures)
            if isinstance(failures, list) and failures
            else list(run_progress.current_cleanup_failures)
        )
        cleanup = (
            {"status": "failed", "errors": effective_failures}
            if effective_failures
            else {"status": "complete-or-not-required"}
        )
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "monitor cancelled",
                    "error_type": type(exc).__name__,
                    "task_count": len(reports),
                    "new_count": sum(
                        int(report.get("new_count", 0)) for report in reports
                    ),
                    "tasks": reports,
                    "search_capability": {"status": capability_status},
                    "authentication": {"status": "not-evaluated"},
                    "identity": {"status": "not-evaluated"},
                    "cleanup": cleanup,
                },
                ensure_ascii=True,
            )
        )
        return 130
    except (OSError, SpiderError, TimeoutError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "task_count": 0,
                    "new_count": 0,
                    "tasks": [],
                    "search_capability": {"status": "not-established"},
                    "authentication": {"status": "not-evaluated"},
                    "identity": {"status": "not-evaluated"},
                    "cleanup": cleanup_evidence(exc),
                },
                ensure_ascii=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

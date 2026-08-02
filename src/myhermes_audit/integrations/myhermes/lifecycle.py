"""Public MyHermes resource shutdown used by an isolated worker."""

from __future__ import annotations

from collections.abc import Sequence

from myhermes_audit.integrations.myhermes.contracts import WorkerWarning


def close_runtime_resources(
    *,
    connection,
    session_id: str | None = None,
    session_ids: Sequence[str] = (),
    process_manager,
    model_client,
    shutdown_background_review: bool = True,
) -> list[WorkerWarning]:
    # All imports are intentionally lazy; see worker.py's import boundary.
    from hermes.delegate_jobs import shutdown_delegate_jobs
    from hermes.session_resources import (
        cleanup_all_session_resources,
        cleanup_session_resources,
    )

    warnings: list[WorkerWarning] = []
    if connection is not None:
        try:
            connection.close()
        except Exception:
            warnings.append(_warning("database_close_error"))

    background_complete = True
    if shutdown_background_review:
        try:
            from hermes.review.runtime import shutdown_background_review_runtime

            background_complete = shutdown_background_review_runtime(2.0) == 0
        except Exception:
            background_complete = False
            warnings.append(_warning("background_review_shutdown_error"))
        if not background_complete:
            warnings.append(_warning("background_review_shutdown_incomplete"))

    delegate_complete = True
    try:
        delegate_complete = not shutdown_delegate_jobs(2.0)
    except Exception:
        delegate_complete = False
        warnings.append(_warning("delegate_shutdown_error"))
    if not delegate_complete:
        warnings.append(_warning("delegate_shutdown_incomplete"))

    managed_session_ids = list(dict.fromkeys(session_ids))
    if session_id is not None and session_id not in managed_session_ids:
        managed_session_ids.append(session_id)
    for current_session_id in managed_session_ids:
        try:
            report = cleanup_session_resources(
                current_session_id,
                process_manager=process_manager,
            )
            if not report.complete:
                warnings.append(_warning("session_cleanup_incomplete"))
        except Exception:
            warnings.append(_warning("session_cleanup_error"))
    try:
        report = cleanup_all_session_resources(
            process_manager=process_manager,
            lifecycle_barrier_complete=(
                background_complete and delegate_complete
            ),
        )
        if not report.complete:
            warnings.append(_warning("global_cleanup_incomplete"))
    except Exception:
        warnings.append(_warning("global_cleanup_error"))

    close = getattr(model_client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            warnings.append(_warning("model_client_close_error"))
    return _deduplicate_warnings(warnings)


def _warning(warning_type: str) -> WorkerWarning:
    return WorkerWarning(
        warning_type=warning_type,
        message=f"MyHermes lifecycle warning: {warning_type}",
    )


def _deduplicate_warnings(items: list[WorkerWarning]) -> list[WorkerWarning]:
    seen: set[str] = set()
    result: list[WorkerWarning] = []
    for item in items:
        if item.warning_type not in seen:
            seen.add(item.warning_type)
            result.append(item)
    return result


__all__ = ("close_runtime_resources",)

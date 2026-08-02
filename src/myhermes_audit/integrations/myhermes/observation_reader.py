"""Read MyHermes' public Monitoring projections into the worker protocol."""

from __future__ import annotations

from pathlib import Path

from myhermes_audit.integrations.myhermes.contracts import (
    ModelObservationRecord,
    ObservationBundle,
    RunObservationRecord,
    ToolObservationRecord,
)


_PAGE_SIZE = 200
_MAX_OBSERVATIONS = 2_000


def latest_run_id(sqlite_path: Path, excluded: set[str]) -> str | None:
    # Imports stay inside the function so worker.py can validate its isolated
    # environment before any hermes module is imported.
    from hermes.observability import ObservationEventType, ObservationQuery
    from hermes.persistence.monitoring import SQLiteObservationReadRepository

    repository = SQLiteObservationReadRepository(sqlite_path)
    items = repository.list_observations(
        ObservationQuery(
            event_type=ObservationEventType.RUN_END,
            limit=_PAGE_SIZE,
        )
    )
    for item in items:
        if item.run_id not in excluded:
            return item.run_id
    return None


def read_observations(
    sqlite_path: Path,
    *,
    run_durations: dict[str, int],
) -> ObservationBundle:
    from hermes.observability import (
        ModelCallObservationView,
        ObservationQuery,
        RunObservationView,
        ToolCallObservationView,
    )
    from hermes.persistence.monitoring import SQLiteObservationReadRepository

    repository = SQLiteObservationReadRepository(sqlite_path)
    collected = []
    offset = 0
    truncated = False
    while len(collected) < _MAX_OBSERVATIONS:
        page = repository.list_observations(
            ObservationQuery(limit=_PAGE_SIZE, offset=offset)
        )
        collected.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        offset += len(page)
    if len(collected) > _MAX_OBSERVATIONS:
        collected = collected[:_MAX_OBSERVATIONS]
        truncated = True
    elif len(collected) == _MAX_OBSERVATIONS:
        extra = repository.list_observations(
            ObservationQuery(limit=1, offset=_MAX_OBSERVATIONS)
        )
        truncated = bool(extra)

    collected.sort(key=lambda item: (item.created_at, item.observation_id))
    runs: list[RunObservationRecord] = []
    model_calls: list[ModelObservationRecord] = []
    tool_calls: list[ToolObservationRecord] = []
    for item in collected:
        if isinstance(item, RunObservationView):
            runs.append(
                RunObservationRecord(
                    run_id=item.run_id,
                    parent_run_id=item.parent_run_id,
                    status=item.status,
                    stop_reason=item.stop_reason,
                    iterations=item.iterations,
                    tool_call_count=item.tool_call_count,
                    has_final_reply=item.has_final_reply,
                    duration_ms=run_durations.get(item.run_id),
                )
            )
        elif isinstance(item, ModelCallObservationView):
            model_calls.append(
                ModelObservationRecord(
                    run_id=item.run_id,
                    parent_run_id=item.parent_run_id,
                    finish_reason=item.finish_reason,
                    prompt_tokens=item.prompt_tokens,
                    completion_tokens=item.completion_tokens,
                    total_tokens=item.total_tokens,
                    duration_ms=item.duration_ms,
                    tool_call_count=item.tool_call_count,
                    error_category=None,
                    compression_applied=getattr(
                        item,
                        "compression_applied",
                        None,
                    ),
                    input_message_count=getattr(
                        item,
                        "input_message_count",
                        None,
                    ),
                    output_message_count=getattr(
                        item,
                        "output_message_count",
                        None,
                    ),
                )
            )
        elif isinstance(item, ToolCallObservationView):
            tool_calls.append(
                ToolObservationRecord(
                    run_id=item.run_id,
                    parent_run_id=item.parent_run_id,
                    tool_call_id=item.tool_call_id,
                    tool_name=item.tool_name,
                    status=item.status,
                    success=item.success,
                    error_type=item.error_type,
                    duration_ms=item.duration_ms,
                )
            )
    return ObservationBundle(
        runs=runs,
        model_calls=model_calls,
        tool_calls=tool_calls,
        truncated=truncated,
    )


__all__ = ("latest_run_id", "read_observations")

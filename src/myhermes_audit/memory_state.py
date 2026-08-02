"""Pure deterministic Memory snapshot comparison with no Subject imports."""

from __future__ import annotations

from myhermes_audit.contracts import (
    MemorySnapshotPhase,
    MemoryStateChange,
    MemoryStateChangeType,
    MemoryStateSnapshot,
)
from myhermes_audit.errors import MemoryStateValidationError
from myhermes_audit.serialization import canonical_sha256


def diff_memory_snapshots(
    before: MemoryStateSnapshot,
    after: MemoryStateSnapshot,
) -> list[MemoryStateChange]:
    """Return stable per-ID facts while preserving Subject-native item order."""

    if (
        before.phase is not MemorySnapshotPhase.BEFORE_CONVERSATION
        or after.phase is not MemorySnapshotPhase.AFTER_CONVERSATION
    ):
        raise MemoryStateValidationError(
            "Memory diff requires ordered before/after snapshots"
        )
    if before.strategy is not after.strategy or before.provider != after.provider:
        raise MemoryStateValidationError(
            "Memory snapshots use incompatible state semantics"
        )
    if after.captured_at < before.captured_at:
        raise MemoryStateValidationError(
            "Memory after snapshot predates the before snapshot"
        )

    before_by_id = {item.memory_id: item for item in before.items}
    after_by_id = {item.memory_id: item for item in after.items}
    ordered_ids = [item.memory_id for item in before.items]
    ordered_ids.extend(
        item.memory_id
        for item in after.items
        if item.memory_id not in before_by_id
    )
    changes: list[MemoryStateChange] = []
    for memory_id in ordered_ids:
        old = before_by_id.get(memory_id)
        new = after_by_id.get(memory_id)
        if old is None and new is not None:
            change_type = MemoryStateChangeType.ADDED
            kind = new.kind
        elif old is not None and new is None:
            change_type = MemoryStateChangeType.REMOVED
            kind = old.kind
        elif old is not None and new is not None:
            if old.kind is not new.kind:
                raise MemoryStateValidationError(
                    "Memory ID changed kind between snapshots",
                    memory_id=memory_id,
                )
            change_type = (
                MemoryStateChangeType.UNCHANGED
                if old == new
                else MemoryStateChangeType.MODIFIED
            )
            kind = old.kind
        else:
            raise MemoryStateValidationError(
                "Memory diff encountered an impossible identity state"
            )
        digest = canonical_sha256(
            {
                "memory_id": memory_id,
                "change_type": change_type.value,
                "before": None if old is None else old.stable_dump(),
                "after": None if new is None else new.stable_dump(),
            }
        )
        changes.append(
            MemoryStateChange(
                change_id=f"memory-change-{digest}",
                change_type=change_type,
                memory_id=memory_id,
                kind=kind,
                before=old,
                after=new,
                metadata={
                    "before_snapshot_id": before.snapshot_id,
                    "after_snapshot_id": after.snapshot_id,
                },
            )
        )
    return changes


__all__ = ("diff_memory_snapshots",)

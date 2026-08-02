"""MyHermes public-Memory adapter used only inside the isolated Worker."""

from __future__ import annotations

import hashlib
import inspect
import time
import unicodedata
from datetime import datetime, timezone

from myhermes_audit.contracts import (
    MemoryFixture,
    MemoryItem,
    MemoryKind,
    MemoryQuery,
    MemoryQueryPhase,
    MemoryQueryResult,
    MemorySnapshotPhase,
    MemoryStateSnapshot,
    RetrievedMemory,
    RetrievalStrategy,
)
from myhermes_audit.errors import (
    MemoryCapabilityError,
    MemoryClearError,
    MemoryKindUnsupportedError,
    MemoryMappingError,
    MemoryQueryError,
    MemoryScopeUnsupportedError,
    MemorySeedError,
    MemorySnapshotError,
    MemoryStrategyUnsupportedError,
)


_PROVIDER = "prompt_context_injection"
_STATE_PROVIDER = "myhermes_public_memory_state"
_TARGETS = (
    ("memory", MemoryKind.LONG_TERM),
    ("user", MemoryKind.USER_PROFILE),
)


class MyHermesMemoryAdapter:
    """Translate strict Audit contracts to public MyHermes Memory calls."""

    def __init__(self, *, strategy: RetrievalStrategy) -> None:
        if strategy not in {
            RetrievalStrategy.SUBJECT_NATIVE,
            RetrievalStrategy.DISABLED,
        }:
            raise MemoryStrategyUnsupportedError(
                "MyHermes adapter has no public implementation for requested strategy",
                requested_strategy=strategy.value,
                supported_strategies=[
                    RetrievalStrategy.SUBJECT_NATIVE.value,
                    RetrievalStrategy.DISABLED.value,
                ],
                missing_capability="ranked_query",
            )
        # This module is imported only after Worker isolation validation. These
        # are public Subject interfaces; no private Memory path is accessed.
        from hermes.tools.memory import (
            mutate_memory_entries,
            read_memory_entries,
            render_memory_section,
        )

        self.strategy = strategy
        self.provider = _PROVIDER if strategy is RetrievalStrategy.SUBJECT_NATIVE else "disabled"
        self._read = read_memory_entries
        self._mutate = mutate_memory_entries
        self._render = render_memory_section
        self._fixture_by_target_content: dict[str, dict[str, MemoryItem]] = {
            target: {} for target, _kind in _TARGETS
        }
        self._fixture_normalized_content: dict[str, set[str]] = {
            target: set() for target, _kind in _TARGETS
        }
        self._seeded: list[tuple[str, str, str]] = []
        self._validate_public_signatures()

    @property
    def seeded_memory_ids(self) -> list[str]:
        return [memory_id for _target, _content, memory_id in self._seeded]

    def _validate_public_signatures(self) -> None:
        try:
            inspect.signature(self._read).bind(target="memory")
            inspect.signature(self._mutate).bind(
                "add",
                target="memory",
                content="placeholder",
                old_text="",
            )
            inspect.signature(self._render).bind(
                include_long=True,
                include_user=True,
            )
        except (TypeError, ValueError) as exc:
            raise MemoryCapabilityError(
                "Subject public Memory call shape is incompatible",
                missing_capability="memory_public_call_shape",
            ) from exc

    async def seed(self, fixture: MemoryFixture) -> None:
        pending: list[tuple[str, MemoryItem]] = []
        for item in fixture.items:
            target = _target_for_kind(item.kind)
            by_content = self._fixture_by_target_content[target]
            normalized = _normalized_content(item.content).casefold()
            if normalized in self._fixture_normalized_content[target]:
                raise MemoryMappingError(
                    "Fixture entries are indistinguishable in a Subject target",
                    target=target,
                )
            by_content[item.content] = item
            self._fixture_normalized_content[target].add(normalized)
            pending.append((target, item))
        for target, item in pending:
            response = self._safe_mutate(
                "add",
                target=target,
                content=item.content,
                error_class=MemorySeedError,
            )
            if response.get("ok") is not True:
                raise MemorySeedError(
                    "Subject rejected a Memory fixture entry",
                    target=target,
                    subject_error_type=_safe_subject_error_type(response),
                )
            self._seeded.append((target, item.content, item.memory_id))

    async def query(
        self,
        query: MemoryQuery,
        *,
        query_id: str,
        phase: MemoryQueryPhase,
    ) -> MemoryQueryResult:
        started = time.perf_counter()
        if query.user_id is not None or query.session_id is not None or query.filters:
            raise MemoryScopeUnsupportedError(
                "Subject-native prompt exposure has no public query scope filtering",
                query_id=query_id,
                missing_capability="user/session/query_filtering",
            )
        if self.strategy is RetrievalStrategy.DISABLED:
            return MemoryQueryResult(
                query_id=query_id,
                phase=phase,
                query=query,
                strategy=self.strategy,
                provider="disabled",
                items=[],
                duration_ms=_duration_ms(started),
                metadata={
                    "query_used": False,
                    "score_semantics": "none",
                    "include_memory": False,
                    "include_user_profile": False,
                },
            )

        exposed: list[MemoryItem] = []
        for target, kind in _TARGETS:
            native_items = self._read_target(target, kind, MemoryQueryError)
            try:
                rendered = self._render(
                    include_long=target == "memory",
                    include_user=target == "user",
                )
            except Exception as exc:
                raise MemoryQueryError(
                    "Subject public Memory prompt rendering failed",
                    query_id=query_id,
                    subject_exception_type=type(exc).__name__,
                ) from exc
            if rendered is None:
                if native_items:
                    raise MemoryCapabilityError(
                        "Subject prompt projection omitted readable Memory entries",
                        query_id=query_id,
                        missing_capability="deterministic_memory_prompt_projection",
                    )
                continue
            if not isinstance(rendered, str):
                raise MemoryCapabilityError(
                    "Subject Memory prompt projection has an invalid public shape",
                    query_id=query_id,
                    missing_capability="deterministic_memory_prompt_projection",
                )
            for item in native_items:
                if item.content not in rendered:
                    raise MemoryCapabilityError(
                        "Subject prompt exposure cannot be mapped to public entries",
                        query_id=query_id,
                        missing_capability="deterministic_memory_prompt_projection",
                    )
                exposed.append(item)

        selected = exposed[: query.top_k]
        return MemoryQueryResult(
            query_id=query_id,
            phase=phase,
            query=query,
            strategy=self.strategy,
            provider=_PROVIDER,
            items=[
                RetrievedMemory(
                    memory_id=item.memory_id,
                    kind=item.kind,
                    content=item.content,
                    rank=rank,
                    score=None,
                    source=item.source,
                    metadata={
                        **item.metadata,
                        "query_used": False,
                        "score_semantics": "none",
                    },
                )
                for rank, item in enumerate(selected, start=1)
            ],
            duration_ms=_duration_ms(started),
            metadata={
                "query_used": False,
                "score_semantics": "none",
                "provider_semantics": "prompt_context_injection",
                "include_memory": True,
                "include_user_profile": True,
                "exposed_item_count": len(exposed),
            },
        )

    async def snapshot(
        self,
        *,
        phase: MemorySnapshotPhase,
    ) -> MemoryStateSnapshot:
        items: list[MemoryItem] = []
        for target, kind in _TARGETS:
            items.extend(self._read_target(target, kind, MemorySnapshotError))
        return MemoryStateSnapshot(
            snapshot_id=f"memory-{phase.value}",
            phase=phase,
            strategy=self.strategy,
            provider=_STATE_PROVIDER,
            captured_at=datetime.now(timezone.utc),
            items=items,
            metadata={
                "targets": [target for target, _kind in _TARGETS],
                "supported_kinds": [kind.value for _target, kind in _TARGETS],
                "public_capabilities": [
                    "read_memory_entries",
                    "mutate_memory_entries",
                    "render_memory_section",
                ],
                "identity_semantics": "fixture_exact_or_target_content_sha256",
                "native_order_preserved": True,
            },
        )

    async def clear(self) -> None:
        by_target: dict[str, list[tuple[str, str]]] = {
            target: [] for target, _kind in _TARGETS
        }
        for target, content, memory_id in self._seeded:
            by_target[target].append((content, memory_id))
        for target, entries in by_target.items():
            if not entries:
                continue
            kind = dict(_TARGETS)[target]
            current = {
                item.content
                for item in self._read_target(target, kind, MemoryClearError)
            }
            # Removing longer entries first keeps Subject substring matching unique.
            for content, _memory_id in sorted(
                entries,
                key=lambda item: len(item[0]),
                reverse=True,
            ):
                if content not in current:
                    continue
                response = self._safe_mutate(
                    "remove",
                    target=target,
                    content=content,
                    error_class=MemoryClearError,
                )
                if response.get("ok") is not True:
                    raise MemoryClearError(
                        "Subject rejected managed Memory cleanup",
                        target=target,
                        subject_error_type=_safe_subject_error_type(response),
                    )
                current.remove(content)

    def _read_target(self, target: str, kind: MemoryKind, error_class):
        try:
            response = self._read(target=target)
        except Exception as exc:
            raise error_class(
                "Subject public Memory read failed",
                target=target,
                subject_exception_type=type(exc).__name__,
            ) from exc
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise error_class(
                "Subject rejected public Memory read",
                target=target,
                subject_error_type=_safe_subject_error_type(response),
            )
        entries = response.get("entries")
        if not isinstance(entries, list) or not all(
            isinstance(item, str) and item.strip() for item in entries
        ):
            raise error_class(
                "Subject public Memory read returned an invalid shape",
                target=target,
            )
        return [self._native_item(target, kind, content) for content in entries]

    def _native_item(
        self,
        target: str,
        kind: MemoryKind,
        content: str,
    ) -> MemoryItem:
        fixture = self._fixture_by_target_content[target].get(content)
        if fixture is not None:
            return fixture.model_copy(
                update={
                    "metadata": {
                        **fixture.metadata,
                        "subject_target": target,
                        "identity_source": "fixture_exact_content",
                    }
                }
            )
        digest = hashlib.sha256(
            f"{target}\0{_normalized_content(content)}".encode("utf-8")
        ).hexdigest()
        return MemoryItem(
            memory_id=f"native-{target}-{digest}",
            kind=kind,
            content=content,
            source="myhermes_public_memory_api",
            metadata={
                "subject_target": target,
                "identity_source": "target_normalized_content_sha256",
            },
        )

    def _safe_mutate(self, action: str, *, target: str, content: str, error_class):
        try:
            response = self._mutate(
                action,
                target=target,
                content=content,
                old_text="",
            )
        except Exception as exc:
            raise error_class(
                "Subject public Memory mutation failed",
                target=target,
                operation=action,
                subject_exception_type=type(exc).__name__,
            ) from exc
        if not isinstance(response, dict):
            raise error_class(
                "Subject public Memory mutation returned an invalid shape",
                target=target,
                operation=action,
            )
        return response


def _target_for_kind(kind: MemoryKind) -> str:
    if kind is MemoryKind.LONG_TERM:
        return "memory"
    if kind is MemoryKind.USER_PROFILE:
        return "user"
    raise MemoryKindUnsupportedError(
        "Subject Adapter does not map the requested Memory kind",
        requested_kind=kind.value,
        supported_kinds=[
            MemoryKind.LONG_TERM.value,
            MemoryKind.USER_PROFILE.value,
        ],
    )


def _normalized_content(content: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", content).split())


def _duration_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _safe_subject_error_type(response: object) -> str:
    if not isinstance(response, dict):
        return "invalid_response"
    value = response.get("error_type")
    if not isinstance(value, str) or not value.strip():
        return "unspecified_subject_error"
    normalized = "".join(
        character if character.isalnum() or character in "._:-" else "_"
        for character in value.strip()
    )[:128]
    return normalized or "unspecified_subject_error"


__all__ = ("MyHermesMemoryAdapter",)

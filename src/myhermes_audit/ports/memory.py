"""未来 Memory Provider 适配器必须满足的异步端口。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from myhermes_audit.contracts.memory import (
    MemoryFixture,
    MemoryQuery,
    MemoryQueryResult,
    MemoryStateSnapshot,
)


@runtime_checkable
class MemoryEvaluationPort(Protocol):
    """不假设向量数据库、BM25 或任意具体持久化的评测端口。"""

    async def seed(self, fixture: MemoryFixture) -> None:
        """注入声明式 Memory Fixture。"""
        ...

    async def query(self, query: MemoryQuery) -> MemoryQueryResult:
        """执行 Provider 自身语义下的检索。"""
        ...

    async def snapshot(self) -> MemoryStateSnapshot:
        """获取算法无关状态快照。"""
        ...

    async def clear(self) -> None:
        """清理仅属于当前评测 Sandbox 的 Memory 状态。"""
        ...

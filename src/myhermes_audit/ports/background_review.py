"""未来 Background Review 适配器必须满足的异步端口。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from myhermes_audit.contracts.background_review import (
    ReviewKind,
    ReviewOutcome,
    ReviewRequest,
    ReviewStateSnapshot,
)
from myhermes_audit.contracts.common import Identifier


@runtime_checkable
class BackgroundReviewEvaluationPort(Protocol):
    """只定义评测交互形状，不执行 MyHermes Review。"""

    async def snapshot(self, kind: ReviewKind) -> ReviewStateSnapshot:
        """获取 Review 前后的算法无关状态。"""
        ...

    async def execute(self, request: ReviewRequest) -> Identifier:
        """提交 Review 请求并返回可关联的 review_id。"""
        ...

    async def collect_outcome(self, review_id: Identifier) -> ReviewOutcome:
        """收集结构化 Review 结果。"""
        ...

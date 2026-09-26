"""Evidence labels that keep shadow/proxy observations distinct from fills."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .models import EvidenceKind, EvidenceRecord


@dataclass
class EvidenceLedger:
    records: list[EvidenceRecord] = field(default_factory=list)
    max_records: int = 200
    _counts: Counter[str] = field(default_factory=Counter, init=False, repr=False)

    def add(
        self, kind: EvidenceKind, *, timestamp: float, source: str, **details: Any
    ) -> EvidenceRecord:
        if kind == EvidenceKind.REAL_EXECUTOR_FILL:
            order_id = next(
                (
                    details.get(key)
                    for key in ("order_id", "exchange_order_id", "client_order_id")
                    if details.get(key)
                ),
                None,
            )
            if order_id is None or not str(order_id).strip():
                raise ValueError("REAL_EXECUTOR_FILL requires a native order identifier")
        record = EvidenceRecord(kind=kind, timestamp=timestamp, source=source, details=details)
        self.records.append(record)
        self._counts[kind.value] += 1
        if len(self.records) > self.max_records:
            self.records.pop(0)
        return record

    @property
    def real_fill_count(self) -> int:
        return self._counts[EvidenceKind.REAL_EXECUTOR_FILL.value]

    @property
    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    @property
    def latest(self) -> EvidenceRecord | None:
        return self.records[-1] if self.records else None

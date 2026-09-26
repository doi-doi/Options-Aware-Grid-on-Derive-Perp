from derive_options_adaptive_grid.evidence import EvidenceLedger
from derive_options_adaptive_grid.models import EvidenceKind


def test_only_real_executor_fill_counts_as_fill():
    ledger = EvidenceLedger()
    ledger.add(EvidenceKind.PUBLIC_MARKET_DATA, timestamp=1, source="derive")
    ledger.add(EvidenceKind.SHADOW_PLAN, timestamp=2, source="controller")
    ledger.add(EvidenceKind.PROXY_TOUCH, timestamp=3, source="bbo")
    assert ledger.real_fill_count == 0
    ledger.add(EvidenceKind.REAL_EXECUTOR_FILL, timestamp=4, source="grid_executor", order_id="x")
    assert ledger.real_fill_count == 1


def test_ledger_is_bounded_but_fill_counts_are_cumulative():
    ledger = EvidenceLedger(max_records=2)
    ledger.add(EvidenceKind.WOULD_CREATE, timestamp=1, source="controller")
    ledger.add(
        EvidenceKind.REAL_EXECUTOR_FILL,
        timestamp=2,
        source="grid_executor",
        exchange_order_id="native-1",
    )
    ledger.add(EvidenceKind.WOULD_STOP, timestamp=3, source="controller")
    assert len(ledger.records) == 2
    assert ledger.real_fill_count == 1
    assert ledger.counts[EvidenceKind.WOULD_CREATE.value] == 1
    assert ledger.counts[EvidenceKind.WOULD_STOP.value] == 1


def test_real_fill_requires_a_native_order_identifier():
    ledger = EvidenceLedger()
    try:
        ledger.add(EvidenceKind.REAL_EXECUTOR_FILL, timestamp=1, source="grid_executor")
    except ValueError as exc:
        assert "order identifier" in str(exc)
    else:  # pragma: no cover - assertion keeps the invariant explicit
        raise AssertionError("real-fill evidence without an order ID was accepted")

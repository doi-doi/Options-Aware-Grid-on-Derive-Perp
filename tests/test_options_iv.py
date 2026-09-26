from derive_options_adaptive_grid.options_iv import (
    DeriveOptionsProvider,
    build_options_snapshot,
    parse_active_option_contracts,
    select_atm_strike,
    select_expiry,
)


def _rows(now: float):
    expiry = now + 7 * 86_400
    return [
        {
            "instrument_type": "option",
            "is_active": True,
            "instrument_name": "SOL-OPT-C",
            "option_details": {"expiry": expiry, "strike": 100, "option_type": "C"},
        },
        {
            "instrument_type": "option",
            "is_active": True,
            "instrument_name": "SOL-OPT-P",
            "option_details": {"expiry": expiry, "strike": 100, "option_type": "P"},
        },
        {
            "instrument_type": "option",
            "is_active": True,
            "instrument_name": "SOL-OPT-FAR",
            "option_details": {"expiry": expiry, "strike": 150, "option_type": "C"},
        },
    ]


def test_selects_sol_atm_and_uses_mark_iv():
    now = 1_700_000_000.0
    contracts = parse_active_option_contracts(_rows(now), underlying="SOL", now=now)
    expiry, rows = select_expiry(
        contracts, now=now, min_days_to_expiry=2, target_days_to_expiry=7, max_days_to_expiry=14
    )
    selection = select_atm_strike(
        rows, expiry_timestamp=expiry, reference_price=99.0, max_atm_distance_pct=0.05
    )
    snapshot = build_options_snapshot(
        selection,
        {
            "SOL-OPT-C": {"t": now - 1, "option_pricing": {"i": 0.72}},
            "SOL-OPT-P": {"t": now - 1, "option_pricing": {"bi": 0.68, "ai": 0.70}},
        },
        reference_price=99.0,
        now=now,
    )
    assert snapshot.underlying == "SOL"
    assert snapshot.atm_iv == 0.705
    assert snapshot.call_iv_source == "mark_iv"
    assert snapshot.put_iv_source == "bid_ask_iv_midpoint"
    assert snapshot.call_strike == snapshot.put_strike == snapshot.atm_strike == 100
    assert snapshot.reference_price == 99.0
    assert snapshot.data_available is True


def test_future_and_stale_tickers_are_not_used():
    now = 1_700_000_000.0
    contracts = parse_active_option_contracts(_rows(now), underlying="SOL", now=now)
    expiry, rows = select_expiry(
        contracts, now=now, min_days_to_expiry=2, target_days_to_expiry=7, max_days_to_expiry=14
    )
    selection = select_atm_strike(
        rows, expiry_timestamp=expiry, reference_price=100, max_atm_distance_pct=0.05
    )
    future = build_options_snapshot(
        selection,
        {
            "SOL-OPT-C": {"t": now + 5, "option_pricing": {"i": 0.7}},
            "SOL-OPT-P": {"t": now + 5, "option_pricing": {"i": 0.7}},
        },
        reference_price=100,
        now=now,
        future_tolerance_seconds=0.1,
    )
    stale = build_options_snapshot(
        selection,
        {
            "SOL-OPT-C": {"t": now - 30, "option_pricing": {"i": 0.7}},
            "SOL-OPT-P": {"t": now - 30, "option_pricing": {"i": 0.7}},
        },
        reference_price=100,
        now=now,
        max_option_data_age_seconds=10,
    )
    assert future.data_available is False
    assert any("future" in error for error in future.errors)
    assert stale.data_available is False
    assert any("stale" in error for error in stale.errors)


def test_no_btc_default_or_option_type_leakage():
    now = 1_700_000_000.0
    contracts = parse_active_option_contracts(_rows(now), now=now)
    assert {item.underlying for item in contracts} == {"SOL"}
    assert all(item.option_type in {"C", "P"} for item in contracts)


def test_non_sol_rows_and_non_sol_provider_are_rejected():
    now = 1_700_000_000.0
    btc_row = {
        "instrument_type": "option",
        "is_active": True,
        "instrument_name": "BTC-OPT-C",
        "currency": "BTC",
        "option_details": {
            "expiry": now + 7 * 86_400,
            "strike": 100,
            "option_type": "C",
        },
    }
    assert parse_active_option_contracts([btc_row], underlying="SOL", now=now) == []
    try:
        DeriveOptionsProvider(currency="BTC")
    except ValueError as exc:
        assert "SOL-only" in str(exc)
    else:
        raise AssertionError("non-SOL provider was accepted")


def test_nearest_unpaired_strike_does_not_beat_paired_strike():
    now = 1_700_000_000.0
    expiry = now + 7 * 86_400
    rows = _rows(now) + [
        {
            "instrument_type": "option",
            "is_active": True,
            "instrument_name": "SOL-OPT-UNPAIRED-C",
            "option_details": {
                "expiry": expiry,
                "strike": 99,
                "option_type": "C",
            },
        }
    ]
    contracts = parse_active_option_contracts(rows, underlying="SOL", now=now)
    _, eligible = select_expiry(
        contracts,
        now=now,
        min_days_to_expiry=2,
        target_days_to_expiry=7,
        max_days_to_expiry=14,
    )
    selection = select_atm_strike(
        eligible,
        expiry_timestamp=expiry,
        reference_price=99,
        max_atm_distance_pct=0.05,
    )
    assert selection.atm_strike == 100


def test_one_missing_atm_side_is_unavailable():
    now = 1_700_000_000.0
    contracts = parse_active_option_contracts(_rows(now), underlying="SOL", now=now)
    expiry, rows = select_expiry(
        contracts,
        now=now,
        min_days_to_expiry=2,
        target_days_to_expiry=7,
        max_days_to_expiry=14,
    )
    selection = select_atm_strike(
        rows,
        expiry_timestamp=expiry,
        reference_price=100,
        max_atm_distance_pct=0.05,
    )
    snapshot = build_options_snapshot(
        selection,
        {"SOL-OPT-C": {"t": now - 1, "option_pricing": {"i": 0.7}}},
        reference_price=100,
        now=now,
    )
    assert snapshot.data_available is False
    assert "ticker missing for SOL-OPT-P" in snapshot.errors


def test_provider_keeps_source_receipt_and_decision_causal_after_delay():
    now = 1_700_000_000.0
    clock_values = iter((now + 1, now + 2))
    provider = DeriveOptionsProvider(clock=lambda: next(clock_values))

    def fake_post(method, _params):
        if method == "public/get_instruments":
            return _rows(now)
        return {
            "tickers": {
                "SOL-OPT-C": {"t": now, "option_pricing": {"i": 0.7}},
                "SOL-OPT-P": {"t": now, "option_pricing": {"i": 0.7}},
            }
        }

    provider._post = fake_post
    snapshot = provider._snapshot_sync(100.0, now)
    assert snapshot.source_timestamp == now
    assert snapshot.received_timestamp == now + 1
    assert snapshot.decision_timestamp == now + 2
    assert snapshot.source_timestamp <= snapshot.received_timestamp <= snapshot.decision_timestamp

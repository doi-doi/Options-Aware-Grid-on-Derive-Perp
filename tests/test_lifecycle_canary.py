from decimal import Decimal

from derive_options_adaptive_grid.connector_contract import OrderBookFreshnessTracker
from derive_options_adaptive_grid.lifecycle_canary import (
    CanaryMode,
    CanaryStage,
    build_dry_run_result,
    build_stage_a_plan,
    build_stage_b_plan,
    evaluate_authorization,
    minimum_safe_amount,
    passive_price,
    run_stage_a,
    run_stage_b,
    validate_account_clean,
    validate_cleanup,
    validate_maker_price,
    validate_market_freshness,
    validate_position_transition,
    validate_preflight_runtime_binding,
    validate_stage_a_result,
    validate_stage_b_result,
    validate_static_contract,
)
from derive_options_adaptive_grid.lifecycle_evidence import EXPECTED_STATIC_CONTRACT


def _snapshot():
    runtime = {
        "base_image_digest": "sha256:image",
        "runtime_contract_id": "sha256:runtime",
        "connector_source_sha256": "sha256:connector",
        "fee_source_sha256": "sha256:fees",
        "runtime_instance_id": "container-1",
        "runtime_image_ref": "local/derive-options-grid-hummingbot:connector-v2",
    }
    binding = {
        "runtime_instance_id": runtime["runtime_instance_id"],
        "runtime_contract_id": runtime["runtime_contract_id"],
        "runtime_image_ref": runtime["runtime_image_ref"],
    }
    return {
        "identity": {
            "environment": "mainnet",
            "connector_name": "derive_perpetual",
            "trading_pair": "SOL-USDC",
            "exchange_instrument": "SOL-PERP",
            "position_mode": "ONEWAY",
            "leverage": 1,
        },
        "runtime": runtime,
        "expected_runtime": runtime,
        "static_contract": {**EXPECTED_STATIC_CONTRACT, **binding},
        "account": {
            "account_fingerprint": "sha256:account",
            "position_base": "0",
            "active_orders": 0,
            "managed_executors": 0,
            "unmanaged_executors": 0,
            **binding,
        },
        "bbo": {
            "best_bid": "100",
            "best_ask": "101",
            "observed_at": 100,
            "snapshot_uid": 1,
            "last_update_id": 1,
            "order_book_marker": (1, 1),
            **binding,
        },
        "rules": {
            "min_base_amount": "0.1",
            "amount_increment": "0.1",
            "min_notional": "5",
            "price_increment": "0.1",
            **binding,
        },
        "max_bbo_age_seconds": 10,
    }


def test_dry_run_calculates_both_stages_without_authorizing_mutation():
    result = build_dry_run_result(_snapshot(), now=101)
    assert result.preflight_blockers == ()
    assert result.stage_a_plan.valid is True
    assert result.stage_a_plan.position_action == "OPEN"
    assert result.stage_a_plan.reduce_only is False
    assert result.stage_b_plan.position_action == "CLOSE"
    assert result.stage_b_plan.reduce_only is True
    assert result.authorization.authorized is False
    assert result.authorization.mutation_attempted is False


def test_stage_authorization_is_separate_and_stage_b_needs_stage_a_pass():
    denied = evaluate_authorization(
        CanaryMode.STAGE_B,
        CanaryStage.STAGE_B,
        cli_authorized=True,
        runtime_stage_authorization="STAGE_B",
        one_run_token="token",
        expected_one_run_token="token",
        runtime_valid=True,
        stage_a_passed=False,
    )
    assert denied.authorized is False
    assert "stage_a_reviewed_pass_required" in denied.blockers

    allowed = evaluate_authorization(
        CanaryMode.STAGE_A,
        CanaryStage.STAGE_A,
        cli_authorized=True,
        runtime_stage_authorization="STAGE_A",
        one_run_token="token",
        expected_one_run_token="token",
        runtime_valid=True,
    )
    assert allowed.authorized is True
    assert allowed.mutation_attempted is False


def test_runtime_account_bbo_and_static_gates_fail_closed():
    assert validate_static_contract({"open_reduce_only": False})
    assert validate_account_clean({"account_fingerprint": "x", "position_base": "1"})
    assert validate_market_freshness(
        {
            "best_bid": "100",
            "best_ask": "101",
            "observed_at": 0,
            "order_book_marker": (1, 1),
        },
        now=20,
        max_age_seconds=10,
    ) == ("bbo_stale",)


def test_order_book_freshness_uses_update_marker_not_read_time():
    tracker = OrderBookFreshnessTracker()
    first_timestamp, first_changed = tracker.observe((10, 20), now=100)
    duplicate_timestamp, duplicate_changed = tracker.observe((10, 20), now=105)
    new_timestamp, new_changed = tracker.observe((10, 21), now=106)
    assert (first_timestamp, first_changed) == (100, True)
    assert (duplicate_timestamp, duplicate_changed) == (100, False)
    assert (new_timestamp, new_changed) == (106, True)


def test_market_freshness_requires_an_order_book_update_marker():
    blockers = validate_market_freshness(
        {"best_bid": "100", "best_ask": "101", "observed_at": 100},
        now=101,
        max_age_seconds=10,
    )
    assert blockers == ("bbo_update_marker_unavailable",)


def test_preflight_rejects_mixed_runtime_sections():
    snapshot = _snapshot()
    snapshot["bbo"] = {**snapshot["bbo"], "runtime_instance_id": "other-container"}
    blockers = validate_preflight_runtime_binding(snapshot)
    assert "BLOCKED_BY_PREFLIGHT_RUNTIME_INSTANCE_BINDING" in blockers
    assert "preflight_runtime_binding_mismatch:bbo:runtime_instance_id" in blockers


def test_amount_and_passive_price_use_decimal_exchange_rules():
    rules = {
        "min_base_amount": "0.1",
        "amount_increment": "0.1",
        "min_notional": "10",
    }
    assert minimum_safe_amount(rules, Decimal("100")) == Decimal("0.1")
    assert passive_price("BUY", Decimal("100"), Decimal("101"), Decimal("0.1")) == Decimal("100")
    assert passive_price("SELL", Decimal("100"), Decimal("101"), Decimal("0.1")) == Decimal("101")


def test_stage_a_plan_default_guard_is_conservative():
    snapshot = _snapshot()
    buy = build_stage_a_plan(side="BUY", bbo=snapshot["bbo"], rules=snapshot["rules"])
    sell = build_stage_a_plan(side="SELL", bbo=snapshot["bbo"], rules=snapshot["rules"])
    assert buy.price == Decimal("99.5")
    assert sell.price == Decimal("101.5")
    assert buy.maker_guard_ticks == sell.maker_guard_ticks == 5
    assert buy.maker_guard_bps == sell.maker_guard_bps == Decimal("15")


def test_stage_a_plan_can_use_a_legacy_one_tick_guard_when_explicit():
    snapshot = _snapshot()
    buy = build_stage_a_plan(
        side="BUY",
        bbo=snapshot["bbo"],
        rules=snapshot["rules"],
        ticks_behind_touch=1,
        maker_guard_bps=Decimal("0"),
    )
    sell = build_stage_a_plan(
        side="SELL",
        bbo=snapshot["bbo"],
        rules=snapshot["rules"],
        ticks_behind_touch=1,
        maker_guard_bps=Decimal("0"),
    )
    assert buy.price == Decimal("99.9")
    assert sell.price == Decimal("101.1")


def test_stage_a_plan_can_require_a_wider_maker_gap_and_records_it():
    snapshot = _snapshot()
    buy = build_stage_a_plan(
        side="BUY",
        bbo=snapshot["bbo"],
        rules=snapshot["rules"],
        ticks_behind_touch=3,
    )
    sell = build_stage_a_plan(
        side="SELL",
        bbo=snapshot["bbo"],
        rules=snapshot["rules"],
        ticks_behind_touch=3,
    )
    assert buy.price == Decimal("99.7")
    assert sell.price == Decimal("101.3")
    assert buy.maker_gap_ticks == sell.maker_gap_ticks == 3
    assert buy.blockers == sell.blockers == ()


def test_stage_a_maker_guard_uses_the_larger_tick_or_bps_distance():
    snapshot = _snapshot()
    snapshot["bbo"] = {
        **snapshot["bbo"],
        "best_bid": "100",
        "best_ask": "101",
    }
    snapshot["rules"] = {**snapshot["rules"], "price_increment": "0.01"}
    buy = build_stage_a_plan(
        side="BUY",
        bbo=snapshot["bbo"],
        rules=snapshot["rules"],
        maker_guard_ticks=5,
        maker_guard_bps=Decimal("15"),
    )
    sell = build_stage_a_plan(
        side="SELL",
        bbo=snapshot["bbo"],
        rules=snapshot["rules"],
        maker_guard_ticks=5,
        maker_guard_bps=Decimal("15"),
    )
    assert buy.price == Decimal("99.84")
    assert sell.price == Decimal("101.16")
    assert buy.maker_guard_ticks == sell.maker_guard_ticks == 5
    assert buy.maker_guard_bps == sell.maker_guard_bps == Decimal("15")


def test_maker_price_validator_rejects_crossing_or_under_gapped_quotes():
    assert validate_maker_price(
        "BUY",
        Decimal("100.0"),
        Decimal("100.0"),
        Decimal("100.1"),
        Decimal("0.1"),
        ticks_behind_touch=1,
    ) == ("maker_buy_gap_too_small",)
    assert validate_maker_price(
        "BUY",
        Decimal("100.1"),
        Decimal("100.0"),
        Decimal("100.1"),
        Decimal("0.1"),
        ticks_behind_touch=1,
    ) == ("maker_buy_would_cross_ask", "maker_buy_gap_too_small")
    assert validate_maker_price(
        "SELL",
        Decimal("101.0"),
        Decimal("100.0"),
        Decimal("101.0"),
        Decimal("0.1"),
        ticks_behind_touch=1,
    ) == ("maker_sell_gap_too_small",)


def test_stage_a_requires_cancel_and_no_fill():
    result = {
        "status": "PASS",
        "position_action": "OPEN",
        "reduce_only": False,
        "order_type": "LIMIT_MAKER",
        "time_in_force": "post_only",
        "native_order_id": "native-a",
        "fill_count": 1,
        "cancel_acknowledged": False,
        "active_orders": 1,
        "position_after": "1",
    }
    blockers = validate_stage_a_result(result)
    assert "stage_a_unexpected_fill" in blockers
    assert "stage_a_cancel_not_acknowledged" in blockers
    assert "stage_a_position_not_flat" in blockers


def test_close_must_reduce_without_increase_or_sign_flip():
    assert validate_position_transition("2", "1", action="CLOSE", reduce_only=True) == ()
    assert "ABORT_POSITION_INCREASED_DURING_CLOSE" in validate_position_transition(
        "1", "2", action="CLOSE", reduce_only=True
    )
    assert "ABORT_POSITION_SIGN_FLIP" in validate_position_transition(
        "1", "-0.1", action="CLOSE", reduce_only=True
    )
    assert "ABORT_REDUCE_ONLY_OPENED_EXPOSURE" in validate_position_transition(
        "0", "1", action="CLOSE", reduce_only=True
    )


def test_stage_b_and_cleanup_require_flat_final_state():
    stage_b = {
        "status": "PASS",
        "position_action": "CLOSE",
        "reduce_only": True,
        "order_type": "LIMIT_MAKER",
        "time_in_force": "post_only",
        "position_before": "1",
        "position_after": "0.1",
    }
    assert "stage_b_position_not_flat" in validate_stage_b_result(stage_b)
    assert validate_cleanup(
        {
            "position_zero": False,
            "active_orders": 0,
            "managed_executors": 0,
            "unmanaged_executors": 1,
        }
    )[0] == "MAINNET_LIFECYCLE_CLEANUP_FAILED"


def test_stage_b_accepts_a_bounded_gtc_reduce_only_close():
    result = {
        "status": "PASS",
        "position_action": "CLOSE",
        "reduce_only": True,
        "order_type": "LIMIT",
        "time_in_force": "gtc",
        "close_reduce_only_requested": True,
        "close_native_order_id": "native-b",
        "fill_count": 1,
        "position_before": "0.1",
        "position_after": "0",
    }
    assert validate_stage_b_result(result) == ()


class _StubLifecycleAdapter:
    def __init__(self, accounts, *, stage_a_observation=None, close_observation=None):
        self.accounts = list(accounts)
        self.stage_a_observation = stage_a_observation or {
            "status": "RESTING",
            "resting_verified": True,
            "fill_count": 0,
        }
        self.close_observation = close_observation or {
            "status": "FILLED",
            "fill_count": 1,
        }
        self.submissions = []
        self.cancels = []

    def submit_open(self, trading_pair, plan):
        self.submissions.append(("open", trading_pair, plan))
        return "native-open"

    def submit_close(self, trading_pair, plan):
        self.submissions.append(("close", trading_pair, plan))
        return "native-close"

    def cancel(self, trading_pair, client_order_id):
        self.cancels.append((trading_pair, client_order_id))
        return {"acknowledged": True}

    def wait_for_order(self, trading_pair, client_order_id, timeout_seconds):
        if client_order_id == "native-open" and len(self.submissions) == 1:
            return dict(self.stage_a_observation)
        return dict(self.close_observation)

    def read_account(self, trading_pair):
        if len(self.accounts) > 1:
            return dict(self.accounts.pop(0))
        return dict(self.accounts[0])


def _authorized(stage, *, stage_a_passed=False):
    return evaluate_authorization(
        stage,
        stage,
        cli_authorized=True,
        runtime_stage_authorization=stage.value,
        one_run_token="one-run",
        expected_one_run_token="one-run",
        runtime_valid=True,
        stage_a_passed=stage_a_passed,
    )


def _clean_account(position="0", active_orders=0, unmanaged=0):
    return {
        "account_fingerprint": "sha256:account",
        "position_base": position,
        "active_orders": active_orders,
        "managed_executors": 0,
        "unmanaged_executors": unmanaged,
    }


def test_stage_a_resting_order_is_cancelled_without_stage_b_chaining():
    adapter = _StubLifecycleAdapter([_clean_account(active_orders=1), _clean_account()])
    snapshot = _snapshot()
    plan = build_stage_a_plan(side="BUY", bbo=snapshot["bbo"], rules=snapshot["rules"])
    result = run_stage_a(
        adapter,
        trading_pair="SOL-USDC",
        plan=plan,
        preflight={"preflight_blockers": ()},
        authorization=_authorized(CanaryStage.STAGE_A),
    )
    assert result.status == "PASS"
    assert result.blockers == ()
    assert result.orders_submitted == 1
    assert result.orders_cancelled == 1
    assert adapter.cancels == [("SOL-USDC", "native-open")]


def test_stage_a_unexpected_fill_aborts_without_stage_b_submission():
    adapter = _StubLifecycleAdapter(
        [_clean_account(position="0.1"), _clean_account(position="0.1")],
        stage_a_observation={"status": "FILLED", "fill_count": 1},
    )
    snapshot = _snapshot()
    plan = build_stage_a_plan(side="BUY", bbo=snapshot["bbo"], rules=snapshot["rules"])
    result = run_stage_a(
        adapter,
        trading_pair="SOL-USDC",
        plan=plan,
        preflight={"preflight_blockers": ()},
        authorization=_authorized(CanaryStage.STAGE_A),
    )
    assert result.status == "FAIL"
    assert "ABORT_STAGE_A_UNEXPECTED_FILL" in result.blockers
    assert [item[0] for item in adapter.submissions] == ["open"]
    assert adapter.cancels == [("SOL-USDC", "native-open")]
    assert "MAINNET_LIFECYCLE_CLEANUP_REQUIRED" in result.blockers
    assert result.observations["cleanup_required"] is True


def test_stage_b_close_reduces_position_and_finishes_clean():
    adapter = _StubLifecycleAdapter(
        [_clean_account(), _clean_account(position="0.1"), _clean_account(), _clean_account()],
        stage_a_observation={"status": "FILLED", "fill_count": 1},
    )
    snapshot = _snapshot()
    open_plan = build_stage_a_plan(side="BUY", bbo=snapshot["bbo"], rules=snapshot["rules"])
    close_plan = build_stage_b_plan(side="BUY", bbo=snapshot["bbo"], rules=snapshot["rules"])
    result = run_stage_b(
        adapter,
        trading_pair="SOL-USDC",
        open_plan=open_plan,
        close_plan=close_plan,
        preflight={"preflight_blockers": ()},
        authorization=_authorized(CanaryStage.STAGE_B, stage_a_passed=True),
    )
    assert result.status == "PASS"
    assert result.blockers == ()
    assert result.orders_submitted == 2
    assert adapter.submissions[1][2].reduce_only is True


def test_stage_b_position_growth_and_flip_abort():
    snapshot = _snapshot()
    open_plan = build_stage_a_plan(side="BUY", bbo=snapshot["bbo"], rules=snapshot["rules"])
    close_plan = build_stage_b_plan(side="BUY", bbo=snapshot["bbo"], rules=snapshot["rules"])
    for final_position, expected in (
        ("0.2", "ABORT_POSITION_INCREASED_DURING_CLOSE"),
        ("-0.1", "ABORT_POSITION_SIGN_FLIP"),
    ):
        adapter = _StubLifecycleAdapter(
            [
                _clean_account(),
                _clean_account(position="0.1"),
                _clean_account(position=final_position),
                _clean_account(position=final_position),
            ],
            stage_a_observation={"status": "FILLED", "fill_count": 1},
        )
        result = run_stage_b(
            adapter,
            trading_pair="SOL-USDC",
            open_plan=open_plan,
            close_plan=close_plan,
            preflight={"preflight_blockers": ()},
            authorization=_authorized(CanaryStage.STAGE_B, stage_a_passed=True),
        )
        assert expected in result.blockers
        assert result.status == "FAIL"


def test_stage_b_cleanup_failure_is_explicit():
    snapshot = _snapshot()
    open_plan = build_stage_a_plan(side="BUY", bbo=snapshot["bbo"], rules=snapshot["rules"])
    close_plan = build_stage_b_plan(side="BUY", bbo=snapshot["bbo"], rules=snapshot["rules"])
    adapter = _StubLifecycleAdapter(
        [
            _clean_account(),
            _clean_account(position="0.1"),
            _clean_account(unmanaged=1),
            _clean_account(unmanaged=1),
        ],
        stage_a_observation={"status": "FILLED", "fill_count": 1},
    )
    result = run_stage_b(
        adapter,
        trading_pair="SOL-USDC",
        open_plan=open_plan,
        close_plan=close_plan,
        preflight={"preflight_blockers": ()},
        authorization=_authorized(CanaryStage.STAGE_B, stage_a_passed=True),
    )
    assert "MAINNET_LIFECYCLE_CLEANUP_FAILED" in result.blockers


def test_authorization_and_dirty_preflight_make_no_adapter_call():
    adapter = _StubLifecycleAdapter([_clean_account()])
    snapshot = _snapshot()
    plan = build_stage_a_plan(side="BUY", bbo=snapshot["bbo"], rules=snapshot["rules"])
    denied = run_stage_a(
        adapter,
        trading_pair="SOL-USDC",
        plan=plan,
        preflight={"preflight_blockers": ()},
        authorization=evaluate_authorization(CanaryMode.STAGE_A, CanaryStage.STAGE_A),
    )
    dirty = run_stage_a(
        adapter,
        trading_pair="SOL-USDC",
        plan=plan,
        preflight={"preflight_blockers": ("account_position_nonzero",)},
        authorization=_authorized(CanaryStage.STAGE_A),
    )
    assert denied.mutation_attempted is False
    assert dirty.mutation_attempted is False
    assert adapter.submissions == []

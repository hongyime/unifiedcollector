from src.core.telegram_throughput import AdaptiveTelegramHistoryScheduler


def test_history_scheduler_starts_with_one_claim_then_earns_more():
    scheduler = AdaptiveTelegramHistoryScheduler(clean_windows_to_raise=2)
    assert scheduler.claim_budget("one") == 1
    scheduler.claimed("one")
    scheduler.completed("one", written=10, elapsed_seconds=2)
    scheduler.claimed("one")
    state = scheduler.completed("one", written=10, elapsed_seconds=2)

    assert state.claim_budget == 2
    assert scheduler.snapshot("one")["write_rate"] == 5.0


def test_flood_wait_only_resets_the_affected_account():
    scheduler = AdaptiveTelegramHistoryScheduler(clean_windows_to_raise=1)
    scheduler.completed("one", written=1, elapsed_seconds=1)
    scheduler.completed("two", written=1, elapsed_seconds=1)
    scheduler.flood_wait("one", 90)

    assert scheduler.claim_budget("one") == 1
    assert scheduler.claim_budget("two") == 2


def test_scheduler_reports_writes_while_a_history_claim_is_active():
    scheduler = AdaptiveTelegramHistoryScheduler()
    scheduler.claimed("one")
    scheduler.wrote("one", 3)
    scheduler.state("one").started_at -= 4

    snapshot = scheduler.snapshot("one")
    assert snapshot["active_claims"] == 1
    assert snapshot["written_messages"] == 3
    assert 0.7 < snapshot["write_rate"] < 0.8

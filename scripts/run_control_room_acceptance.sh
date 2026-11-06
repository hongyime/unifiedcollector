#!/usr/bin/env bash
# Stable completion gate for control-room and source-control changes.
set -euo pipefail

cd "$(dirname "$0")/.."
.venv/bin/python -m compileall -q src/core src/worker src/collectors/profile_only_runner.py src/notifications/collector_bot.py
.venv/bin/pytest -q \
  tests/core/test_handoff_freshness.py \
  tests/cli/test_handoff_freshness_command.py \
  tests/core/test_source_controls.py \
  tests/core/test_source_states.py \
  tests/core/test_schedule_status.py \
  tests/core/test_telegram_account_controls.py \
  tests/core/test_target_status.py \
  tests/test_profile_only_runner_startup_delay.py \
  tests/notifications/test_collector_bot.py \
  tests/collectors/test_beeper.py \
  tests/collectors/test_whatsapp.py \
  tests/test_worker_target_priority_refresh.py

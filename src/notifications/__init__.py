"""Fail-safe Telegram notifications (send-only).

A Telegram outage must NEVER break collection: every send is best-effort and
returns a bool instead of raising. See telegram.send() and alerts.*.
"""

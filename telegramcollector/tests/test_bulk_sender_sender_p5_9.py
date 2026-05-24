# Feature: bulk-sender-service, Property 10: Collector Query Default Message Type
"""
Property 10: Collector Query Default Message Type
Validates: Requirements 7.5

For any collector_query dict that does not contain a message_type key (or
contains message_type = None), _build_collector_query() SHALL produce SQL
that filters message_type = 'photo'.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from services.bulk_sender.sender import Sender


def make_sender() -> Sender:
    """Construct a minimal Sender instance without real dependencies."""
    sender = Sender.__new__(Sender)
    sender.job_manager = None
    sender.send_delay = 1.0
    sender.max_retries = 3
    sender.sessions_path = "/tmp"
    sender.bot_tokens = []
    return sender


optional_message_type = st.one_of(
    st.none(),
    st.just("photo"),
    st.just("video"),
    st.just("document"),
)


@given(
    st.fixed_dictionaries({
        "message_type": optional_message_type,
    })
)
@settings(max_examples=100)
def test_collector_query_default_message_type_with_key(collector_query):
    """When message_type key is present (None or a value), SQL and params are correct."""
    # Feature: bulk-sender-service, Property 10: Collector Query Default Message Type
    sender = make_sender()
    sql, params = sender._build_collector_query(collector_query)

    expected_type = collector_query["message_type"] or "photo"

    assert "message_type = %s" in sql
    assert expected_type in params


@given(st.just({}))
@settings(max_examples=100)
def test_collector_query_default_message_type_without_key(collector_query):
    """When message_type key is absent entirely, SQL defaults to 'photo'."""
    # Feature: bulk-sender-service, Property 10: Collector Query Default Message Type
    sender = make_sender()
    sql, params = sender._build_collector_query(collector_query)

    assert "message_type = %s" in sql
    assert "photo" in params

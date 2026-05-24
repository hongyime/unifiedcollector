# Bugfix Requirements Document

## Introduction

The user analyzer feature has two efficiency issues that reduce its effectiveness and increase API load:

1. **Incomplete username extraction**: The system only extracts @username patterns but misses t.me/username and https://t.me/username links in message text, causing valid user references to be ignored.

2. **Redundant API calls across accounts**: When multiple accounts scan the same groups, each account makes separate get_entity() API calls to Telegram for the same username because the entity cache is per-processor instance (in-memory only) and not shared across accounts.

These issues result in missed user discoveries and unnecessary API load that could trigger rate limiting.

## Bug Analysis

### Current Behavior (Defect)

**Issue 1: Missing t.me/username extraction**

1.1 WHEN a message contains "t.me/username" or "https://t.me/username" links THEN the system ignores these valid user references

1.2 WHEN the regex pattern `r'(?<![\w@])@([A-Za-z0-9_]{5,32})'` is applied to message text THEN only @username patterns are extracted

**Issue 2: Redundant API calls across accounts**

1.3 WHEN multiple accounts (e.g., 4 accounts) scan the same groups and encounter the same username THEN each account makes separate get_entity() API calls to Telegram

1.4 WHEN successful entity resolutions occur THEN they are stored in per-instance cache `self._entity_cache: Dict[str, Any] = {}` and not shared across accounts

1.5 WHEN entity lookups fail THEN they ARE correctly shared via the database failed_lookups table

### Expected Behavior (Correct)

**Issue 1: Complete username extraction**

2.1 WHEN a message contains "t.me/username" links THEN the system SHALL extract the username and process it

2.2 WHEN a message contains "https://t.me/username" links THEN the system SHALL extract the username and process it

2.3 WHEN the username extraction regex is applied THEN it SHALL capture @username, t.me/username, and https://t.me/username patterns

**Issue 2: Shared entity cache across accounts**

2.4 WHEN an account successfully resolves a username via get_entity() THEN the system SHALL cache the result in a shared storage accessible to all accounts

2.5 WHEN another account encounters the same username THEN the system SHALL retrieve the cached entity without making a redundant API call

2.6 WHEN checking for cached entities THEN the system SHALL consult the shared cache before calling get_entity()

### Unchanged Behavior (Regression Prevention)

3.1 WHEN entity lookups fail THEN the system SHALL CONTINUE TO cache failures in the database failed_lookups table

3.2 WHEN @username patterns appear in message text THEN the system SHALL CONTINUE TO extract and process them correctly

3.3 WHEN processing messages from multiple sources (mentions, forwards, replies, actions) THEN the system SHALL CONTINUE TO extract users from all configured sources

3.4 WHEN the user analyzer runs THEN it SHALL CONTINUE TO store user information and memberships in the database

3.5 WHEN non-fatal errors occur during extraction THEN the system SHALL CONTINUE TO log warnings and continue processing without aborting the scan

3.6 WHEN the entity cache is consulted THEN the system SHALL CONTINUE TO use cache keys based on normalized references

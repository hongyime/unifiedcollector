# User Analyzer Efficiency Fix Design


## Overview

This bugfix addresses two efficiency issues in the user analyzer processor:

1. **Missing t.me/username extraction**: The system only extracts @username patterns but misses t.me/username and https://t.me/username links in message text
2. **Redundant API calls across accounts**: Each account makes separate get_entity() API calls for the same username because the entity cache is per-instance (in-memory only)

These issues result in missed user discoveries and unnecessary API load that could trigger rate limiting.

## Glossary

- **Bug_Condition_1 (C1)**: Message text contains t.me/username or https://t.me/username patterns that are not extracted
- **Bug_Condition_2 (C2)**: Multiple accounts resolve the same username via separate API calls instead of using shared cache
- **Property_1 (P1)**: All username patterns (@username, t.me/username, https://t.me/username) are extracted from message text
- **Property_2 (P2)**: Entity resolutions are cached in shared storage and reused across accounts
- **Preservation**: Existing @username extraction, failed lookup caching, and all other user extraction sources remain unchanged
- **_extract_raw_usernames**: Method in `UserAnalyzerProcessor` that extracts @username patterns using regex
- **_resolve_reference**: Method that calls `client.get_entity()` to resolve username/ID to full entity object
- **_entity_cache**: Per-instance dictionary `Dict[str, Any]` that caches resolved entities in memory only
- **failed_lookups**: Database table that caches failed entity lookups (already shared across accounts)
- **StateManager**: Singleton class in `toolkit/core/state_manager.py` that manages SQLite database operations

## Bug Details

### Bug Condition 1: Missing t.me/username Extraction

The system fails to extract valid user references from t.me links in message text. The `_extract_raw_usernames` method only matches @username patterns.

**Formal Specification:**
```
FUNCTION isBugCondition1(message)
  INPUT: message of type TelegramMessage
  OUTPUT: boolean
  
  text := _get_message_text(message)
  
  RETURN (text CONTAINS "t.me/" OR text CONTAINS "https://t.me/")
         AND extractedUsernames := _extract_raw_usernames(text)
         AND NOT (all t.me usernames IN extractedUsernames)
END FUNCTION
```

### Bug Condition 2: Redundant API Calls Across Accounts

When multiple accounts scan the same groups, each account makes separate `get_entity()` API calls for the same username because successful resolutions are stored in per-instance `self._entity_cache` (memory only) instead of shared storage.

**Formal Specification:**
```
FUNCTION isBugCondition2(accounts, username)
  INPUT: accounts of type List[Account], username of type string
  OUTPUT: boolean
  
  apiCallCount := 0
  FOR EACH account IN accounts DO
    IF account.resolves(username) THEN
      apiCallCount := apiCallCount + 1
    END IF
  END FOR
  
  RETURN apiCallCount > 1
         AND username was successfully resolved
         AND NOT (resolution cached in shared storage)
END FUNCTION
```

### Examples

**Bug 1 Examples:**
- Message contains "Check out t.me/username" → username is NOT extracted (bug)
- Message contains "Visit https://t.me/username" → username is NOT extracted (bug)
- Message contains "@username" → username IS extracted (works correctly)
- Message contains "t.me/ab" → correctly ignored (username too short, < 5 chars)

**Bug 2 Examples:**
- Account1 resolves @username → makes API call, caches in memory
- Account2 resolves @username → makes ANOTHER API call (bug), should reuse cached result
- Account1 fails to resolve @baduser → cached in database failed_lookups table
- Account2 encounters @baduser → skips API call (works correctly, uses shared failed_lookups)

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- @username pattern extraction must continue to work exactly as before
- Failed lookup caching in database failed_lookups table must remain unchanged
- All other user extraction sources (mentions, forwards, replies, actions, participants) must remain unchanged
- Non-fatal error handling and logging must remain unchanged
- Entity cache key generation via `_make_cache_key` must remain unchanged
- Cross-account fallback in `retry_api_call` must remain unchanged

**Scope:**
All inputs that do NOT involve t.me/username links or repeated username resolutions across accounts should be completely unaffected by this fix. This includes:
- @username mentions (existing functionality)
- User ID resolutions
- Forward sender resolutions
- Reply sender resolutions
- Action user resolutions
- Participant collection
- Failed lookup handling

## Hypothesized Root Cause

Based on the bug description and code analysis, the root causes are:

1. **Incomplete Regex Pattern**: The `_extract_raw_usernames` method uses regex `r'(?<![\w@])@([A-Za-z0-9_]{5,32})'` which only matches @username patterns
   - Does not match t.me/username format
   - Does not match https://t.me/username format
   - The regex requires @ prefix, excluding t.me links

2. **Per-Instance Entity Cache**: The `_entity_cache` is initialized as `self._entity_cache: Dict[str, Any] = {}` in `__init__`
   - Each processor instance has its own cache
   - Cache is in-memory only, not persisted
   - Not shared across accounts or processor instances
   - Failed lookups ARE correctly shared via database, but successful resolutions are not

3. **No Shared Entity Storage**: Unlike failed_lookups table, there is no database table for successful entity resolutions
   - StateManager has tables for users, memberships, failed_lookups
   - No entity_cache table exists for sharing resolved entities

4. **Cache Cleared on Restart**: Since cache is in-memory, it's lost when processor restarts
   - No persistence mechanism for successful resolutions
   - Repeated scans make redundant API calls

## Correctness Properties

Property 1: Bug Condition 1 - Complete Username Extraction

_For any_ message text where t.me/username or https://t.me/username links appear with valid usernames (5-32 alphanumeric/underscore characters), the fixed `_extract_raw_usernames` function SHALL extract those usernames and return them in the set of extracted usernames, enabling them to be processed through `_collect_reference` just like @username mentions.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Bug Condition 2 - Shared Entity Cache

_For any_ username that has been successfully resolved by any account via `get_entity()`, subsequent resolution attempts by any account SHALL retrieve the cached entity from shared storage without making redundant API calls to Telegram, reducing API load and avoiding rate limiting.

**Validates: Requirements 2.4, 2.5, 2.6**

Property 3: Preservation - Existing Extraction Behavior

_For any_ message that does NOT contain t.me/username links, and for any entity resolution that is NOT a repeated username lookup, the fixed code SHALL produce exactly the same behavior as the original code, preserving all existing functionality for @username extraction, failed lookup caching, and all other user extraction sources.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `toolkit/managers/processors/user_analyzer_processor.py`

**Function**: `_extract_raw_usernames`

**Specific Changes**:
1. **Expand Regex Pattern**: Update the regex to capture t.me/username and https://t.me/username patterns
   - Add pattern for `t.me/username` format
   - Add pattern for `https://t.me/username` format
   - Maintain existing `@username` pattern
   - Combine patterns with OR logic
   - Ensure username length validation (5-32 chars) applies to all patterns

2. **Extract Usernames from All Patterns**: Process matches from all three patterns
   - Normalize extracted usernames (strip @ prefix if present)
   - Return unified set of usernames from all sources

**File**: `toolkit/core/state_manager.py`

**Changes**:
3. **Add Entity Cache Table**: Create new database table for shared entity cache
   - Table name: `entity_cache`
   - Columns: `cache_key TEXT PRIMARY KEY`, `entity_data TEXT`, `entity_type TEXT`, `cached_at TIMESTAMP`
   - Store serialized entity data (JSON)
   - Add index on cache_key for fast lookups

4. **Add Entity Cache Methods**: Implement methods for entity cache operations
   - `get_cached_entity(cache_key: str) -> Optional[Dict]`: Retrieve cached entity
   - `save_cached_entity(cache_key: str, entity: Any, entity_type: str)`: Store entity in cache
   - `_serialize_entity(entity: Any) -> str`: Convert entity to JSON
   - `_deserialize_entity(entity_data: str, entity_type: str) -> Any`: Reconstruct entity from JSON

**File**: `toolkit/managers/processors/user_analyzer_processor.py`

**Function**: `_resolve_reference`

**Changes**:
5. **Check Shared Cache First**: Before calling `get_entity()`, check database cache
   - Query `state.get_cached_entity(cache_key)`
   - If found, return cached entity
   - If not found, proceed with API call

6. **Store Successful Resolutions**: After successful `get_entity()` call, cache result
   - Call `state.save_cached_entity(cache_key, entity, entity_type)`
   - Update in-memory cache as before
   - This makes successful resolutions available to all accounts

7. **Maintain In-Memory Cache**: Keep existing `self._entity_cache` for performance
   - Check in-memory cache first (fastest)
   - Then check database cache (fast)
   - Finally make API call (slowest)
   - This provides three-tier caching: memory → database → API

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bugs on unfixed code, then verify the fixes work correctly and preserve existing behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate both bugs BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Write tests that simulate messages with t.me links and multiple account scenarios. Run these tests on the UNFIXED code to observe failures and understand the root causes.

**Test Cases**:
1. **t.me Link Extraction Test**: Create message with "Check out t.me/testuser" (will fail on unfixed code - username not extracted)
2. **https t.me Link Extraction Test**: Create message with "Visit https://t.me/testuser" (will fail on unfixed code - username not extracted)
3. **@username Extraction Test**: Create message with "@testuser" (will pass on unfixed code - existing functionality works)
4. **Multiple Account API Call Test**: Simulate 4 accounts resolving same username, count API calls (will show 4 calls on unfixed code instead of 1)
5. **Failed Lookup Sharing Test**: Simulate failed lookup, verify second account skips API call (will pass on unfixed code - already works)

**Expected Counterexamples**:
- t.me/username patterns are not extracted from message text
- https://t.me/username patterns are not extracted from message text
- Each account makes separate API calls for the same username
- Possible causes: incomplete regex pattern, per-instance cache without shared storage

### Fix Checking

**Goal**: Verify that for all inputs where the bug conditions hold, the fixed functions produce the expected behavior.

**Pseudocode for Bug 1:**
```
FOR ALL message WHERE message.text CONTAINS "t.me/" OR "https://t.me/" DO
  usernames := _extract_raw_usernames_fixed(message.text)
  ASSERT all valid t.me usernames IN usernames
END FOR
```

**Pseudocode for Bug 2:**
```
FOR ALL username WHERE username is resolvable DO
  account1.resolve(username)  // First resolution
  apiCallsBefore := count_api_calls()
  account2.resolve(username)  // Second resolution
  apiCallsAfter := count_api_calls()
  ASSERT apiCallsAfter == apiCallsBefore  // No new API call
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug conditions do NOT hold, the fixed functions produce the same result as the original functions.

**Pseudocode:**
```
FOR ALL message WHERE NOT (message.text CONTAINS "t.me/") DO
  ASSERT _extract_raw_usernames_original(message.text) = _extract_raw_usernames_fixed(message.text)
END FOR

FOR ALL reference WHERE reference is first-time lookup DO
  ASSERT _resolve_reference_original(reference) = _resolve_reference_fixed(reference)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many test cases automatically across the input domain
- It catches edge cases that manual unit tests might miss
- It provides strong guarantees that behavior is unchanged for all non-buggy inputs

**Test Plan**: Observe behavior on UNFIXED code first for @username extraction and first-time lookups, then write property-based tests capturing that behavior.

**Test Cases**:
1. **@username Preservation**: Observe that @username extraction works on unfixed code, then verify it continues after fix
2. **Failed Lookup Preservation**: Observe that failed lookups are cached on unfixed code, then verify this continues after fix
3. **First-Time Lookup Preservation**: Observe that first-time lookups make API calls on unfixed code, then verify this continues after fix
4. **Other Extraction Sources Preservation**: Verify forwards, replies, actions, participants continue working

### Unit Tests

- Test `_extract_raw_usernames` with various message formats:
  - Messages with @username only
  - Messages with t.me/username only
  - Messages with https://t.me/username only
  - Messages with mixed patterns
  - Messages with invalid usernames (too short, too long, invalid chars)
  - Messages with no usernames
- Test entity cache database operations:
  - Save and retrieve cached entities
  - Handle missing cache entries
  - Handle serialization/deserialization
- Test `_resolve_reference` with shared cache:
  - First lookup (cache miss, makes API call)
  - Second lookup (cache hit, no API call)
  - Failed lookup (uses failed_lookups table)

### Property-Based Tests

- Generate random message texts with various username patterns and verify all valid usernames are extracted
- Generate random sequences of account lookups for the same username and verify only the first makes an API call
- Generate random message texts without t.me links and verify extraction results match original behavior
- Generate random entity references and verify resolution behavior matches original for first-time lookups

### Integration Tests

- Test full user analyzer flow with messages containing t.me links across multiple accounts
- Test that extracted t.me usernames are properly resolved and stored in database
- Test that multiple accounts scanning the same groups reuse cached entities
- Test that the system continues to handle all existing extraction sources correctly
- Test that failed lookups continue to be shared across accounts
- Test that entity cache persists across processor restarts

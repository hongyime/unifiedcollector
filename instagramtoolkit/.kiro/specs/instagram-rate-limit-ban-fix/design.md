# Instagram Rate Limit Ban Fix - Bugfix Design

## Overview

Instagram accounts are receiving temporary bans due to excessive request rates that exceed Instagram's ~200 requests/hour threshold. The current implementation uses 3-8 second delays between requests, allowing 450-1200 requests/hour—significantly exceeding safe limits. This fix implements randomized delays (20-40 seconds between requests), mandatory rest periods (5-10 minutes after 30-50 operations), and hourly request monitoring to ensure request rates stay below 180 requests/hour per account, providing a safety margin while preserving all existing functionality (account rotation, cooldowns, quota management, operation-specific multipliers).

## Glossary

- **Bug_Condition (C)**: The condition that triggers the bug - when request delays are too short (3-8s), causing request rates (450-1200 req/hr) to exceed Instagram's safe limit (~200 req/hr)
- **Property (P)**: The desired behavior - request rates must stay below 180 req/hr through randomized delays (20-40s), rest periods (5-10min after 30-50 ops), and hourly monitoring
- **Preservation**: Existing account rotation, cooldown enforcement, quota management, and operation-specific delay multipliers that must remain unchanged
- **RateLimiter**: The class in `lib/rate_limiter.py` that provides centralized delay management with configurable base delays and periodic long breaks
- **ConservativeRateLimiter**: The class in `lib/conservative_rate_limiter.py` that applies operation-specific delay multipliers (PUBLIC: 1.0x, FOLLOWING_REQUIRED: 1.5x, MUTUAL_FOLLOWING: 2.0x)
- **MIN_DELAY / MAX_DELAY**: Configuration constants in `lib/config.py` that define the base delay range between requests
- **OPS_BEFORE_BREAK_MIN / OPS_BEFORE_BREAK_MAX**: Configuration constants that define when to trigger mandatory rest periods
- **BREAK_DURATION_MIN / BREAK_DURATION_MAX**: Configuration constants that define the length of mandatory rest periods
- **Request Rate**: Number of API requests per hour per account (Instagram enforces ~200 req/hr limit)

## Bug Details

### Bug Condition

The bug manifests when the system makes Instagram API requests with delays that are too short (3-8 seconds), resulting in request rates between 450-1200 requests per hour. This exceeds Instagram's enforcement threshold of approximately 200 requests per hour per account, triggering automated ban detection systems that place accounts on temporary restrictions.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type RequestDelayConfig
  OUTPUT: boolean
  
  RETURN (input.min_delay >= 3 AND input.max_delay <= 8)
         AND (calculated_hourly_rate(input) > 200)
         AND (no_mandatory_rest_periods OR rest_periods_too_short)
         AND account_receives_temporary_ban
END FUNCTION

FUNCTION calculated_hourly_rate(config)
  avg_delay = (config.min_delay + config.max_delay) / 2
  requests_per_hour = 3600 / avg_delay
  RETURN requests_per_hour
END FUNCTION
```

### Examples

- **Current behavior (3-8s delays)**: Average delay = 5.5s → 654 requests/hour → Exceeds 200 req/hr limit → Account banned
- **Current behavior (3s minimum)**: 3s delay → 1200 requests/hour → Significantly exceeds limit → Account banned quickly
- **Current behavior (no rest periods)**: Continuous requests without breaks → Predictable pattern → Detection triggered
- **Expected behavior (20-40s delays)**: Average delay = 30s → 120 requests/hour → Below 180 req/hr ceiling → No ban
- **Expected behavior (with rest periods)**: 30-50 operations → 5-10 minute rest → Mimics human behavior → Reduced detection risk

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Account rotation logic must continue to work with multiple Instagram accounts
- Account cooldown enforcement must continue to apply 15-minute minimum cooldowns after rate-limit hits
- Daily quota management must continue to track profile views (180/day) and actions (6000/day)
- Operation-specific delay multipliers must continue to apply (PUBLIC: 1.0x, FOLLOWING_REQUIRED: 1.5x, MUTUAL_FOLLOWING: 2.0x)
- Emergency backoff and account switching logic must continue to function on rate-limit errors
- Session file saving and authentication state management must continue to work
- Exponential backoff retry logic must continue to function

**Scope:**
All inputs that do NOT involve the base delay configuration (MIN_DELAY, MAX_DELAY, OPS_BEFORE_BREAK, BREAK_DURATION) should be completely unaffected by this fix. This includes:
- Account rotation and switching mechanisms
- Cooldown tracking and enforcement
- Quota tracking and limits
- Operation classification and multiplier application
- Error detection and handling
- Session management and persistence

## Hypothesized Root Cause

Based on the bug description and analysis of the current implementation, the root causes are:

1. **Insufficient Base Delays**: MIN_DELAY=3s and MAX_DELAY=8s are too short
   - Average delay of 5.5 seconds allows ~654 requests/hour
   - Instagram's limit is ~200 requests/hour
   - Current delays exceed safe limits by 3-6x

2. **Inadequate Rest Periods**: OPS_BEFORE_BREAK (5-15 ops) and BREAK_DURATION (3-8 min) are insufficient
   - Rest periods trigger too infrequently (every 5-15 operations)
   - Break durations (3-8 minutes) are too short to significantly reduce hourly rate
   - No hourly request monitoring to enforce 180 req/hr ceiling

3. **Missing Account Switch Cooldowns**: ACCOUNT_SWITCH_DELAY (5-10s) is too short
   - Current 5-10 second delays don't mimic human account switching behavior
   - Rapid account switches create detectable patterns
   - Should be 60-120 seconds to appear more human-like

4. **No Hourly Request Monitoring**: System doesn't track requests per hour per account
   - No mechanism to pause operations when approaching 180 req/hr limit
   - Can't proactively prevent exceeding Instagram's threshold
   - Relies only on delays without rate verification

## Correctness Properties

Property 1: Bug Condition - Request Rate Below Safe Limit

_For any_ sequence of Instagram API requests made by the system, the fixed rate limiting SHALL ensure the request rate stays below 180 requests per hour per account through randomized delays (20-40 seconds), mandatory rest periods (5-10 minutes after 30-50 operations), and hourly request monitoring that pauses operations when approaching the limit.

**Validates: Requirements 2.1, 2.2, 2.3, 2.5, 2.6**

Property 2: Preservation - Existing Functionality Unchanged

_For any_ rate limiting operation that does NOT involve base delay timing (account rotation, cooldown enforcement, quota management, operation multipliers, error handling), the fixed code SHALL produce exactly the same behavior as the original code, preserving all existing account management, quota tracking, and operation classification functionality.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `lib/config.py`

**Constants to Update**:
1. **Base Delay Range**: Update MIN_DELAY and MAX_DELAY
   - Change MIN_DELAY from 3 to 20 seconds
   - Change MAX_DELAY from 8 to 40 seconds
   - Rationale: 20-40s delays → avg 30s → 120 req/hr (well below 180 ceiling)

2. **Rest Period Frequency**: Update OPS_BEFORE_BREAK range
   - Change OPS_BEFORE_BREAK_MIN from 5 to 30 operations
   - Change OPS_BEFORE_BREAK_MAX from 15 to 50 operations
   - Rationale: More operations before rest mimics sustained human activity

3. **Rest Period Duration**: Update BREAK_DURATION range
   - Change BREAK_DURATION_MIN from 3 to 5 minutes
   - Change BREAK_DURATION_MAX from 8 to 10 minutes
   - Rationale: Longer breaks reduce hourly rate and mimic human rest patterns

4. **Account Switch Cooldown**: Update ACCOUNT_SWITCH_DELAY range
   - Change ACCOUNT_SWITCH_DELAY_MIN from 5 to 60 seconds
   - Change ACCOUNT_SWITCH_DELAY_MAX from 10 to 120 seconds
   - Rationale: Longer cooldowns mimic human account switching behavior

5. **Enumeration Delays**: Update ENUM_PAUSE_EVERY and ENUM_PAUSE_SECONDS
   - Keep ENUM_PAUSE_EVERY at 12 (already reasonable)
   - Increase ENUM_PAUSE_SECONDS from 10 to 30 seconds
   - Rationale: Progressive delays during follower/following enumeration maintain 180 req/hr ceiling

**File**: `lib/rate_limiter.py`

**No structural changes required** - the RateLimiter class already implements:
- Randomized delays via `_human_delay()` with gaussian distribution
- Periodic long breaks via `track_operation()` with randomized thresholds
- Interruptible sleep for graceful shutdown
- The config changes will automatically apply through the existing logic

**File**: `lib/conservative_rate_limiter.py`

**No structural changes required** - the ConservativeRateLimiter class already implements:
- Operation-specific delay multipliers (PUBLIC: 1.0x, FOLLOWING_REQUIRED: 1.5x, MUTUAL_FOLLOWING: 2.0x)
- Account switch delays via `account_switch_delay()`
- Enumeration delays via `following_enumeration_delay()`
- The config changes will automatically apply through the existing logic

**File**: `lib/account_cooldown.py` (Optional Enhancement)

**Optional Addition**: Hourly request monitoring
- Add `record_request()` method to track requests per hour per account
- Add `can_make_request()` method to check if account is below 180 req/hr
- Add hourly reset logic (similar to daily quota reset)
- This is optional but recommended for proactive rate enforcement

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bug on unfixed code (request rates exceeding 200 req/hr), then verify the fix works correctly (rates below 180 req/hr) and preserves existing behavior (account rotation, cooldowns, quotas, multipliers).

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm that current delay settings (3-8s) produce request rates exceeding 200 req/hr. If we refute, we will need to re-hypothesize.

**Test Plan**: Write tests that simulate sequences of Instagram API requests using current delay settings (MIN_DELAY=3, MAX_DELAY=8). Measure the actual request rate over a simulated hour. Run these tests on the UNFIXED code to observe request rates exceeding 200 req/hr and confirm the root cause.

**Test Cases**:
1. **Minimum Delay Test**: Simulate requests with 3s delays → measure rate → expect ~1200 req/hr (will fail on unfixed code)
2. **Maximum Delay Test**: Simulate requests with 8s delays → measure rate → expect ~450 req/hr (will fail on unfixed code)
3. **Average Delay Test**: Simulate requests with 5.5s average delays → measure rate → expect ~654 req/hr (will fail on unfixed code)
4. **With Current Rest Periods**: Simulate requests with 5-15 ops before 3-8 min breaks → measure rate → expect still exceeds 200 req/hr (will fail on unfixed code)

**Expected Counterexamples**:
- Request rates consistently exceed 200 req/hr with current settings
- Possible causes: MIN_DELAY too short (3s), MAX_DELAY too short (8s), rest periods too infrequent (5-15 ops), break durations too short (3-8 min)

### Fix Checking

**Goal**: Verify that for all request sequences where the bug condition holds (current delay settings), the fixed function produces request rates below 180 req/hr.

**Pseudocode:**
```
FOR ALL request_sequence WHERE isBugCondition(current_config) DO
  rate := measure_request_rate_with_fixed_config(request_sequence)
  ASSERT rate < 180  // Below safe ceiling
  ASSERT rate >= 100  // Above minimum threshold (not too slow)
END FOR
```

**Test Cases**:
1. **Fixed Minimum Delay**: Simulate requests with 20s delays → measure rate → expect ~180 req/hr
2. **Fixed Maximum Delay**: Simulate requests with 40s delays → measure rate → expect ~90 req/hr
3. **Fixed Average Delay**: Simulate requests with 30s average delays → measure rate → expect ~120 req/hr
4. **With Fixed Rest Periods**: Simulate requests with 30-50 ops before 5-10 min breaks → measure rate → expect <180 req/hr
5. **With Account Switch Cooldowns**: Simulate account switches with 60-120s delays → verify rate stays <180 req/hr
6. **With Enumeration Delays**: Simulate follower enumeration with 30s pauses every 12 items → verify rate <180 req/hr

### Preservation Checking

**Goal**: Verify that for all operations where the bug condition does NOT hold (account rotation, cooldowns, quotas, multipliers), the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL operation WHERE NOT involves_base_delay_timing(operation) DO
  ASSERT fixed_system(operation) = original_system(operation)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many test cases automatically across the operation domain
- It catches edge cases that manual unit tests might miss
- It provides strong guarantees that behavior is unchanged for all non-timing operations

**Test Plan**: Observe behavior on UNFIXED code first for account rotation, cooldowns, quotas, and multipliers, then write property-based tests capturing that behavior.

**Test Cases**:
1. **Account Rotation Preservation**: Observe that account rotation logic works correctly on unfixed code, then write test to verify this continues after fix
2. **Cooldown Enforcement Preservation**: Observe that 15-minute cooldowns are enforced on unfixed code, then write test to verify this continues after fix
3. **Quota Management Preservation**: Observe that daily quotas (180 profile views, 6000 actions) are tracked on unfixed code, then write test to verify this continues after fix
4. **Operation Multiplier Preservation**: Observe that operation-specific multipliers (PUBLIC: 1.0x, FOLLOWING_REQUIRED: 1.5x, MUTUAL_FOLLOWING: 2.0x) are applied on unfixed code, then write test to verify this continues after fix
5. **Emergency Backoff Preservation**: Observe that exponential backoff and account switching work on unfixed code, then write test to verify this continues after fix
6. **Session Management Preservation**: Observe that session files are saved correctly on unfixed code, then write test to verify this continues after fix

### Unit Tests

- Test that MIN_DELAY and MAX_DELAY produce delays in the 20-40 second range
- Test that OPS_BEFORE_BREAK triggers rest periods after 30-50 operations
- Test that BREAK_DURATION produces rest periods of 5-10 minutes
- Test that ACCOUNT_SWITCH_DELAY produces cooldowns of 60-120 seconds
- Test that ENUM_PAUSE_SECONDS produces 30-second pauses during enumeration
- Test that calculated request rates stay below 180 req/hr with new settings
- Test edge cases: single request, burst of requests, long-running sequences

### Property-Based Tests

- Generate random request sequences and verify all produce rates <180 req/hr with fixed config
- Generate random operation types and verify multipliers are still applied correctly
- Generate random account rotation scenarios and verify cooldowns are still enforced
- Generate random quota usage patterns and verify limits are still tracked
- Test that all non-timing operations continue to work across many scenarios

### Integration Tests

- Test full request flow with new delays in each operation context (PUBLIC, FOLLOWING_REQUIRED, MUTUAL_FOLLOWING)
- Test account switching with new cooldown periods and verify rotation continues to work
- Test follower/following enumeration with progressive delays and verify rate stays <180 req/hr
- Test that emergency cooldowns still trigger on rate-limit errors
- Test that session files are still saved correctly after operations complete
- Test that hourly request monitoring (if implemented) correctly pauses operations at 180 req/hr threshold

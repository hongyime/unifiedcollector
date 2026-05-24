# Human-Like Rate Limiting Bugfix Design

## Overview

The Lemon8 scraper currently makes direct HTTP requests without rate limiting, causing 403 Forbidden errors due to automated traffic detection. While an `AdaptiveRateLimiter` class exists in `src/rate_limiter.py`, it is not integrated into the `Lemon8Scraper` class. This bugfix will integrate the rate limiter into all HTTP requests, add exponential backoff with jitter for retry logic, and enhance the rate limiter to support randomized delays that mimic human-like behavior.

The fix will make requests appear more human-like through:
- Enforced delays before each HTTP request using the existing `AdaptiveRateLimiter`
- Exponential backoff with retries for 403/429 responses
- Randomized jitter added to delays to avoid predictable timing patterns
- Progressive delay increases on repeated failures

## Glossary

- **Bug_Condition (C)**: The condition that triggers the bug - when HTTP requests are made without rate limiting, causing 403 Forbidden errors
- **Property (P)**: The desired behavior when making HTTP requests - requests should be rate-limited with human-like delays and retry logic
- **Preservation**: Existing scraping functionality (media extraction, cookie handling, header rotation) that must remain unchanged by the fix
- **AdaptiveRateLimiter**: The class in `src/rate_limiter.py` that manages per-account rate limiting with cooldown support
- **Lemon8Scraper**: The main scraper class in `src/scraper.py` that makes HTTP requests to Lemon8 endpoints
- **session.get()**: The requests library method used to make HTTP GET requests - currently called without rate limiting
- **Jitter**: Random variation added to delays to make timing patterns less predictable and more human-like
- **Exponential Backoff**: A retry strategy where delays increase exponentially after each failure (e.g., 2s, 4s, 8s)

## Bug Details

### Bug Condition

The bug manifests when the scraper makes HTTP requests to Lemon8 endpoints without rate limiting. The `Lemon8Scraper.__init__` method does not create an `AdaptiveRateLimiter` instance, and all `session.get()` calls are made directly without calling `rate_limiter.wait()` beforehand. This causes requests to be sent with no delays, appearing as automated traffic and triggering 403 Forbidden responses.

**Formal Specification:**
```
FUNCTION isBugCondition(request)
  INPUT: request of type HTTPRequest
  OUTPUT: boolean
  
  RETURN request.method == 'GET'
         AND request.target IN ['lemon8-app.com', 'tiktokcdn.com', 'byteimg.com']
         AND NOT rate_limiter_called_before_request(request)
         AND NOT retry_logic_exists_for_403_429(request)
         AND NOT jitter_applied_to_delays(request)
END FUNCTION
```

### Examples

- **User Profile Scraping**: When `scrape_user()` calls `self.session.get(url, timeout=30)` at line 1645, the request is made immediately without any delay, causing Lemon8 to detect automated traffic and return 403 Forbidden
- **Feed Scraping**: When `scrape_feed()` calls `self.session.get(url, timeout=30)` at line 1866, consecutive requests are sent with no delay between them, creating a predictable pattern that triggers rate limiting
- **Post Scraping**: When `scrape_post()` calls `self.session.get(post_url, timeout=30)` at line 2120, a 403 response causes immediate failure with no retry or backoff logic
- **Tag Discovery**: When `scrape_tag()` calls `self.session.get(discover_url, timeout=30)` at line 2180, the request is sent without checking if the account is in cooldown from previous 429 responses
- **Discover Page**: When `scrape_discover()` calls `self.session.get(page_url, timeout=30)` at line 2291, fixed delays (if any) are used without randomization, making the timing pattern predictable

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Media extraction from HTML and JSON must continue to work correctly (all `_extract_media_*` methods)
- Cookie loading and authentication must remain unchanged (`_load_cookies_into_session`)
- Header rotation must continue to apply randomized browser-like headers (`_apply_rotating_headers`)
- Media file downloads must save files to correct directories with proper naming
- Progress tracking and visited user tracking must continue to update correctly
- The `AdaptiveRateLimiter` must continue to reduce delays after consecutive successes
- pylemon8 API integration must continue to work when available

**Scope:**
All functionality that does NOT involve making HTTP requests to Lemon8 endpoints should be completely unaffected by this fix. This includes:
- Media URL parsing and validation (`_is_valid_media_url`, `_clean_media_url`)
- Username extraction and normalization (`_extract_username_from_*`, `_normalize_username`)
- JSON data structure traversal (`_find_item_lists_in_json`, `_extract_urls_from_json`)
- File I/O operations (downloading media, saving files)
- Data structure manipulation (deduplication, building media items)

## Hypothesized Root Cause

Based on the bug description and code analysis, the root causes are:

1. **Missing Rate Limiter Integration**: The `Lemon8Scraper.__init__` method does not instantiate an `AdaptiveRateLimiter`, so no rate limiting infrastructure exists
   - The `AdaptiveRateLimiter` class exists in `src/rate_limiter.py` but is never imported or used in `src/scraper.py`
   - No `self.rate_limiter` attribute is created during scraper initialization

2. **No Pre-Request Delays**: All five `session.get()` calls in `scraper.py` (lines 1645, 1866, 2120, 2180, 2291) are made directly without calling `rate_limiter.wait()` beforehand
   - Requests are sent immediately with no enforced delays
   - No account-specific cooldown checking occurs before requests

3. **Missing Retry Logic**: When 403 or 429 responses occur, the code calls `response.raise_for_status()` which immediately raises an exception without retry attempts
   - No exponential backoff is implemented
   - No retry counter or maximum retry limit exists

4. **No Jitter Support**: The existing `AdaptiveRateLimiter.wait()` method uses fixed delays without randomization
   - Delays are predictable (exactly `current_delay` seconds)
   - No random variation is added to make timing patterns appear human-like

5. **No Error-Specific Handling**: The code does not distinguish between 403 (Forbidden) and 429 (Too Many Requests) responses
   - Both should trigger different backoff strategies
   - 429 should trigger cooldown, while 403 should trigger progressive delay increases

## Correctness Properties

Property 1: Bug Condition - Rate-Limited HTTP Requests with Retry Logic

_For any_ HTTP GET request to Lemon8 endpoints where the bug condition holds (no rate limiting applied), the fixed scraper SHALL call `rate_limiter.wait()` before making the request, implement exponential backoff with jitter for 403/429 responses (up to 3 retry attempts with delays of base_delay * 2^attempt + random jitter), and record successes/failures with the rate limiter to adapt delays dynamically.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7**

Property 2: Preservation - Existing Scraping Functionality

_For any_ scraping operation that does NOT involve making HTTP requests (media extraction, cookie handling, header rotation, file downloads, progress tracking), the fixed code SHALL produce exactly the same behavior as the original code, preserving all existing functionality for data parsing, validation, and file operations.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `src/scraper.py`

**Function**: `Lemon8Scraper.__init__`

**Specific Changes**:

1. **Import AdaptiveRateLimiter**: Add import statement at the top of the file
   - `from rate_limiter import AdaptiveRateLimiter`

2. **Initialize Rate Limiter in __init__**: Create an `AdaptiveRateLimiter` instance with appropriate parameters
   - Add `self.rate_limiter = AdaptiveRateLimiter(base_delay=3.0, min_delay=2.0, max_delay=15.0, success_threshold=5, delay_reduction=0.5)` after session initialization
   - Use higher base_delay (3.0s) and max_delay (15.0s) than default to be more conservative with Lemon8

3. **Enhance AdaptiveRateLimiter with Jitter**: Modify `AdaptiveRateLimiter.wait()` in `src/rate_limiter.py` to add randomized jitter
   - Add jitter parameter to `__init__` (default 0.3 for ±30% variation)
   - In `wait()` method, calculate `jittered_delay = current_delay * (1 + random.uniform(-jitter, jitter))`
   - Use `jittered_delay` instead of `current_delay` for sleep duration

4. **Create Retry Wrapper Method**: Add a new method `_make_request_with_retry()` to handle retries with exponential backoff
   - Parameters: `url`, `max_retries=3`, `account='default'`, `referer=None`, `timeout=30`
   - Loop up to `max_retries` attempts
   - Before each attempt: call `self.rate_limiter.wait(account)`
   - Make request: `response = self.session.get(url, timeout=timeout)`
   - On success (2xx): call `self.rate_limiter.record_success(account)` and return response
   - On 429: call `self.rate_limiter.record_rate_limit(account)`, log warning, continue to next retry
   - On 403: call `self.rate_limiter.record_error(account)`, log warning, continue to next retry
   - On other errors: raise immediately
   - After max retries: raise the last exception

5. **Replace All session.get() Calls**: Update all five locations to use the new retry wrapper
   - Line 1645 in `scrape_user()`: Replace with `response = self._make_request_with_retry(url, referer=get_user_url(username))`
   - Line 1866 in `scrape_feed()`: Replace with `response = self._make_request_with_retry(url, referer=FEED_URL)`
   - Line 2120 in `scrape_post()`: Replace with `response = self._make_request_with_retry(post_url, referer=post_url)`
   - Line 2180 in `scrape_tag()`: Replace with `response = self._make_request_with_retry(discover_url, referer=discover_url)`
   - Line 2291 in `scrape_discover()`: Replace with `response = self._make_request_with_retry(page_url, referer=page_url)`

6. **Add Logging for Rate Limiting**: Enhance logging to show rate limiting activity
   - Log when rate limiter waits (already in `AdaptiveRateLimiter.wait()`)
   - Log when retries occur in `_make_request_with_retry()`
   - Log when delays are adjusted (already in `AdaptiveRateLimiter.record_success()` and `record_rate_limit()`)

7. **Remove raise_for_status() Calls**: Since retry logic handles errors, remove immediate `raise_for_status()` calls after `session.get()`
   - The retry wrapper will handle status code checking and retries
   - Only raise after all retries are exhausted

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bug on unfixed code (requests fail with 403 due to lack of rate limiting), then verify the fix works correctly (requests succeed with rate limiting) and preserves existing behavior (media extraction still works).

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Write tests that make HTTP requests to Lemon8 endpoints without rate limiting and observe 403 Forbidden responses. Run these tests on the UNFIXED code to confirm the bug exists and understand the failure patterns.

**Test Cases**:
1. **Rapid Consecutive Requests Test**: Make 5 consecutive `session.get()` calls to a user profile URL with no delays (will fail with 403 on unfixed code)
2. **No Retry on 403 Test**: Make a request that returns 403 and verify it raises immediately without retry (will fail on unfixed code)
3. **Fixed Delay Pattern Test**: Make requests with fixed 2-second delays and verify they still get blocked due to predictable timing (may fail on unfixed code)
4. **Missing Rate Limiter Test**: Verify that `Lemon8Scraper` instance has no `rate_limiter` attribute (will fail on unfixed code)

**Expected Counterexamples**:
- Requests fail with 403 Forbidden when made without delays
- Requests fail with 403 even with fixed delays due to predictable patterns
- No `rate_limiter` attribute exists on scraper instance
- Possible causes: missing rate limiter integration, no retry logic, no jitter in delays

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL request WHERE isBugCondition(request) DO
  result := make_request_with_rate_limiting(request)
  ASSERT expectedBehavior(result)
END FOR
```

**Expected Behavior After Fix:**
- `rate_limiter.wait()` is called before each `session.get()`
- Delays have randomized jitter (vary by ±30%)
- 403/429 responses trigger retries with exponential backoff
- Successful requests record success with rate limiter
- Failed requests record errors/rate limits with rate limiter

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL operation WHERE NOT isBugCondition(operation) DO
  ASSERT original_scraper(operation) = fixed_scraper(operation)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many test cases automatically across the input domain
- It catches edge cases that manual unit tests might miss
- It provides strong guarantees that behavior is unchanged for all non-HTTP-request operations

**Test Plan**: Observe behavior on UNFIXED code first for media extraction, cookie handling, and data parsing, then write property-based tests capturing that behavior.

**Test Cases**:
1. **Media Extraction Preservation**: Verify that `_extract_media_urls()` produces the same results before and after fix
2. **Cookie Loading Preservation**: Verify that `_load_cookies_into_session()` loads the same cookies before and after fix
3. **Header Rotation Preservation**: Verify that `_apply_rotating_headers()` generates the same header patterns before and after fix
4. **Username Extraction Preservation**: Verify that `_extract_username_from_item()` extracts the same usernames before and after fix
5. **Media Item Building Preservation**: Verify that `_build_media_item()` creates the same data structures before and after fix

### Unit Tests

- Test `AdaptiveRateLimiter` jitter calculation (verify delays vary within expected range)
- Test `_make_request_with_retry()` with mocked responses (200, 403, 429)
- Test retry logic exhausts after max_retries attempts
- Test exponential backoff increases delays correctly (2s, 4s, 8s pattern)
- Test that `rate_limiter.wait()` is called before each request
- Test that `rate_limiter.record_success()` is called on 2xx responses
- Test that `rate_limiter.record_rate_limit()` is called on 429 responses
- Test that `rate_limiter.record_error()` is called on 403 responses

### Property-Based Tests

- Generate random sequences of HTTP status codes (200, 403, 429, 500) and verify retry logic handles them correctly
- Generate random delay values and verify jitter stays within bounds (base_delay ± jitter%)
- Generate random request patterns and verify rate limiter state updates correctly (delays, successes, cooldowns)
- Test that all non-HTTP operations (media extraction, parsing) produce identical results with random input data

### Integration Tests

- Test full user scraping flow with rate limiting enabled (scrape a real user profile)
- Test feed scraping with multiple pages and verify delays are applied between requests
- Test that 403 responses trigger retries and eventually succeed (or fail gracefully after max retries)
- Test that 429 responses trigger cooldown and subsequent requests wait appropriately
- Test that successful scraping sessions gradually reduce delays (adaptive behavior)
- Test that mixed success/failure patterns adjust delays correctly

# Bugfix Requirements Document

## Introduction

Instagram accounts are experiencing temporary bans when using instaloader for automated operations. Users receive prompts on their mobile devices indicating temporary restrictions that prevent scrolling or fetching data. The issue persists despite existing rate limiting measures (MIN_DELAY=3s, MAX_DELAY=8s, pauses every 12 operations).

Research indicates Instagram enforces approximately 200 API requests per hour per account. Current delay settings (3-8 seconds between requests) allow 450-1200 requests per hour, significantly exceeding Instagram's safe limits and triggering automated ban detection systems.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the system makes requests with 3-8 second delays between operations THEN Instagram detects excessive request frequency (450-1200 requests/hour vs. safe limit of ~200/hour)

1.2 WHEN Instagram detects excessive request patterns THEN accounts receive temporary bans with mobile prompts requiring acknowledgment

1.3 WHEN multiple operations are performed in sequence without sufficient spacing THEN the cumulative request rate exceeds Instagram's 200 requests/hour threshold

1.4 WHEN accounts switch without adequate cooldown periods THEN the new account immediately continues high-frequency requests, compounding detection risk

1.5 WHEN follower/following enumeration occurs with 10-second pauses every 12 items THEN the request rate still exceeds safe limits during enumeration operations

### Expected Behavior (Correct)

2.1 WHEN the system makes requests THEN delays SHALL ensure the request rate stays below 180 requests per hour per account (safety margin below 200/hour limit)

2.2 WHEN calculating delay between requests THEN the system SHALL use randomized delays with minimum 20 seconds and maximum 40 seconds to mimic human interaction patterns

2.3 WHEN performing follower/following enumeration THEN the system SHALL implement progressive delays that maintain the 180 requests/hour ceiling

2.4 WHEN switching between accounts THEN the system SHALL enforce randomized cooldown periods between 60-120 seconds to avoid pattern detection and mimic human behavior

2.5 WHEN an account completes a batch of operations THEN the system SHALL implement randomized mandatory rest periods (5-10 minutes) after every 30-50 operations to mimic human rest patterns

2.6 WHEN operations are tracked THEN the system SHALL monitor hourly request counts per account and pause operations when approaching 180 requests/hour

### Unchanged Behavior (Regression Prevention)

3.1 WHEN rate limiting is applied THEN the system SHALL CONTINUE TO support multiple Instagram accounts with rotation

3.2 WHEN delays are increased THEN the system SHALL CONTINUE TO track account cooldowns and quota management

3.3 WHEN conservative rate limiting is active THEN the system SHALL CONTINUE TO apply operation-specific delay multipliers (PUBLIC: 1.0x, FOLLOWING_REQUIRED: 1.5x, MUTUAL_FOLLOWING: 2.0x)

3.4 WHEN emergency cooldowns are triggered THEN the system SHALL CONTINUE TO enforce minimum 15-minute account cooldowns

3.5 WHEN the system encounters rate limit errors THEN the system SHALL CONTINUE TO implement exponential backoff and account switching logic

3.6 WHEN operations complete successfully THEN the system SHALL CONTINUE TO save session files and maintain authentication state

# Bugfix Requirements Document

## Introduction

The Instagram toolkit is experiencing excessive rate limiting (429 Too Many Requests errors) when making API calls to Instagram's endpoints during spider operations. Despite having rate limiting mechanisms in place (20-40s base delays, enumeration pauses, long breaks), the current implementation is not human-like enough, causing Instagram's detection systems to identify and block the requests. This leads to failed operations, potential account bans, and disrupted data collection workflows.

The bug manifests as:
- Multiple consecutive 429 errors for different usernames
- Errors occurring even with existing delays (e.g., "Waiting 43s" between batches)
- Spider operations showing "Natural pause in list traversal" but still getting rate limited

This bugfix aims to make the rate limiting behavior more human-like by introducing additional randomization, variability, and behavioral patterns that better mimic real human usage of Instagram.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the toolkit makes API calls with fixed delay ranges (20-40s base delay) THEN the system produces predictable timing patterns that Instagram's detection systems can identify as bot behavior

1.2 WHEN enumeration pauses occur at fixed intervals (every 12 items) THEN the system creates regular patterns that don't match human scroll behavior

1.3 WHEN delays are calculated using simple uniform random distribution within narrow ranges THEN the system generates timing patterns that lack the natural variability of human behavior

1.4 WHEN the same delay calculation method is used for all operations THEN the system fails to differentiate between different types of user actions (quick glances vs. detailed reading)

1.5 WHEN account switching occurs with delays in a fixed range (180-300s) THEN the system creates predictable patterns that don't match human account switching behavior

1.6 WHEN long breaks occur at predictable intervals (30-50 operations) THEN the system creates regular session patterns that don't match natural human fatigue and distraction

1.7 WHEN the toolkit processes items in rapid succession without micro-pauses THEN the system exhibits machine-like consistency that differs from human interaction patterns

1.8 WHEN API calls are made without considering the type of content being viewed THEN the system fails to simulate realistic reading/viewing time based on content complexity

### Expected Behavior (Correct)

2.1 WHEN the toolkit makes API calls THEN the system SHALL use variable delay ranges with multiple randomization layers (base delay + jitter + micro-delays) to create unpredictable timing patterns

2.2 WHEN enumeration pauses occur THEN the system SHALL vary both the interval (not always every 12 items) and the pause duration to simulate natural human scroll fatigue patterns

2.3 WHEN delays are calculated THEN the system SHALL use multiple probability distributions (Gaussian, exponential, uniform) to create more natural timing variability

2.4 WHEN different operations are performed THEN the system SHALL apply operation-specific delay multipliers and patterns (e.g., longer delays for profile views vs. list scrolling)

2.5 WHEN account switching occurs THEN the system SHALL use wider delay ranges with additional randomization to better simulate human account switching behavior

2.6 WHEN long breaks are scheduled THEN the system SHALL randomize both the trigger point and duration more aggressively to avoid predictable session patterns

2.7 WHEN processing items THEN the system SHALL introduce random micro-pauses (0.5-3s) between operations to simulate human reading/thinking time

2.8 WHEN API calls are made THEN the system SHALL consider content type and adjust delays accordingly (e.g., longer delays when viewing profiles with more content)

### Unchanged Behavior (Regression Prevention)

3.1 WHEN rate limiting is applied THEN the system SHALL CONTINUE TO respect minimum and maximum delay bounds configured in config.py

3.2 WHEN emergency cooldowns are triggered (429 errors) THEN the system SHALL CONTINUE TO place accounts on cooldown for at least 15 minutes

3.3 WHEN smart scheduling is enabled THEN the system SHALL CONTINUE TO apply time-of-day multipliers during risky hours

3.4 WHEN shutdown is requested (Ctrl+C) THEN the system SHALL CONTINUE TO interrupt delays immediately and save progress

3.5 WHEN session statistics are tracked THEN the system SHALL CONTINUE TO display operation counts and elapsed time

3.6 WHEN human-readable messages are shown THEN the system SHALL CONTINUE TO display clear explanations for why delays are occurring

3.7 WHEN countdown timers are displayed THEN the system SHALL CONTINUE TO show remaining time for long waits (≥30 seconds)

3.8 WHEN account availability is checked THEN the system SHALL CONTINUE TO filter out accounts currently on cooldown

3.9 WHEN daily quotas are enforced THEN the system SHALL CONTINUE TO track and limit profile views and actions per account

3.10 WHEN operation-specific delays are applied THEN the system SHALL CONTINUE TO use the existing multiplier system (PUBLIC: 1.0x, FOLLOWING_REQUIRED: 1.5x, MUTUAL_FOLLOWING: 2.0x)

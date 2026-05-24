# Bugfix Requirements Document

## Introduction

The Lemon8 scraper is experiencing HTTP 403 Forbidden errors when making requests to scrape user profiles and API endpoints. The root cause is that the scraper makes direct HTTP requests without proper rate limiting, human-like delays, or retry logic, causing the requests to be detected and blocked as automated traffic.

While an `AdaptiveRateLimiter` class exists in the codebase (`src/rate_limiter.py`), it is not integrated into the `Lemon8Scraper` class. The scraper makes direct `session.get()` calls without any rate limiting, exponential backoff, jitter, or retry logic for 403/429 responses.

This bugfix will integrate the existing rate limiter into all HTTP requests and enhance it with exponential backoff, randomized delays (jitter), and proper retry logic to make requests appear more human-like and avoid detection.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the scraper makes HTTP requests to Lemon8 endpoints THEN the system makes direct `session.get()` calls without any rate limiting delays

1.2 WHEN the scraper receives a 403 Forbidden response THEN the system fails immediately without retry or backoff logic

1.3 WHEN the scraper makes consecutive requests THEN the system sends them with no delay or randomization, appearing as automated traffic

1.4 WHEN the scraper receives a 429 rate limit response THEN the system does not implement exponential backoff or sufficient cooldown periods

1.5 WHEN the scraper makes multiple requests in sequence THEN the system uses fixed delays without jitter, creating predictable timing patterns

1.6 WHEN the `AdaptiveRateLimiter` class exists in the codebase THEN the system does not integrate or use it in the `Lemon8Scraper` class

### Expected Behavior (Correct)

2.1 WHEN the scraper makes HTTP requests to Lemon8 endpoints THEN the system SHALL integrate the `AdaptiveRateLimiter` to enforce delays before each request

2.2 WHEN the scraper receives a 403 Forbidden response THEN the system SHALL implement exponential backoff with retries (e.g., 3 attempts with increasing delays)

2.3 WHEN the scraper makes consecutive requests THEN the system SHALL add randomized jitter to delays to simulate human-like timing patterns

2.4 WHEN the scraper receives a 429 rate limit response THEN the system SHALL trigger the rate limiter's cooldown mechanism and increase delays exponentially

2.5 WHEN the scraper makes multiple requests in sequence THEN the system SHALL randomize delays within a range (e.g., base_delay ± 50% jitter) to avoid predictable patterns

2.6 WHEN the scraper initializes THEN the system SHALL create an `AdaptiveRateLimiter` instance and use it for all HTTP requests in both traditional scraping and pylemon8 API calls

2.7 WHEN the scraper encounters repeated 403 errors THEN the system SHALL increase the base delay progressively and log warnings about potential blocking

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the scraper successfully retrieves content without errors THEN the system SHALL CONTINUE TO parse and extract media URLs, user information, and metadata correctly

3.2 WHEN the scraper uses rotating headers THEN the system SHALL CONTINUE TO apply randomized browser-like headers to requests

3.3 WHEN the scraper loads cookies from a cookie file THEN the system SHALL CONTINUE TO authenticate requests using the loaded cookies

3.4 WHEN the scraper downloads media files THEN the system SHALL CONTINUE TO save files to the correct directories with proper naming

3.5 WHEN the scraper tracks visited users and progress THEN the system SHALL CONTINUE TO update the tracker and progress manager correctly

3.6 WHEN the rate limiter reduces delays after consecutive successes THEN the system SHALL CONTINUE TO optimize request timing for successful scraping sessions

3.7 WHEN the scraper uses the pylemon8 API (if available) THEN the system SHALL CONTINUE TO fall back to web scraping methods when the API is unavailable or blocked

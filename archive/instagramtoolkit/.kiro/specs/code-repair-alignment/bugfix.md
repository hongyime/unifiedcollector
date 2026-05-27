# Bugfix Requirements Document: Code Repair and Alignment

## Introduction

This bugfix addresses multiple critical issues across the Instagram data collection toolkit that have accumulated as technical debt. The issues span security vulnerabilities (exposed credentials), logic correctness errors (return contract mismatches, validation fallbacks), state consistency problems (missing file locks, fragile metadata collection), and documentation drift (README/PRD misalignment with actual code).

The repair plan is prioritized into three tiers:
- **P0 (Critical)**: Security issues requiring immediate action
- **P1 (High)**: Critical logic errors and state consistency issues affecting reliability
- **P2 (Medium)**: Documentation drift and structural debt affecting maintainability

**Impact**: These issues collectively compromise security, reliability, and maintainability of the toolkit. The systematic repair will restore code integrity and align documentation with actual capabilities.

## Bug Analysis

### Current Behavior (Defect)

#### 1. Security Issues (P0)

1.1 WHEN .env file contains real account credentials THEN the system exposes sensitive information in the repository

1.2 WHEN sessions/ directory contains real session files THEN the system exposes authenticated sessions in the repository

1.3 WHEN .gitignore does not properly cover .env and sessions/ THEN the system risks committing sensitive files to version control

#### 2. Critical Logic Errors (P1)

2.1 WHEN MediaDownloader.download_all() returns a dict with success/partial_success keys THEN callers in InstagramProcessor.download_media and main.py treat the return value as boolean, causing incorrect success/failure detection

2.2 WHEN InstagramAccountManager.login performs session validation and the check fails THEN the system falls back to "assuming valid" behavior, masking real authentication failures

2.3 WHEN ProgressManager.cleanup_progress and ArchiveRetentionManager.cleanup_all execute archive cleanup THEN the system uses inconsistent directory paths, file naming patterns, and glob patterns, causing archive retention policy to fail

#### 3. State Consistency Issues (P1)

3.1 WHEN collect_relationships.py writes to relationships.json THEN the system performs critical JSON writes without proper file locking, risking data corruption

3.2 WHEN profile_access_tracker.py writes to profile_access.json THEN the system performs critical JSON writes without proper file locking, risking data corruption

3.3 WHEN user_metadata_manager.py writes to user_profiles.json THEN the system performs critical JSON writes without proper file locking, risking data corruption

3.4 WHEN lib/account_cooldown.py writes to account_cooldowns.json and account_quotas.json THEN the system performs critical JSON writes without proper file locking, risking data corruption

3.5 WHEN UserMetadataManager.update_profile collects follower count THEN the system uses mediacount as a proxy, then tries _metadata with unclear fallback strategy, resulting in fragile and inaccurate metadata collection

3.6 WHEN RelationshipCollector.collect_for_user performs deduplication checks THEN the system uses linear any(...) iteration for each relationship, causing O(n²) performance degradation on large datasets

#### 4. Documentation Drift (P2)

4.1 WHEN users read README.md THEN the system documentation is missing: analyze-profiles command, --seed-mutual/--min-mutual flags, correct anti-detection parameters, account manager limitations, and security warnings

4.2 WHEN users read PRD.md THEN the system documentation contains inaccurate claims including "Production Ready" status, "unlimited target users" capability, and overstated command architecture

4.3 WHEN users access the web dashboard THEN the system documentation does not describe limitations such as lack of access restrictions, CORS configuration, or rendering limits

#### 5. Structural Debt (P2)

5.1 WHEN main.py dispatches commands THEN the system uses a monolithic dispatcher pattern instead of leveraging the lib/commands/ module architecture

5.2 WHEN web/server.py starts the HTTP server THEN the system binds to all network interfaces (0.0.0.0) with CORS wide open, creating security exposure

5.3 WHEN web/dashboard.js renders relationship graphs THEN the system has no rendering limits for large datasets, causing browser performance issues

### Expected Behavior (Correct)

#### 1. Security Issues (P0)

1.1 WHEN .env file is checked THEN the system SHALL contain only placeholder credentials with clear warnings not to commit real credentials

1.2 WHEN sessions/ directory is checked THEN the system SHALL contain no real session files in the repository

1.3 WHEN .gitignore is reviewed THEN the system SHALL properly exclude .env and sessions/ from version control

1.4 WHEN exposed credentials are identified THEN the system documentation SHALL include external action requirement to rotate all exposed account passwords

#### 2. Critical Logic Errors (P1)

2.1 WHEN MediaDownloader.download_all() returns a result THEN the system SHALL ensure callers correctly interpret the dict structure with success/partial_success keys, propagating download status accurately

2.2 WHEN InstagramAccountManager.login performs session validation THEN the system SHALL not fall back to "assuming valid" behavior when validation fails, ensuring authentication failures are properly detected and reported

2.3 WHEN ProgressManager.cleanup_progress and ArchiveRetentionManager.cleanup_all execute THEN the system SHALL use consistent directory paths, file naming patterns, and glob patterns to ensure archive retention policy works correctly

#### 3. State Consistency Issues (P1)

3.1 WHEN collect_relationships.py writes to relationships.json THEN the system SHALL use FileLock wrapper for atomic writes to prevent data corruption

3.2 WHEN profile_access_tracker.py writes to profile_access.json THEN the system SHALL use FileLock wrapper for atomic writes to prevent data corruption

3.3 WHEN user_metadata_manager.py writes to user_profiles.json THEN the system SHALL use FileLock wrapper for atomic writes to prevent data corruption

3.4 WHEN lib/account_cooldown.py writes to account_cooldowns.json and account_quotas.json THEN the system SHALL use FileLock wrapper for atomic writes to prevent data corruption

3.5 WHEN UserMetadataManager.update_profile collects follower count THEN the system SHALL use a clear, documented strategy to extract follower/following counts from profile objects with explicit fallback behavior

3.6 WHEN RelationshipCollector.collect_for_user performs deduplication THEN the system SHALL use a set-based or dict-based approach to achieve O(1) lookup performance

#### 4. Documentation Drift (P2)

4.1 WHEN users read README.md THEN the system SHALL document: analyze-profiles command, --seed-mutual/--min-mutual flags, correct anti-detection parameters, account manager limitations, and security warnings

4.2 WHEN users read PRD.md THEN the system SHALL accurately describe: actual production status, realistic user limits based on quotas, and accurate command architecture

4.3 WHEN users access web dashboard documentation THEN the system SHALL document: lack of access restrictions, CORS configuration, and rendering limits for large graphs

#### 5. Structural Debt (P2)

5.1 WHEN main.py dispatches commands THEN the system SHALL use a lightweight router that delegates to lib/commands/ modules instead of monolithic dispatcher logic

5.2 WHEN web/server.py configuration is reviewed THEN the system SHALL document the security implications of binding to all interfaces and CORS settings, with recommendations for production deployment

5.3 WHEN web/dashboard.js renders large datasets THEN the system SHALL implement rendering limits or pagination to prevent browser performance degradation

### Unchanged Behavior (Regression Prevention)

#### 1. Core Functionality Preservation

1.1 WHEN spider operations collect relationships THEN the system SHALL CONTINUE TO collect followers/following data correctly for accessible profiles

1.2 WHEN download operations retrieve media THEN the system SHALL CONTINUE TO download posts, stories, highlights, and profile photos successfully

1.3 WHEN account rotation occurs THEN the system SHALL CONTINUE TO switch accounts on rate limits and maintain cooldown/quota tracking

1.4 WHEN progress tracking saves state THEN the system SHALL CONTINUE TO enable resumption of interrupted operations

#### 2. Authentication and Session Management

2.1 WHEN valid sessions exist THEN the system SHALL CONTINUE TO restore sessions successfully without re-authentication

2.2 WHEN 2FA is required THEN the system SHALL CONTINUE TO prompt for OTP codes interactively

2.3 WHEN browser cookies are available THEN the system SHALL CONTINUE TO import sessions from configured browsers

#### 3. Data Integrity

3.1 WHEN safe_json_write is used THEN the system SHALL CONTINUE TO perform atomic writes using temp file + rename pattern

3.2 WHEN FileLock is used THEN the system SHALL CONTINUE TO provide cross-platform file locking (Windows/Unix)

3.3 WHEN retry_with_backoff is used THEN the system SHALL CONTINUE TO provide exponential backoff retry logic

#### 4. Rate Limiting and Anti-Detection

4.1 WHEN RateLimiter tracks operations THEN the system SHALL CONTINUE TO enforce configurable delays and automatic long breaks

4.2 WHEN AccountQuotaManager tracks usage THEN the system SHALL CONTINUE TO enforce daily quotas (180 profile views, 6000 actions per account)

4.3 WHEN AccountCooldownManager manages cooldowns THEN the system SHALL CONTINUE TO place accounts on 15-minute cooldown after rate limits

#### 5. CLI and User Interface

5.1 WHEN users run CLI commands THEN the system SHALL CONTINUE TO provide consistent command structure with --help documentation

5.2 WHEN batch files are executed THEN the system SHALL CONTINUE TO provide Windows menu interface for ease of use

5.3 WHEN web dashboard is accessed THEN the system SHALL CONTINUE TO serve static files and JSON API endpoints

#### 6. Testing Infrastructure

6.1 WHEN pytest runs unit tests THEN the system SHALL CONTINUE TO execute offline tests without API calls

6.2 WHEN pytest runs with --run-integration flag THEN the system SHALL CONTINUE TO execute integration tests with Instagram API

6.3 WHEN test suite completes THEN the system SHALL CONTINUE TO report 465 test cases across 22 test modules

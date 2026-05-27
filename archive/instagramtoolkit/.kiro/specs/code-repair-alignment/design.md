# Code Repair and Alignment Bugfix Design

## Overview

This design addresses multiple critical issues across the Instagram data collection toolkit that have accumulated as technical debt. The repair plan systematically fixes security vulnerabilities (exposed credentials), logic correctness errors (return contract mismatches, validation fallbacks), state consistency problems (missing file locks, fragile metadata collection), and documentation drift (README/PRD misalignment with actual code).

The fixes are prioritized into three tiers:
- **P0 (Critical)**: Security issues requiring immediate action to prevent credential exposure
- **P1 (High)**: Critical logic errors and state consistency issues affecting reliability and data integrity
- **P2 (Medium)**: Documentation drift and structural debt affecting maintainability and user experience

The systematic repair will restore code integrity, eliminate data corruption risks, and align documentation with actual capabilities.

## Glossary

- **Bug_Condition (C)**: The condition that triggers each specific bug - varies by issue category (security exposure, logic error, state corruption, documentation drift)
- **Property (P)**: The desired behavior when the bug condition is fixed - secure credentials, correct return handling, atomic writes, accurate documentation
- **Preservation**: Existing functionality that must remain unchanged - core data collection, authentication, progress tracking, CLI interface
- **FileLock**: Cross-platform file locking mechanism in `lib/io_utils.py` that prevents concurrent write corruption
- **safe_json_write**: Atomic JSON write function using temp file + rename pattern to prevent corruption
- **MediaDownloader.download_all()**: Function in `lib/download_media.py` that returns dict with success/partial_success keys
- **InstagramProcessor**: Batch processor in `lib/parallel_processor.py` that orchestrates account rotation and operations
- **ProgressManager**: Progress tracking system in `lib/progress_manager.py` that manages operation resumption
- **ArchiveRetentionManager**: Archive cleanup system in `lib/archive_manager.py` that enforces retention policies

## Bug Details

### Bug Condition

The bugs manifest across multiple categories when specific conditions are met. The system exhibits defects in security, logic correctness, state consistency, and documentation accuracy.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type SystemState
  OUTPUT: boolean
  
  RETURN (
    // P0: Security Issues
    (input.envFile.containsRealCredentials() OR
     input.sessionsDir.containsRealSessions() OR
     NOT input.gitignore.properlyExcludes(['.env', 'sessions/'])) OR
    
    // P1: Logic Errors
    (input.callerCode.treatsDownloadResultAsBoolean() OR
     input.loginValidation.fallsBackToAssumingValid() OR
     input.archiveCleanup.usesInconsistentPaths()) OR
    
    // P1: State Consistency
    (input.jsonWrites.missingFileLock() IN [
       'collect_relationships.py',
       'profile_access_tracker.py',
       'user_metadata_manager.py',
       'account_cooldown.py'
     ] OR
     input.metadataCollection.usesFragileStrategy() OR
     input.deduplication.usesLinearSearch()) OR
    
    // P2: Documentation Drift
    (input.documentation.missingCommands() OR
     input.documentation.containsInaccurateClaims() OR
     input.documentation.missingLimitations())
  )
END FUNCTION
```

### Examples

**P0 Security Issues:**
- `.env` file contains `INSTA_ACCOUNT_1_PASS=actual_password_123` instead of placeholder
- `sessions/` directory contains real session files committed to repository
- `.gitignore` missing proper exclusion patterns for sensitive files
- No documentation warning users to rotate exposed passwords

**P1 Logic Errors:**
- `InstagramProcessor.download_media()` calls `downloader.download_all()` and treats result as boolean: `if result:` when result is `{'success': False, 'partial_success': True, ...}`
- `InstagramAccountManager.login()` catches validation exception and falls back to "assuming valid" instead of failing
- `ProgressManager.cleanup_progress()` uses `progress_{base}_{timestamp}.archive` while `ArchiveRetentionManager.cleanup_by_count()` searches for `progress_*.archive` pattern mismatch

**P1 State Consistency:**
- `collect_relationships.py` calls `safe_json_write(RELATIONSHIPS_FILE, self.relationships)` without FileLock wrapper, risking corruption during concurrent access
- `UserMetadataManager.update_profile()` tries `mediacount` as proxy for followers, then `_metadata` with unclear fallback, resulting in inaccurate counts
- `RelationshipCollector.collect_for_user()` uses `any(r['source'] == username and r['target'] == follower_username ...)` for each relationship, causing O(n²) performance

**P2 Documentation Drift:**
- README.md missing `analyze-profiles` command that exists in main.py
- PRD.md claims "Production Ready" status but codebase has critical bugs
- Web dashboard documentation doesn't mention lack of authentication or CORS configuration

### Expected Behavior

**P0 Security Fixes:**
- `.env` SHALL contain only placeholder credentials with clear warnings
- `sessions/` directory SHALL be empty in repository
- `.gitignore` SHALL properly exclude `.env` and `sessions/`
- Documentation SHALL include external action requirement to rotate exposed passwords

**P1 Logic Error Fixes:**
- `MediaDownloader.download_all()` callers SHALL correctly interpret dict structure with success/partial_success keys
- `InstagramAccountManager.login()` SHALL NOT fall back to "assuming valid" when validation fails
- Archive cleanup SHALL use consistent directory paths, file naming patterns, and glob patterns

**P1 State Consistency Fixes:**
- All JSON writes to critical files SHALL use FileLock wrapper for atomic writes
- `UserMetadataManager.update_profile()` SHALL use clear, documented strategy to extract follower/following counts
- `RelationshipCollector.collect_for_user()` SHALL use set-based approach for O(1) deduplication lookup

**P2 Documentation Updates:**
- README.md SHALL document all commands, flags, parameters, limitations, and security warnings
- PRD.md SHALL accurately describe production status, realistic limits, and actual architecture
- Web dashboard documentation SHALL describe security implications and rendering limits

## Hypothesized Root Cause

Based on the bug analysis, the most likely root causes are:

### 1. **Security Issues (P0)**
   - **Credential Exposure**: Real credentials were added to `.env` during development/testing and never replaced with placeholders before committing
   - **Session File Exposure**: Real session files were generated during testing and committed to repository
   - **Incomplete .gitignore**: `.gitignore` was not properly configured to exclude sensitive files from version control
   - **Missing Documentation**: No security warnings or password rotation instructions were added to documentation

### 2. **Logic Errors (P1)**
   - **Return Contract Mismatch**: `MediaDownloader.download_all()` was refactored to return detailed dict structure, but callers were not updated to handle new format
   - **Validation Fallback**: Login validation was added with exception handling, but fallback logic assumes session is valid instead of failing safely
   - **Archive Path Inconsistency**: `ProgressManager` and `ArchiveRetentionManager` were developed separately with different naming conventions, causing cleanup to fail

### 3. **State Consistency Issues (P1)**
   - **Missing File Locks**: FileLock was added to some modules but not systematically applied to all critical JSON writes
   - **Fragile Metadata Collection**: Metadata collection uses trial-and-error approach without clear fallback strategy or error handling
   - **Linear Deduplication**: Deduplication uses simple `any()` iteration without considering performance implications for large datasets

### 4. **Documentation Drift (P2)**
   - **Missing Commands**: New commands were added to CLI without updating README.md
   - **Inaccurate Claims**: PRD.md was written optimistically before all features were fully tested and hardened
   - **Missing Limitations**: Web dashboard was added without documenting security implications or rendering limits

## Correctness Properties

Property 1: Bug Condition - Security Credential Exposure

_For any_ system state where `.env` contains real credentials OR `sessions/` contains real session files OR `.gitignore` does not properly exclude sensitive files, the fixed system SHALL contain only placeholder credentials in `.env`, empty `sessions/` directory in repository, proper `.gitignore` exclusions, and documentation warning about password rotation.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4**

Property 2: Bug Condition - Logic Error Return Contract

_For any_ code path where `MediaDownloader.download_all()` is called, the fixed system SHALL correctly interpret the dict structure with success/partial_success keys instead of treating the result as a boolean, ensuring accurate download status propagation.

**Validates: Requirements 2.1**

Property 3: Bug Condition - Logic Error Validation Fallback

_For any_ authentication flow where session validation fails, the fixed system SHALL NOT fall back to "assuming valid" behavior, ensuring authentication failures are properly detected and reported.

**Validates: Requirements 2.2**

Property 4: Bug Condition - Logic Error Archive Cleanup

_For any_ archive cleanup operation, the fixed system SHALL use consistent directory paths, file naming patterns, and glob patterns between `ProgressManager` and `ArchiveRetentionManager`, ensuring retention policy works correctly.

**Validates: Requirements 2.3**

Property 5: Bug Condition - State Consistency File Locking

_For any_ JSON write operation to critical files (relationships.json, profile_access.json, user_profiles.json, account_cooldowns.json, account_quotas.json), the fixed system SHALL use FileLock wrapper for atomic writes to prevent data corruption.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

Property 6: Bug Condition - State Consistency Metadata Collection

_For any_ profile metadata collection operation, the fixed system SHALL use a clear, documented strategy to extract follower/following counts from profile objects with explicit fallback behavior.

**Validates: Requirements 3.5**

Property 7: Bug Condition - State Consistency Deduplication Performance

_For any_ relationship deduplication operation, the fixed system SHALL use set-based or dict-based approach to achieve O(1) lookup performance instead of O(n²) linear search.

**Validates: Requirements 3.6**

Property 8: Bug Condition - Documentation Drift Commands

_For any_ user reading README.md, the fixed system SHALL document all commands (analyze-profiles), flags (--seed-mutual, --min-mutual), parameters, limitations, and security warnings.

**Validates: Requirements 4.1**

Property 9: Bug Condition - Documentation Drift Accuracy

_For any_ user reading PRD.md, the fixed system SHALL accurately describe production status, realistic user limits based on quotas, and actual command architecture without overstated claims.

**Validates: Requirements 4.2**

Property 10: Bug Condition - Documentation Drift Web Dashboard

_For any_ user accessing web dashboard documentation, the fixed system SHALL document lack of access restrictions, CORS configuration, and rendering limits for large graphs.

**Validates: Requirements 4.3**

Property 11: Preservation - Core Functionality

_For any_ spider operation, download operation, account rotation, or progress tracking, the fixed system SHALL produce exactly the same behavior as the original system, preserving all core data collection and resilience features.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 4.3, 5.1, 5.2, 5.3**

## Fix Implementation

### Changes Required

The fixes are organized by priority tier and category:

### P0: Security Fixes (CRITICAL - Immediate Action Required)

**File**: `.env`

**Changes**:
1. **Replace Real Credentials with Placeholders**:
   - Replace all real Instagram usernames with `your_username`, `other_username`
   - Replace all real passwords with `your_password`, `other_password`
   - Add comment header: `# WARNING: Never commit real credentials to version control`
   - Add comment: `# Replace placeholders with actual credentials locally only`

**File**: `sessions/`

**Changes**:
1. **Remove Real Session Files**:
   - Delete all session files from `sessions/` directory
   - Add `.gitkeep` file to preserve directory structure
   - Verify `.gitignore` excludes `sessions/*` except `.gitkeep`

**File**: `.gitignore`

**Changes**:
1. **Verify Exclusion Patterns**:
   - Ensure `.env` is excluded
   - Ensure `sessions/*` is excluded (except `.gitkeep`)
   - Add `!sessions/.gitkeep` to preserve directory

**File**: `README.md`

**Changes**:
1. **Add Security Warning Section**:
   - Add "## Security Notice" section after "Installation"
   - Document: "If you previously committed real credentials to this repository, you MUST rotate all exposed account passwords immediately"
   - Document: "Never commit `.env` file or `sessions/` directory contents to version control"
   - Document: "Use strong, unique passwords for Instagram accounts"

### P1: Logic Error Fixes (HIGH Priority)

**File**: `lib/download_media.py`

**Function**: `MediaDownloader.download_all()`

**Changes**:
1. **Document Return Contract**:
   - Add docstring clearly documenting return structure: `{'success': bool, 'partial_success': bool, 'success_count': int, 'total_count': int, 'results': dict}`
   - Add comment explaining: "Returns dict with success/partial_success keys - callers must check dict structure, not truthiness"

**File**: `lib/parallel_processor.py`

**Function**: `InstagramProcessor.download_media()`

**Changes**:
1. **Fix Return Value Handling**:
   - Change `return result.get('success') or result.get('partial_success', False)` to explicitly check dict keys
   - Update logic to handle partial success correctly
   - Add comment: "download_all() returns dict - check success/partial_success keys explicitly"

**File**: `main.py`

**Function**: `main()` download command handler

**Changes**:
1. **Fix Return Value Handling**:
   - Change `success = download_result['success'] or download_result['partial_success']` to explicitly check dict structure
   - Add comment explaining dict structure

**File**: `lib/account_manager.py`

**Function**: `InstagramAccountManager.login()`

**Changes**:
1. **Remove "Assuming Valid" Fallback**:
   - Remove fallback logic that assumes session is valid when validation fails
   - Change validation exception handling to properly fail and return False
   - Add comment: "Session validation failure means authentication failed - do not assume valid"
   - Ensure failed validation triggers re-authentication or returns False

**File**: `lib/progress_manager.py`

**Function**: `ProgressManager.cleanup_progress()`

**Changes**:
1. **Unify Archive Naming Pattern**:
   - Change archive naming from `progress_{base}_{timestamp}.archive` to `{base}_{timestamp}.archive`
   - Change batch archive naming from `batch_{base}_{timestamp}.archive` to `{base}_{timestamp}.archive`
   - Update directory path to use `archived_logs/` consistently
   - Add comment: "Archive naming must match ArchiveRetentionManager glob patterns"

**File**: `lib/archive_manager.py`

**Function**: `ArchiveRetentionManager.cleanup_by_count()`

**Changes**:
1. **Unify Glob Pattern**:
   - Change glob pattern from `{file_type}_*.archive` to `*_{file_type}_*.archive` or adjust to match ProgressManager naming
   - Ensure pattern matches actual archive file names created by ProgressManager
   - Add comment: "Glob pattern must match ProgressManager archive naming convention"

### P1: State Consistency Fixes (HIGH Priority)

**File**: `lib/collect_relationships.py`

**Function**: `RelationshipCollector._save_relationships()`

**Changes**:
1. **Add FileLock Wrapper**:
   - Import FileLock from io_utils
   - Wrap `safe_json_write()` call with `with FileLock(RELATIONSHIPS_FILE, timeout=10):`
   - Add comment: "FileLock prevents corruption during concurrent access"

**File**: `lib/profile_access_tracker.py`

**Function**: `ProfileAccessTracker.save_access_data()`

**Changes**:
1. **Add FileLock Wrapper**:
   - Wrap `safe_json_write()` call with `with FileLock(self.access_file, timeout=10):`
   - Add comment: "FileLock prevents corruption during concurrent access"

**File**: `lib/user_metadata_manager.py`

**Function**: `UserMetadataManager._save_metadata()`

**Changes**:
1. **Add FileLock Wrapper**:
   - Wrap `safe_json_write()` call with `with FileLock(self.metadata_file, timeout=10):`
   - Add comment: "FileLock prevents corruption during concurrent access"

**File**: `lib/account_cooldown.py`

**Function**: `AccountCooldownManager._save()` and `AccountQuotaManager._save()`

**Changes**:
1. **Add FileLock Wrapper**:
   - Both functions already use FileLock - verify implementation is correct
   - Ensure timeout is set to 10 seconds
   - Add comment if missing: "FileLock prevents corruption during concurrent access"

**File**: `lib/user_metadata_manager.py`

**Function**: `UserMetadataManager.update_profile()`

**Changes**:
1. **Fix Metadata Collection Strategy**:
   - Replace fragile mediacount proxy with direct access to profile._metadata
   - Use clear fallback chain: `profile._metadata.get('edge_followed_by', {}).get('count', 0)`
   - Add try-except with explicit error handling
   - Add comment documenting the strategy: "Extract follower count from _metadata dict, fallback to 0 if unavailable"
   - Document that mediacount is NOT a proxy for followers
   - Add logging when fallback is used

**File**: `lib/collect_relationships.py`

**Function**: `RelationshipCollector.collect_for_user()`

**Changes**:
1. **Optimize Deduplication with Set-Based Approach**:
   - Before loop, build set of existing relationship keys: `existing_keys = {(r['source'], r['target'], r['type']) for r in self.relationships}`
   - In loop, check: `if (username, follower_username, 'followers') not in existing_keys:`
   - Add to set when adding to list: `existing_keys.add((username, follower_username, 'followers'))`
   - Add comment: "Set-based deduplication provides O(1) lookup instead of O(n²) linear search"

### P2: Documentation Updates (MEDIUM Priority)

**File**: `README.md`

**Changes**:
1. **Add Missing Commands**:
   - Add `analyze-profiles` command to command reference table
   - Document `--seed-mutual` and `--min-mutual` flags for spider command
   - Add examples showing mutual connection filtering

2. **Update Anti-Detection Parameters**:
   - Correct rate limiting parameters in configuration section
   - Document actual cooldown behavior (15 minutes default)
   - Document actual quota limits (180 profile views, 6000 actions per day)

3. **Add Account Manager Limitations**:
   - Document 2FA requirement for some accounts
   - Document session expiry behavior
   - Document browser cookie import limitations

4. **Add Security Warnings**:
   - Add "## Security Best Practices" section
   - Document credential rotation requirements
   - Document session file security implications
   - Document proxy configuration security

**File**: `PRD.md`

**Changes**:
1. **Update Production Status**:
   - Change "Production Ready" to "Active Development" or "Beta"
   - Add "Known Issues" section documenting current limitations
   - Document that some features are experimental

2. **Correct User Limits**:
   - Change "unlimited target users" to "limited by daily quotas (180 profile views per account per day)"
   - Document realistic batch processing limits
   - Document account rotation requirements for large-scale operations

3. **Accurate Command Architecture**:
   - Document actual command dispatch pattern in main.py
   - Note that lib/commands/ modules exist but are not fully integrated
   - Document planned vs. implemented architecture

**File**: `README.md` (Web Dashboard Section)

**Changes**:
1. **Add Web Dashboard Limitations**:
   - Add "## Web Dashboard Limitations" subsection
   - Document: "No authentication - dashboard is accessible to anyone on the network"
   - Document: "CORS is configured to allow all origins (*) - not suitable for production"
   - Document: "No rendering limits - large datasets may cause browser performance issues"
   - Document: "Recommended for local development only"

### P2: Structural Improvements (MEDIUM Priority)

**File**: `main.py`

**Changes**:
1. **Document Command Dispatch Pattern**:
   - Add comment at top of main() function: "TODO: Refactor to use lib/commands/ module architecture"
   - Add comment: "Current implementation uses monolithic dispatcher pattern"
   - Add comment: "lib/commands/ modules exist but are not fully integrated"
   - No code changes required - documentation only

**File**: `web/server.py`

**Changes**:
1. **Document Security Implications**:
   - Add docstring to `run_dashboard()` function documenting security implications
   - Add comment: "WARNING: Binds to all interfaces (0.0.0.0) with CORS wide open"
   - Add comment: "Suitable for local development only - not production"
   - Add comment: "For production: Add authentication, restrict CORS, use HTTPS"
   - No code changes required - documentation only

**File**: `web/dashboard.js`

**Changes**:
1. **Add Rendering Limits**:
   - Add check before rendering network graph: `if (nodes.length > 1000) { alert('Too many nodes to render'); return; }`
   - Add comment: "Limit rendering to prevent browser performance issues"
   - Add pagination or filtering UI for large datasets (optional enhancement)

## Testing Strategy

### Validation Approach

The testing strategy follows a three-phase approach:
1. **Exploratory Bug Condition Checking**: Surface counterexamples demonstrating bugs BEFORE implementing fixes
2. **Fix Checking**: Verify fixes work correctly for all bug conditions
3. **Preservation Checking**: Verify existing behavior is unchanged for all non-buggy inputs

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate bugs BEFORE implementing fixes. Confirm or refute root cause analysis.

**Test Plan**: Write tests that check for each bug condition and assert expected failures on UNFIXED code.

**Test Cases**:

1. **P0 Security - Credential Exposure Test**:
   - Read `.env` file and assert it contains real credentials (will pass on unfixed code)
   - Check `sessions/` directory for real session files (will pass on unfixed code)
   - Verify `.gitignore` excludes sensitive files (will fail on unfixed code if missing)

2. **P1 Logic - Return Contract Test**:
   - Call `MediaDownloader.download_all()` and check if result is treated as boolean in callers (will fail on unfixed code)
   - Mock download_all to return `{'success': False, 'partial_success': True}` and verify caller behavior

3. **P1 Logic - Validation Fallback Test**:
   - Mock session validation to raise exception and verify login() falls back to "assuming valid" (will pass on unfixed code)
   - Check if failed validation triggers re-authentication (will fail on unfixed code)

4. **P1 Logic - Archive Cleanup Test**:
   - Create archive files with ProgressManager naming convention
   - Run ArchiveRetentionManager cleanup and verify files are found (will fail on unfixed code due to pattern mismatch)

5. **P1 State - File Lock Test**:
   - Check if `collect_relationships.py` uses FileLock for JSON writes (will fail on unfixed code)
   - Check if `profile_access_tracker.py` uses FileLock (will fail on unfixed code)
   - Check if `user_metadata_manager.py` uses FileLock (will fail on unfixed code)

6. **P1 State - Metadata Collection Test**:
   - Mock profile object with missing `_metadata` and verify fallback behavior (will fail on unfixed code with unclear error)
   - Check if mediacount is used as proxy for followers (will pass on unfixed code - incorrect behavior)

7. **P1 State - Deduplication Performance Test**:
   - Create large relationship dataset (10,000 relationships)
   - Measure deduplication time and verify O(n²) behavior (will pass on unfixed code - slow performance)

8. **P2 Documentation - Missing Commands Test**:
   - Parse README.md and verify `analyze-profiles` command is documented (will fail on unfixed code)
   - Verify `--seed-mutual` and `--min-mutual` flags are documented (will fail on unfixed code)

**Expected Counterexamples**:
- `.env` contains real credentials instead of placeholders
- `MediaDownloader.download_all()` result is treated as boolean, causing incorrect success detection
- Session validation failure falls back to "assuming valid" instead of failing
- Archive cleanup fails to find files due to pattern mismatch
- JSON writes occur without FileLock, risking corruption
- Metadata collection uses fragile strategy with unclear fallbacks
- Deduplication uses O(n²) linear search instead of O(1) set lookup
- Documentation missing commands, flags, and limitations

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed system produces the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := fixedSystem(input)
  ASSERT expectedBehavior(result)
END FOR
```

**Test Cases**:

1. **P0 Security Fixes**:
   - Verify `.env` contains only placeholders
   - Verify `sessions/` directory is empty in repository
   - Verify `.gitignore` properly excludes sensitive files
   - Verify README.md contains password rotation warning

2. **P1 Logic Fixes**:
   - Verify `MediaDownloader.download_all()` callers correctly interpret dict structure
   - Verify login validation failure does not fall back to "assuming valid"
   - Verify archive cleanup uses consistent naming patterns

3. **P1 State Fixes**:
   - Verify all critical JSON writes use FileLock wrapper
   - Verify metadata collection uses clear, documented strategy
   - Verify deduplication uses set-based approach with O(1) lookup

4. **P2 Documentation Fixes**:
   - Verify README.md documents all commands, flags, and limitations
   - Verify PRD.md accurately describes production status and limits
   - Verify web dashboard documentation includes security warnings

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed system produces the same result as the original system.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT originalSystem(input) = fixedSystem(input)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many test cases automatically across the input domain
- It catches edge cases that manual unit tests might miss
- It provides strong guarantees that behavior is unchanged for all non-buggy inputs

**Test Plan**: Observe behavior on UNFIXED code first for non-bug scenarios, then write property-based tests capturing that behavior.

**Test Cases**:

1. **Core Functionality Preservation**:
   - Verify spider operations collect relationships correctly
   - Verify download operations retrieve media successfully
   - Verify account rotation switches accounts on rate limits
   - Verify progress tracking enables resumption

2. **Authentication Preservation**:
   - Verify valid sessions restore successfully
   - Verify 2FA prompts for OTP codes interactively
   - Verify browser cookies import sessions correctly

3. **Data Integrity Preservation**:
   - Verify safe_json_write performs atomic writes
   - Verify FileLock provides cross-platform locking
   - Verify retry_with_backoff provides exponential backoff

4. **Rate Limiting Preservation**:
   - Verify RateLimiter enforces delays and breaks
   - Verify AccountQuotaManager enforces daily quotas
   - Verify AccountCooldownManager manages cooldowns

5. **CLI Preservation**:
   - Verify all CLI commands work with --help
   - Verify batch files provide Windows menu interface
   - Verify web dashboard serves files and API endpoints

6. **Testing Infrastructure Preservation**:
   - Verify pytest runs unit tests without API calls
   - Verify pytest runs integration tests with --run-integration
   - Verify test suite reports 465 test cases

### Unit Tests

- Test credential placeholder replacement in `.env`
- Test return value handling for `MediaDownloader.download_all()`
- Test login validation failure handling
- Test archive naming pattern consistency
- Test FileLock wrapper for all critical JSON writes
- Test metadata collection fallback strategy
- Test set-based deduplication performance
- Test documentation completeness

### Property-Based Tests

- Generate random system states and verify security fixes prevent credential exposure
- Generate random download results and verify callers handle dict structure correctly
- Generate random authentication scenarios and verify validation failures are handled properly
- Generate random archive cleanup scenarios and verify retention policy works correctly
- Generate random concurrent write scenarios and verify FileLock prevents corruption
- Generate random profile objects and verify metadata collection handles all cases
- Generate random relationship datasets and verify deduplication performance is O(1)

### Integration Tests

- Test full security audit workflow (check .env, sessions/, .gitignore)
- Test full download workflow with return value handling
- Test full authentication workflow with validation failure scenarios
- Test full archive cleanup workflow with retention policy
- Test full concurrent write workflow with FileLock protection
- Test full metadata collection workflow with various profile types
- Test full relationship collection workflow with large datasets
- Test full documentation verification workflow
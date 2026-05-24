# Implementation Tasks: Code Repair and Alignment

## Task Overview

This document outlines the implementation tasks for fixing multiple critical issues across the Instagram data collection toolkit. Tasks are organized by priority (P0 → P1 → P2) following the bugfix workflow methodology with exploration tests, implementation fixes, and preservation tests.

## P0: Security Fixes (CRITICAL - Immediate Action)

### Task 1: P0 Security - Write Exploration Tests for Credential Exposure

Write property-based tests that demonstrate credential exposure bugs BEFORE implementing fixes.

**Test Files**: `tests/test_security_audit.py` (new file)

**Sub-tasks**:
- [x] 1.1 Write test to verify .env contains real credentials (should pass on unfixed code)
- [x] 1.2 Write test to check sessions/ directory for real session files (should pass on unfixed code)
- [x] 1.3 Write test to verify .gitignore excludes sensitive files (should fail on unfixed code if missing)
- [x] 1.4 Run tests and document counterexamples found

**Validates**: Property 1 (Bug Condition - Security Credential Exposure)

**Expected Outcome**: Tests should reveal real credentials in .env, real session files in sessions/, and potentially missing .gitignore patterns.

---

### Task 2: P0 Security - Clear Sensitive Information from Repository

Remove all real credentials and session files from the repository.

**Files**: `.env`, `sessions/`, `.gitignore`

**Sub-tasks**:
- [x] 2.1 Replace all real Instagram usernames in .env with placeholders (your_username, other_username)
- [x] 2.2 Replace all real passwords in .env with placeholders (your_password, other_password)
- [x] 2.3 Add warning comment header to .env: "# WARNING: Never commit real credentials to version control"
- [x] 2.4 Add comment to .env: "# Replace placeholders with actual credentials locally only"
- [x] 2.5 Delete all session files from sessions/ directory
- [x] 2.6 Add .gitkeep file to sessions/ to preserve directory structure
- [x] 2.7 Verify .gitignore excludes .env
- [x] 2.8 Verify .gitignore excludes sessions/* (except .gitkeep)
- [x] 2.9 Add !sessions/.gitkeep to .gitignore if needed

**Validates**: Property 1 (Bug Condition - Security Credential Exposure)

**Expected Outcome**: Repository contains no real credentials or session files, .gitignore properly configured.

---

### Task 3: P0 Security - Add Security Documentation

Add security warnings and password rotation instructions to documentation.

**Files**: `README.md`

**Sub-tasks**:
- [x] 3.1 Add "## Security Notice" section after "Installation" section
- [x] 3.2 Document: "If you previously committed real credentials to this repository, you MUST rotate all exposed account passwords immediately"
- [x] 3.3 Document: "Never commit .env file or sessions/ directory contents to version control"
- [x] 3.4 Document: "Use strong, unique passwords for Instagram accounts"

**Validates**: Property 1 (Bug Condition - Security Credential Exposure)

**Expected Outcome**: README.md contains clear security warnings and password rotation instructions.

---

### Task 4: P0 Security - Write Fix Checking Tests

Write tests to verify security fixes work correctly.

**Test Files**: `tests/test_security_audit.py`

**Sub-tasks**:
- [x] 4.1 Write test to verify .env contains only placeholders
- [x] 4.2 Write test to verify sessions/ directory is empty in repository (except .gitkeep)
- [x] 4.3 Write test to verify .gitignore properly excludes sensitive files
- [x] 4.4 Write test to verify README.md contains password rotation warning
- [x] 4.5 Run tests and verify all pass

**Validates**: Property 1 (Bug Condition - Security Credential Exposure)

**Expected Outcome**: All security fix tests pass, confirming no credential exposure.

---

## P1: Logic Error Fixes (HIGH Priority)

### Task 5: P1 Logic - Write Exploration Tests for Return Contract Mismatch

Write tests that demonstrate MediaDownloader.download_all() return contract bugs BEFORE implementing fixes.

**Test Files**: `tests/test_download_media.py`, `tests/test_parallel_processor.py`, `tests/test_main.py`

**Sub-tasks**:
- [x] 5.1 Write test mocking download_all() to return {'success': False, 'partial_success': True}
- [x] 5.2 Verify InstagramProcessor.download_media() treats result as boolean (should fail on unfixed code)
- [x] 5.3 Verify main.py download handler treats result as boolean (should fail on unfixed code)
- [x] 5.4 Run tests and document counterexamples found

**Validates**: Property 2 (Bug Condition - Logic Error Return Contract)

**Expected Outcome**: Tests should reveal that callers treat dict result as boolean, causing incorrect success detection.

---

### Task 6: P1 Logic - Fix MediaDownloader Return Contract

Fix MediaDownloader.download_all() return contract and update all callers.

**Files**: `lib/download_media.py`, `lib/parallel_processor.py`, `main.py`

**Sub-tasks**:
- [x] 6.1 Add docstring to MediaDownloader.download_all() documenting return structure: {'success': bool, 'partial_success': bool, 'success_count': int, 'total_count': int, 'results': dict}
- [x] 6.2 Add comment: "Returns dict with success/partial_success keys - callers must check dict structure, not truthiness"
- [x] 6.3 Fix InstagramProcessor.download_media() to explicitly check result.get('success') or result.get('partial_success', False)
- [x] 6.4 Add comment: "download_all() returns dict - check success/partial_success keys explicitly"
- [x] 6.5 Fix main.py download handler to explicitly check download_result['success'] or download_result['partial_success']
- [x] 6.6 Add comment explaining dict structure

**Validates**: Property 2 (Bug Condition - Logic Error Return Contract)

**Expected Outcome**: All callers correctly interpret dict structure, download status propagated accurately.

---

### Task 7: P1 Logic - Write Exploration Tests for Session Validation Fallback

Write tests that demonstrate InstagramAccountManager.login() validation fallback bugs BEFORE implementing fixes.

**Test Files**: `tests/test_account_manager.py`

**Sub-tasks**:
- [x] 7.1 Write test mocking session validation to raise exception
- [x] 7.2 Verify login() falls back to "assuming valid" (should pass on unfixed code - incorrect behavior)
- [x] 7.3 Verify failed validation does not trigger re-authentication (should fail on unfixed code)
- [x] 7.4 Run tests and document counterexamples found

**Validates**: Property 3 (Bug Condition - Logic Error Validation Fallback)

**Expected Outcome**: Tests should reveal that validation failures fall back to "assuming valid" instead of failing properly.

---

### Task 8: P1 Logic - Remove Session Validation Fallback

Remove "assuming valid" fallback from InstagramAccountManager.login().

**Files**: `lib/account_manager.py`

**Sub-tasks**:
- [x] 8.1 Remove fallback logic that assumes session is valid when validation fails
- [x] 8.2 Change validation exception handling to properly fail and return False
- [x] 8.3 Add comment: "Session validation failure means authentication failed - do not assume valid"
- [x] 8.4 Ensure failed validation triggers re-authentication or returns False

**Validates**: Property 3 (Bug Condition - Logic Error Validation Fallback)

**Expected Outcome**: Authentication failures properly detected and reported, no false successes.

---

### Task 9: P1 Logic - Write Exploration Tests for Archive Cleanup Inconsistency

Write tests that demonstrate archive cleanup path/naming bugs BEFORE implementing fixes.

**Test Files**: `tests/test_progress_manager.py`, `tests/test_archive_manager.py`

**Sub-tasks**:
- [x] 9.1 Create archive files with ProgressManager naming convention
- [x] 9.2 Run ArchiveRetentionManager cleanup and verify files are found (should fail on unfixed code due to pattern mismatch)
- [x] 9.3 Document counterexamples showing pattern mismatch

**Validates**: Property 4 (Bug Condition - Logic Error Archive Cleanup)

**Expected Outcome**: Tests should reveal that archive cleanup fails to find files due to inconsistent naming patterns.

---

### Task 10: P1 Logic - Unify Archive Cleanup Paths and Naming

Unify archive directory paths and naming patterns between ProgressManager and ArchiveRetentionManager.

**Files**: `lib/progress_manager.py`, `lib/archive_manager.py`

**Sub-tasks**:
- [x] 10.1 Change ProgressManager.cleanup_progress() archive naming from progress_{base}_{timestamp}.archive to {base}_{timestamp}.archive
- [x] 10.2 Change batch archive naming from batch_{base}_{timestamp}.archive to {base}_{timestamp}.archive
- [x] 10.3 Update directory path to use archived_logs/ consistently
- [x] 10.4 Add comment: "Archive naming must match ArchiveRetentionManager glob patterns"
- [x] 10.5 Change ArchiveRetentionManager.cleanup_by_count() glob pattern to match ProgressManager naming
- [x] 10.6 Add comment: "Glob pattern must match ProgressManager archive naming convention"

**Validates**: Property 4 (Bug Condition - Logic Error Archive Cleanup)

**Expected Outcome**: Archive cleanup uses consistent paths and patterns, retention policy works correctly.

---

## P1: State Consistency Fixes (HIGH Priority)

### Task 11: P1 State - Write Exploration Tests for Missing File Locks

Write tests that demonstrate missing FileLock bugs BEFORE implementing fixes.

**Test Files**: `tests/test_collect_relationships.py`, `tests/test_profile_access_tracker.py`, `tests/test_user_metadata_manager.py`, `tests/test_account_cooldown.py`

**Sub-tasks**:
- [x] 11.1 Check if collect_relationships.py uses FileLock for JSON writes (should fail on unfixed code)
- [x] 11.2 Check if profile_access_tracker.py uses FileLock (should fail on unfixed code)
- [x] 11.3 Check if user_metadata_manager.py uses FileLock (should fail on unfixed code)
- [x] 11.4 Verify account_cooldown.py already uses FileLock correctly
- [x] 11.5 Document counterexamples showing missing locks

**Validates**: Property 5 (Bug Condition - State Consistency File Locking)

**Expected Outcome**: Tests should reveal that critical JSON writes occur without FileLock, risking corruption.

---

### Task 12: P1 State - Add FileLock to Critical JSON Writes

Add FileLock wrapper to all critical JSON write operations.

**Files**: `lib/collect_relationships.py`, `lib/profile_access_tracker.py`, `lib/user_metadata_manager.py`, `lib/account_cooldown.py`

**Sub-tasks**:
- [x] 12.1 Import FileLock from io_utils in collect_relationships.py
- [x] 12.2 Wrap safe_json_write() call in RelationshipCollector._save_relationships() with FileLock(RELATIONSHIPS_FILE, timeout=10)
- [x] 12.3 Add comment: "FileLock prevents corruption during concurrent access"
- [x] 12.4 Wrap safe_json_write() call in ProfileAccessTracker.save_access_data() with FileLock(self.access_file, timeout=10)
- [x] 12.5 Add comment: "FileLock prevents corruption during concurrent access"
- [x] 12.6 Wrap safe_json_write() call in UserMetadataManager._save_metadata() with FileLock(self.metadata_file, timeout=10)
- [x] 12.7 Add comment: "FileLock prevents corruption during concurrent access"
- [x] 12.8 Verify AccountCooldownManager._save() and AccountQuotaManager._save() use FileLock correctly
- [x] 12.9 Ensure timeout is set to 10 seconds
- [x] 12.10 Add comment if missing: "FileLock prevents corruption during concurrent access"

**Validates**: Property 5 (Bug Condition - State Consistency File Locking)

**Expected Outcome**: All critical JSON writes use FileLock, preventing data corruption during concurrent access.

---

### Task 13: P1 State - Write Exploration Tests for Fragile Metadata Collection

Write tests that demonstrate metadata collection bugs BEFORE implementing fixes.

**Test Files**: `tests/test_user_metadata_manager.py`

**Sub-tasks**:
- [x] 13.1 Mock profile object with missing _metadata and verify fallback behavior (should fail on unfixed code with unclear error)
- [x] 13.2 Check if mediacount is used as proxy for followers (should pass on unfixed code - incorrect behavior)
- [x] 13.3 Document counterexamples showing fragile strategy

**Validates**: Property 6 (Bug Condition - State Consistency Metadata Collection)

**Expected Outcome**: Tests should reveal that metadata collection uses fragile strategy with unclear fallbacks.

---

### Task 14: P1 State - Fix Metadata Collection Strategy

Fix UserMetadataManager.update_profile() to use clear, documented metadata collection strategy.

**Files**: `lib/user_metadata_manager.py`

**Sub-tasks**:
- [x] 14.1 Replace fragile mediacount proxy with direct access to profile._metadata
- [x] 14.2 Use clear fallback chain: profile._metadata.get('edge_followed_by', {}).get('count', 0)
- [x] 14.3 Add try-except with explicit error handling
- [x] 14.4 Add comment: "Extract follower count from _metadata dict, fallback to 0 if unavailable"
- [x] 14.5 Document that mediacount is NOT a proxy for followers
- [x] 14.6 Add logging when fallback is used

**Validates**: Property 6 (Bug Condition - State Consistency Metadata Collection)

**Expected Outcome**: Metadata collection uses clear, documented strategy with explicit fallback behavior.

---

### Task 15: P1 State - Write Exploration Tests for Linear Deduplication

Write tests that demonstrate deduplication performance bugs BEFORE implementing fixes.

**Test Files**: `tests/test_collect_relationships.py`

**Sub-tasks**:
- [x] 15.1 Create large relationship dataset (10,000 relationships)
- [x] 15.2 Measure deduplication time and verify O(n²) behavior (should pass on unfixed code - slow performance)
- [x] 15.3 Document counterexamples showing linear search performance

**Validates**: Property 7 (Bug Condition - State Consistency Deduplication Performance)

**Expected Outcome**: Tests should reveal that deduplication uses O(n²) linear search instead of O(1) set lookup.

---

### Task 16: P1 State - Optimize Relationship Deduplication

Optimize RelationshipCollector.collect_for_user() to use set-based deduplication.

**Files**: `lib/collect_relationships.py`

**Sub-tasks**:
- [x] 16.1 Before loop, build set of existing relationship keys: existing_keys = {(r['source'], r['target'], r['type']) for r in self.relationships}
- [x] 16.2 In loop, check: if (username, follower_username, 'followers') not in existing_keys:
- [x] 16.3 Add to set when adding to list: existing_keys.add((username, follower_username, 'followers'))
- [x] 16.4 Add comment: "Set-based deduplication provides O(1) lookup instead of O(n²) linear search"

**Validates**: Property 7 (Bug Condition - State Consistency Deduplication Performance)

**Expected Outcome**: Deduplication uses set-based approach with O(1) lookup performance.

---

## P1: Write Preservation Tests for Logic and State Fixes

### Task 17: P1 Preservation - Write Preservation Tests for Core Functionality

Write property-based tests to verify existing behavior is unchanged for all non-buggy inputs.

**Test Files**: `tests/test_preservation_core.py` (new file)

**Sub-tasks**:
- [ ] 17.1 Write property test: spider operations collect relationships correctly
- [ ] 17.2 Write property test: download operations retrieve media successfully
- [ ] 17.3 Write property test: account rotation switches accounts on rate limits
- [ ] 17.4 Write property test: progress tracking enables resumption
- [ ] 17.5 Run tests and verify all pass

**Validates**: Property 11 (Preservation - Core Functionality)

**Expected Outcome**: All core functionality preservation tests pass, confirming no regressions.

---

### Task 18: P1 Preservation - Write Preservation Tests for Authentication

Write property-based tests to verify authentication behavior is unchanged.

**Test Files**: `tests/test_preservation_auth.py` (new file)

**Sub-tasks**:
- [ ] 18.1 Write property test: valid sessions restore successfully
- [ ] 18.2 Write property test: 2FA prompts for OTP codes interactively
- [ ] 18.3 Write property test: browser cookies import sessions correctly
- [ ] 18.4 Run tests and verify all pass

**Validates**: Property 11 (Preservation - Core Functionality)

**Expected Outcome**: All authentication preservation tests pass, confirming no regressions.

---

### Task 19: P1 Preservation - Write Preservation Tests for Data Integrity

Write property-based tests to verify data integrity mechanisms are unchanged.

**Test Files**: `tests/test_preservation_data.py` (new file)

**Sub-tasks**:
- [ ] 19.1 Write property test: safe_json_write performs atomic writes
- [ ] 19.2 Write property test: FileLock provides cross-platform locking
- [ ] 19.3 Write property test: retry_with_backoff provides exponential backoff
- [ ] 19.4 Run tests and verify all pass

**Validates**: Property 11 (Preservation - Core Functionality)

**Expected Outcome**: All data integrity preservation tests pass, confirming no regressions.

---

### Task 20: P1 Preservation - Write Preservation Tests for Rate Limiting

Write property-based tests to verify rate limiting behavior is unchanged.

**Test Files**: `tests/test_preservation_rate_limiting.py` (new file)

**Sub-tasks**:
- [ ] 20.1 Write property test: RateLimiter enforces delays and breaks
- [ ] 20.2 Write property test: AccountQuotaManager enforces daily quotas
- [ ] 20.3 Write property test: AccountCooldownManager manages cooldowns
- [ ] 20.4 Run tests and verify all pass

**Validates**: Property 11 (Preservation - Core Functionality)

**Expected Outcome**: All rate limiting preservation tests pass, confirming no regressions.

---

## P2: Documentation Updates (MEDIUM Priority)

### Task 21: P2 Documentation - Update README.md with Missing Commands

Add missing commands, flags, and parameters to README.md.

**Files**: `README.md`

**Sub-tasks**:
- [ ] 21.1 Add analyze-profiles command to command reference table
- [ ] 21.2 Document --seed-mutual flag for spider command
- [ ] 21.3 Document --min-mutual flag for spider command
- [ ] 21.4 Add examples showing mutual connection filtering

**Validates**: Property 8 (Bug Condition - Documentation Drift Commands)

**Expected Outcome**: README.md documents all commands and flags accurately.

---

### Task 22: P2 Documentation - Update README.md with Correct Parameters

Update anti-detection parameters and account manager limitations in README.md.

**Files**: `README.md`

**Sub-tasks**:
- [ ] 22.1 Correct rate limiting parameters in configuration section
- [ ] 22.2 Document actual cooldown behavior (15 minutes default)
- [ ] 22.3 Document actual quota limits (180 profile views, 6000 actions per day)
- [ ] 22.4 Document 2FA requirement for some accounts
- [ ] 22.5 Document session expiry behavior
- [ ] 22.6 Document browser cookie import limitations

**Validates**: Property 8 (Bug Condition - Documentation Drift Commands)

**Expected Outcome**: README.md accurately describes all parameters and limitations.

---

### Task 23: P2 Documentation - Add Security Best Practices to README.md

Add security best practices section to README.md.

**Files**: `README.md`

**Sub-tasks**:
- [ ] 23.1 Add "## Security Best Practices" section
- [ ] 23.2 Document credential rotation requirements
- [ ] 23.3 Document session file security implications
- [ ] 23.4 Document proxy configuration security

**Validates**: Property 8 (Bug Condition - Documentation Drift Commands)

**Expected Outcome**: README.md includes comprehensive security best practices.

---

### Task 24: P2 Documentation - Update PRD.md Production Status

Update PRD.md to accurately describe production status and limitations.

**Files**: `PRD.md`

**Sub-tasks**:
- [ ] 24.1 Change "Production Ready" to "Active Development" or "Beta"
- [ ] 24.2 Add "Known Issues" section documenting current limitations
- [ ] 24.3 Document that some features are experimental

**Validates**: Property 9 (Bug Condition - Documentation Drift Accuracy)

**Expected Outcome**: PRD.md accurately describes production status.

---

### Task 25: P2 Documentation - Update PRD.md User Limits

Update PRD.md to accurately describe user limits and quotas.

**Files**: `PRD.md`

**Sub-tasks**:
- [ ] 25.1 Change "unlimited target users" to "limited by daily quotas (180 profile views per account per day)"
- [ ] 25.2 Document realistic batch processing limits
- [ ] 25.3 Document account rotation requirements for large-scale operations

**Validates**: Property 9 (Bug Condition - Documentation Drift Accuracy)

**Expected Outcome**: PRD.md accurately describes user limits and quotas.

---

### Task 26: P2 Documentation - Update PRD.md Command Architecture

Update PRD.md to accurately describe command architecture.

**Files**: `PRD.md`

**Sub-tasks**:
- [ ] 26.1 Document actual command dispatch pattern in main.py
- [ ] 26.2 Note that lib/commands/ modules exist but are not fully integrated
- [ ] 26.3 Document planned vs. implemented architecture

**Validates**: Property 9 (Bug Condition - Documentation Drift Accuracy)

**Expected Outcome**: PRD.md accurately describes command architecture.

---

### Task 27: P2 Documentation - Add Web Dashboard Limitations to README.md

Add web dashboard limitations section to README.md.

**Files**: `README.md`

**Sub-tasks**:
- [ ] 27.1 Add "## Web Dashboard Limitations" subsection
- [ ] 27.2 Document: "No authentication - dashboard is accessible to anyone on the network"
- [ ] 27.3 Document: "CORS is configured to allow all origins (*) - not suitable for production"
- [ ] 27.4 Document: "No rendering limits - large datasets may cause browser performance issues"
- [ ] 27.5 Document: "Recommended for local development only"

**Validates**: Property 10 (Bug Condition - Documentation Drift Web Dashboard)

**Expected Outcome**: README.md documents web dashboard limitations clearly.

---

## P2: Structural Improvements (MEDIUM Priority)

### Task 28: P2 Structural - Document Command Dispatch Pattern in main.py

Add documentation comments to main.py about command dispatch pattern.

**Files**: `main.py`

**Sub-tasks**:
- [ ] 28.1 Add comment at top of main() function: "TODO: Refactor to use lib/commands/ module architecture"
- [ ] 28.2 Add comment: "Current implementation uses monolithic dispatcher pattern"
- [ ] 28.3 Add comment: "lib/commands/ modules exist but are not fully integrated"

**Validates**: Property 9 (Bug Condition - Documentation Drift Accuracy)

**Expected Outcome**: main.py clearly documents current dispatcher pattern and planned refactoring.

---

### Task 29: P2 Structural - Document Security Implications in web/server.py

Add documentation about security implications to web/server.py.

**Files**: `web/server.py`

**Sub-tasks**:
- [ ] 29.1 Add docstring to run_dashboard() function documenting security implications
- [ ] 29.2 Add comment: "WARNING: Binds to all interfaces (0.0.0.0) with CORS wide open"
- [ ] 29.3 Add comment: "Suitable for local development only - not production"
- [ ] 29.4 Add comment: "For production: Add authentication, restrict CORS, use HTTPS"

**Validates**: Property 10 (Bug Condition - Documentation Drift Web Dashboard)

**Expected Outcome**: web/server.py clearly documents security implications.

---

### Task 30: P2 Structural - Add Rendering Limits to web/dashboard.js

Add rendering limits to prevent browser performance issues with large datasets.

**Files**: `web/dashboard.js`

**Sub-tasks**:
- [ ] 30.1 Add check before rendering network graph: if (nodes.length > 1000) { alert('Too many nodes to render'); return; }
- [ ] 30.2 Add comment: "Limit rendering to prevent browser performance issues"
- [ ] 30.3* Add pagination or filtering UI for large datasets (optional enhancement)

**Validates**: Property 10 (Bug Condition - Documentation Drift Web Dashboard)

**Expected Outcome**: dashboard.js has rendering limits to prevent performance issues.

---

## P2: Write Preservation Tests for Documentation and Structural Changes

### Task 31: P2 Preservation - Write Preservation Tests for CLI Interface

Write property-based tests to verify CLI interface behavior is unchanged.

**Test Files**: `tests/test_preservation_cli.py` (new file)

**Sub-tasks**:
- [ ] 31.1 Write property test: all CLI commands work with --help
- [ ] 31.2 Write property test: batch files provide Windows menu interface
- [ ] 31.3 Write property test: web dashboard serves files and API endpoints
- [ ] 31.4 Run tests and verify all pass

**Validates**: Property 11 (Preservation - Core Functionality)

**Expected Outcome**: All CLI preservation tests pass, confirming no regressions.

---

### Task 32: P2 Preservation - Write Preservation Tests for Testing Infrastructure

Write property-based tests to verify testing infrastructure is unchanged.

**Test Files**: `tests/test_preservation_testing.py` (new file)

**Sub-tasks**:
- [ ] 32.1 Write property test: pytest runs unit tests without API calls
- [ ] 32.2 Write property test: pytest runs integration tests with --run-integration
- [ ] 32.3 Write property test: test suite reports 465 test cases
- [ ] 32.4 Run tests and verify all pass

**Validates**: Property 11 (Preservation - Core Functionality)

**Expected Outcome**: All testing infrastructure preservation tests pass, confirming no regressions.

---

## Final Validation

### Task 33: Run Full Test Suite and Verify All Properties

Run complete test suite to verify all correctness properties and preservation requirements.

**Sub-tasks**:
- [ ] 33.1 Run all exploration tests and verify counterexamples are documented
- [ ] 33.2 Run all fix checking tests and verify all pass
- [ ] 33.3 Run all preservation tests and verify all pass
- [ ] 33.4 Run full pytest suite and verify 465+ test cases pass
- [ ] 33.5 Generate test coverage report
- [ ] 33.6 Document any remaining issues or limitations

**Validates**: All Properties (1-11)

**Expected Outcome**: All tests pass, all properties validated, no regressions detected.

---

## Summary

**Total Tasks**: 33 tasks
**P0 Tasks**: 4 tasks (Security fixes)
**P1 Tasks**: 16 tasks (Logic errors and state consistency)
**P2 Tasks**: 13 tasks (Documentation and structural improvements)

**Execution Order**: P0 → P1 → P2 (by priority)

**Testing Approach**: Exploration → Fix → Preservation for each category

**Completion Criteria**:
- All P0 security issues resolved (no credential exposure)
- All P1 logic errors fixed (correct return handling, validation, archive cleanup)
- All P1 state consistency issues resolved (FileLock, metadata, deduplication)
- All P2 documentation updated (README, PRD, web dashboard)
- All P2 structural improvements documented
- All tests pass (exploration, fix checking, preservation)
- No regressions in existing functionality

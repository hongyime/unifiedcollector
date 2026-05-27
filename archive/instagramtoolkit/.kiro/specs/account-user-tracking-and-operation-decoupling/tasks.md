# Implementation Plan: Account-User Tracking and Operation Decoupling

## Overview

This implementation plan transforms the Instagram scraping toolkit from a flat-file username storage system to an intelligent, structured database with operation-aware account selection and conservative rate limiting. The implementation follows a bottom-up approach: core data structures first, then classification and selection logic, followed by integration with existing components, and finally comprehensive testing.

## Tasks

- [x] 1. Implement core username database with source tracking
  - [x] 1.1 Create UsernameDatabase class with data models
    - Implement UsernameRecord dataclass with all fields (username, source_account, timestamps, metadata, following_status)
    - Implement UsernameDatabase class with in-memory storage and source account indexing
    - Add validation for username format and source account existence
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_
  
  - [x] 1.2 Implement database persistence and atomic writes
    - Add save() method with atomic write using temporary files
    - Add load() method with JSON schema validation and corruption handling
    - Implement file locking to prevent concurrent writes
    - Add backup creation before destructive operations
    - _Requirements: 1.7, 1.8, 8.3, 8.4, 10.3_
  
  - [x] 1.3 Write property tests for username database
    - **Property 1: Username Uniqueness** - Adding same username multiple times results in single record
    - **Property 2: Source Account Validity** - All source accounts exist in configuration
    - **Property 3: Username Format Validation** - Only valid Instagram usernames accepted
    - **Property 4: Timestamp Recording** - Timestamps within 1 second of current time
    - **Property 5: Source Account Query Completeness** - Query returns all and only usernames for that source
    - **Property 6: Database Persistence Round-Trip** - Save and load preserves all data
    - **Validates: Requirements 1.2, 1.5, 1.4, 1.3, 1.6, 1.7**
  
  - [x] 1.4 Write unit tests for username database
    - Test add_username with valid and invalid inputs
    - Test duplicate username rejection
    - Test get_usernames_by_source filtering
    - Test metadata updates and merging
    - Test database corruption recovery
    - _Requirements: 1.1, 1.2, 1.6, 6.2, 8.3, 8.4_

- [x] 2. Implement flat file migration functionality
  - [x] 2.1 Add migration methods to UsernameDatabase
    - Implement migrate_from_flat_file() with backup creation
    - Add validation and statistics tracking (added, skipped, duplicates, invalid)
    - Implement export_to_flat_file() for backward compatibility
    - Add migration conflict resolution (skip duplicates, preserve existing)
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 9.1, 9.2, 9.3_
  
  - [x] 2.2 Write property tests for migration
    - **Property 11: Migration Preservation** - All valid usernames from flat file appear in database
    - **Property 12: Migration Statistics Completeness** - Sum of counts equals total lines
    - **Property 21: Export Format Validity** - Each line has one username, all usernames present
    - **Property 22: Export Ordering Preservation** - Usernames ordered by added_timestamp
    - **Validates: Requirements 5.3, 5.7, 5.6, 9.2, 9.1, 9.3**
  
  - [x] 2.3 Write unit tests for migration
    - Test migration with valid flat file
    - Test migration with invalid usernames
    - Test migration with duplicates
    - Test export to flat file format
    - Test migration conflict resolution
    - _Requirements: 5.3, 5.4, 5.5, 8.7, 9.2, 9.3_

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 4. Implement operation classification system
  - [x] 4.1 Create OperationType enum and OperationMetadata dataclass
    - Define OperationType enum (PUBLIC, FOLLOWING_REQUIRED, MUTUAL_FOLLOWING)
    - Create OperationMetadata dataclass with name, operation_type, rate_limit_weight, description
    - Add validation for rate_limit_weight (1-10 range)
    - _Requirements: 2.1, 2.4, 12.3_
  
  - [x] 4.2 Implement OperationClassifier class
    - Create operation registry in config.py with all supported operations
    - Implement classify() method with safe default (PUBLIC)
    - Add requires_following() and is_public_operation() helper methods
    - Implement get_operation_metadata() for querying operation details
    - Add registry validation on initialization
    - _Requirements: 2.1, 2.2, 2.3, 2.5, 2.6, 12.1, 12.2, 12.4, 12.5_
  
  - [x] 4.3 Write property tests for operation classifier
    - **Property 13: Operation Classification Determinism** - Same operation always returns same type
    - **Property 14: Operation Classification Default Safety** - Unknown operations return PUBLIC
    - **Property 15: Rate Limit Weight Validity** - All weights between 1-10
    - **Property 26: Operation Registry Completeness** - All operations have required fields
    - **Validates: Requirements 2.2, 2.3, 2.4, 12.3, 12.5**
  
  - [x] 4.4 Write unit tests for operation classifier
    - Test classification of all registered operations
    - Test unknown operation handling
    - Test operation metadata retrieval
    - Test registry validation on startup
    - _Requirements: 2.2, 2.3, 2.6, 12.2_

- [x] 5. Implement smart account selection logic
  - [x] 5.1 Create SmartAccountSelector class with following relationship queries
    - Implement select_for_operation() for single username
    - Add get_following_overlap() to query following relationships
    - Integrate with UsernameDatabase for following_status cache
    - Integrate with ProfileAccessTracker for following relationship data
    - Implement fallback logic (cache → tracker → source account → None)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.6, 3.7_
  
  - [x] 5.2 Implement batch processing with smart grouping
    - Implement select_for_batch() to group usernames by optimal account
    - Add logic for PUBLIC operations (single account assignment)
    - Add logic for FOLLOWING_REQUIRED operations (group by following relationships)
    - Optimize to minimize account switches
    - _Requirements: 3.5, 7.1, 7.2, 7.3, 7.4_
  
  - [x] 5.3 Write property tests for account selection
    - **Property 7: Complete Username Coverage in Batch Assignment** - Every username appears exactly once
    - **Property 8: Following Relationship Consistency** - Selected account follows target or is source account
    - **Property 19: Public Operation Single Account Assignment** - All usernames to single account for PUBLIC
    - **Property 20: Following-Required Operation Smart Grouping** - Usernames grouped by following relationships
    - **Property 24: Cache Update Consistency** - Following status from tracker updates cache
    - **Validates: Requirements 7.3, 7.5, 3.2, 3.3, 7.2, 7.3, 3.7**
  
  - [x] 5.4 Write unit tests for account selection
    - Test public operation selection (any account)
    - Test following-required selection with following relationships
    - Test fallback to source account
    - Test no following relationship found (returns None)
    - Test batch grouping optimization
    - Test cache update after tracker query
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.7_

- [x] 6. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Enhance rate limiter with conservative delays
  - [x] 7.1 Extend RateLimiter class with operation-specific delays
    - Add operation_delay() method that accepts OperationType
    - Implement delay scaling: base for PUBLIC, 1.5x for FOLLOWING_REQUIRED, 2x for MUTUAL_FOLLOWING
    - Add random jitter for human-like behavior
    - Implement account_switch_delay() for mandatory delays between account switches
    - Add following_enumeration_delay() with progressive delays every 10 operations
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.8_
  
  - [x] 7.2 Implement account cooldown enforcement
    - Add emergency_cooldown() to apply cooldown after rate limit hits (15+ minutes)
    - Implement check_account_available() to verify account not in cooldown
    - Integrate with existing account_cooldowns.json storage
    - Add cooldown expiration checking
    - _Requirements: 4.6, 4.7, 8.1, 8.2_
  
  - [x] 7.3 Write property tests for rate limiter
    - **Property 9: Rate Limit Monotonicity** - Higher weight operations have longer delays
    - **Property 10: Account Cooldown Enforcement** - Accounts in cooldown return false for availability
    - **Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.6, 4.7**
  
  - [x] 7.4 Write unit tests for rate limiter
    - Test operation-specific delays
    - Test delay scaling by operation type
    - Test account switch delay enforcement
    - Test emergency cooldown application
    - Test account availability checking
    - Test progressive delays during enumeration
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8_

- [x] 8. Integrate with existing account manager and profile tracker
  - [x] 8.1 Enhance AccountManager with availability checking
    - Add get_available_accounts() method that excludes accounts in cooldown
    - Integrate with ConservativeRateLimiter for cooldown checks
    - Add account selection integration with SmartAccountSelector
    - _Requirements: 3.1, 4.7, 8.1_
  
  - [x] 8.2 Enhance ProfileAccessTracker with following relationship queries
    - Add query interface for following relationships
    - Ensure get_profile_summary() returns accessible_by list
    - Add integration with SmartAccountSelector for cache updates
    - _Requirements: 3.6, 3.7_
  
  - [x] 8.3 Write integration tests for account manager and tracker
    - Test AccountManager returns only available accounts
    - Test ProfileAccessTracker following relationship queries
    - Test integration between SmartAccountSelector and ProfileAccessTracker
    - Test cache update flow from tracker to UsernameDatabase
    - _Requirements: 3.6, 3.7, 4.7, 8.1_

- [x] 9. Implement main operation processing with smart routing
  - [x] 9.1 Create process_operation_with_smart_routing() function
    - Implement main algorithm from design document
    - Integrate OperationClassifier for operation type determination
    - Integrate SmartAccountSelector for account assignment
    - Integrate ConservativeRateLimiter for rate limiting
    - Add error handling for rate limit exceptions
    - Add metadata updates after successful operations
    - Return statistics (total, success_count, failed_count)
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 8.2, 8.6_
  
  - [x] 9.2 Add error recovery and resilience
    - Implement all-accounts-in-cooldown waiting logic
    - Add mid-batch rate limit recovery (switch to different account)
    - Implement retry logic with exponential backoff for database saves
    - Add invalid source account error handling with suggestions
    - Add no-following-relationship warning and skip logic
    - _Requirements: 8.1, 8.2, 8.4, 8.5, 8.6, 8.7_
  
  - [x] 9.3 Write property tests for batch processing
    - **Property 18: Batch Processing Statistics Completeness** - Sum of success and failed equals total
    - **Validates: Requirements 7.7**
  
  - [x] 9.4 Write integration tests for operation processing
    - Test end-to-end operation processing with mock Instaloader
    - Test account selection and rate limiting integration
    - Test error recovery from rate limit hits
    - Test metadata updates after operations
    - Test batch processing with multiple accounts
    - Test all-accounts-in-cooldown scenario
    - _Requirements: 7.1, 7.5, 7.6, 7.7, 7.8, 8.1, 8.2, 8.6_

- [x] 10. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Enhance batch processor with smart routing integration
  - [x] 11.1 Update BatchProcessor to use smart routing
    - Replace direct account selection with SmartAccountSelector
    - Integrate process_operation_with_smart_routing() into batch workflows
    - Add progress checkpointing for resuming interrupted batches
    - Update batch state tracking to include account assignments
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 10.4_
  
  - [x] 11.2 Write integration tests for batch processor
    - Test batch processing with smart account selection
    - Test progress checkpointing and resume
    - Test multi-account batch processing
    - Test batch processing with account cooldowns
    - _Requirements: 7.1, 7.5, 10.4_

- [x] 12. Implement metadata management and updates
  - [x] 12.1 Add metadata update methods to UsernameDatabase
    - Implement update_metadata() with merge logic
    - Add last_accessed timestamp updates
    - Add following_status updates
    - Ensure JSON serializability validation
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_
  
  - [x] 12.2 Write property tests for metadata management
    - **Property 16: Metadata JSON Serializability** - All metadata can be serialized to JSON
    - **Property 17: Metadata Merge Preservation** - Updates preserve old fields and overwrite duplicates
    - **Validates: Requirements 6.1, 6.2**
  
  - [x] 12.3 Write unit tests for metadata management
    - Test metadata updates with merge logic
    - Test last_accessed timestamp updates
    - Test following_status updates
    - Test JSON serializability validation
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

- [x] 13. Add comprehensive logging and audit trail
  - [x] 13.1 Implement logging throughout all components
    - Add username addition logging (username, source account, timestamp)
    - Add operation execution logging (operation type, target, selected account)
    - Add rate limit hit logging (account, operation, cooldown duration)
    - Add migration logging (statistics, backup path)
    - Add error logging (error type, context, recovery action)
    - Ensure no sensitive information logged (passwords, session tokens)
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6_
  
  - [x] 13.2 Write unit tests for logging
    - Test logging output for all major operations
    - Test sensitive information is not logged
    - Test error logging includes context
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6_

- [x] 14. Implement performance optimizations
  - [x] 14.1 Add source account indexing for fast queries
    - Build in-memory index on database load
    - Implement O(1) lookup for get_usernames_by_source()
    - Add lazy loading for full UsernameRecord objects
    - _Requirements: 10.1, 10.2_
  
  - [x] 14.2 Optimize batch processing with periodic saves
    - Change from per-operation saves to every-10-operations saves
    - Implement write-behind caching for non-critical updates
    - Add flush() method for explicit save
    - _Requirements: 10.4_
  
  - [x] 14.3 Optimize account selection performance
    - Pre-compute following relationships during follower scraping
    - Cache following_status in UsernameRecord
    - Implement fast path for cache hits
    - _Requirements: 10.5_
  
  - [x] 14.4 Write performance tests
    - Test query performance with 3000+ usernames
    - Test batch selection completes in <100ms for 1000 usernames
    - Test database save time <100ms for 3000 records
    - _Requirements: 10.1, 10.2, 10.5_

- [x] 15. Add backward compatibility and migration support
  - [x] 15.1 Implement flat file format support
    - Ensure export_to_flat_file() produces valid format
    - Add format preference logic (prefer database over flat file)
    - Test reading from flat file for migration
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_
  
  - [x] 15.2 Write integration tests for backward compatibility
    - Test system works with both database and flat file
    - Test preference for database when both exist
    - Test migration from flat file to database
    - Test export back to flat file
    - _Requirements: 9.1, 9.4, 9.5_

- [x] 16. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 17. Create CLI commands and user-facing interfaces
  - [x] 17.1 Add CLI commands for username database management
    - Add command to view usernames by source account
    - Add command to migrate from flat file
    - Add command to export to flat file
    - Add command to view database statistics
    - Update existing commands to use UsernameDatabase instead of flat file
  
  - [x] 17.2 Update existing commands to use smart routing
    - Update download commands to use process_operation_with_smart_routing()
    - Update batch processing commands to use SmartAccountSelector
    - Add operation type display in command output
    - Add account selection reasoning in verbose mode
  
  - [x] 17.3 Write integration tests for CLI commands
    - Test CLI commands with mock database
    - Test migration command end-to-end
    - Test updated download commands use smart routing
    - Test verbose output shows account selection reasoning

- [x] 18. Documentation and final integration
  - [x] 18.1 Update README and documentation
    - Document new username database format
    - Document migration process from flat file
    - Document operation classification system
    - Document smart account selection behavior
    - Add examples of new CLI commands
  
  - [x] 18.2 Create migration guide for existing users
    - Document step-by-step migration process
    - Explain backup and rollback procedures
    - Document expected behavior changes
    - Add troubleshooting section

- [x] 19. Final validation - Run full test suite
  - Run all unit tests, property tests, and integration tests
  - Verify all 26 correctness properties pass
  - Verify code coverage meets 85% minimum
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation at reasonable breaks
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- Integration tests validate component interactions and end-to-end workflows
- The implementation uses Python as specified in the design document
- All 26 correctness properties from the design are covered by property tests
- Conservative rate limiting is a core feature to avoid bans without proxy infrastructure

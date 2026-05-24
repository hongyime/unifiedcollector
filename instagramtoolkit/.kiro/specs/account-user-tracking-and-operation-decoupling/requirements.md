# Requirements Document

## Introduction

This document specifies the business requirements for enhancing the Instagram scraping toolkit with intelligent account-user relationship tracking and operation decoupling. The system currently stores usernames in a flat file without metadata about which account scraped which users, making targeted re-scraping impossible. Additionally, all operations are coupled together regardless of access requirements, leading to inefficient account usage and increased rate limiting risk.

The enhanced system will track which account scraped which users, decouple operations by access requirements (public vs following-required), intelligently select accounts based on following relationships, and implement conservative rate limiting to avoid bans without proxy infrastructure.

## Glossary

- **Username_Database**: Structured storage system that tracks Instagram usernames with source account attribution and metadata
- **Source_Account**: The Instagram account that originally scraped or discovered a username
- **Operation**: An action performed on Instagram (e.g., download profile picture, download stories, download media)
- **Public_Operation**: An operation that can be performed by any account without following the target user
- **Following_Required_Operation**: An operation that requires the executing account to follow the target user
- **Operation_Classifier**: Component that categorizes operations by their access requirements
- **Smart_Account_Selector**: Component that selects optimal accounts based on following relationships and operation requirements
- **Rate_Limiter**: Component that enforces delays between operations to avoid Instagram rate limits
- **Cooldown_Period**: Time duration during which an account cannot be used after hitting rate limits
- **Username_Record**: Data structure containing username, source account, timestamps, and metadata
- **Following_Status**: Boolean mapping indicating which accounts follow which target users
- **Profile_Access_Tracker**: Existing component that tracks which accounts can access which profiles
- **Batch_Processing**: Processing multiple usernames in a single operation run
- **Migration**: Converting data from flat file format to structured database format

## Requirements

### Requirement 1: Username Database with Source Tracking

**User Story:** As a developer, I want to store usernames with source account metadata, so that I can track which account scraped which users and enable targeted re-scraping.

#### Acceptance Criteria

1. WHEN a username is added to the system, THE Username_Database SHALL store the username with the source account that scraped it
2. WHEN a username is added that already exists, THE Username_Database SHALL reject the addition and preserve the existing record
3. WHEN a username is added, THE Username_Database SHALL record the current timestamp as the added_timestamp
4. THE Username_Database SHALL validate that usernames match Instagram username format before adding
5. THE Username_Database SHALL validate that source accounts exist in system configuration before adding
6. WHEN querying by source account, THE Username_Database SHALL return all usernames scraped by that account
7. THE Username_Database SHALL persist all data to disk in JSON format
8. WHEN the database is loaded, THE Username_Database SHALL validate the JSON schema and handle corruption gracefully

### Requirement 2: Operation Classification System

**User Story:** As a system operator, I want operations classified by access requirements, so that the system can select appropriate accounts for each operation type.

#### Acceptance Criteria

1. THE Operation_Classifier SHALL categorize each operation as PUBLIC, FOLLOWING_REQUIRED, or MUTUAL_FOLLOWING
2. WHEN classifying a registered operation, THE Operation_Classifier SHALL return the correct operation type
3. WHEN classifying an unregistered operation, THE Operation_Classifier SHALL return PUBLIC as a safe default
4. THE Operation_Classifier SHALL provide rate limit weight metadata for each operation (1-10 scale)
5. THE Operation_Classifier SHALL maintain a registry of all supported operations with their metadata
6. WHEN querying operation metadata, THE Operation_Classifier SHALL return operation type, rate limit weight, and description

### Requirement 3: Smart Account Selection

**User Story:** As a system operator, I want intelligent account selection based on following relationships, so that following-required operations use accounts that actually follow the target users.

#### Acceptance Criteria

1. WHEN selecting an account for a PUBLIC operation, THE Smart_Account_Selector SHALL return any available account
2. WHEN selecting an account for a FOLLOWING_REQUIRED operation, THE Smart_Account_Selector SHALL return an account that follows the target user
3. WHEN no account follows the target user, THE Smart_Account_Selector SHALL return the source account as fallback
4. WHEN no following relationship is found and source account is unavailable, THE Smart_Account_Selector SHALL return None
5. WHEN processing a batch of usernames, THE Smart_Account_Selector SHALL group usernames by optimal account to minimize account switches
6. WHEN querying following status, THE Smart_Account_Selector SHALL check the Username_Record cache first before querying Profile_Access_Tracker
7. WHEN following status is found in Profile_Access_Tracker, THE Smart_Account_Selector SHALL update the Username_Record cache

### Requirement 4: Conservative Rate Limiting

**User Story:** As a system operator, I want conservative rate limiting without proxies, so that I can avoid Instagram bans while still scraping data.

#### Acceptance Criteria

1. WHEN executing an operation, THE Rate_Limiter SHALL apply a delay based on the operation's rate limit weight
2. WHEN executing a PUBLIC operation, THE Rate_Limiter SHALL apply the base delay
3. WHEN executing a FOLLOWING_REQUIRED operation, THE Rate_Limiter SHALL apply 1.5x the base delay
4. WHEN executing a MUTUAL_FOLLOWING operation, THE Rate_Limiter SHALL apply 2x the base delay
5. WHEN switching between accounts, THE Rate_Limiter SHALL enforce a mandatory account switch delay
6. WHEN a rate limit exception occurs, THE Rate_Limiter SHALL apply an emergency cooldown of at least 15 minutes to the affected account
7. WHEN an account is in cooldown, THE Rate_Limiter SHALL return false for availability checks
8. WHEN processing multiple operations in sequence, THE Rate_Limiter SHALL add progressive delays every 10 operations

### Requirement 5: Flat File Migration

**User Story:** As a system administrator, I want to migrate existing flat file usernames to the structured database, so that I can preserve existing data while gaining new functionality.

#### Acceptance Criteria

1. WHEN migrating from flat file, THE Username_Database SHALL create a backup of the original file
2. WHEN migrating from flat file, THE Username_Database SHALL read all lines and process each username
3. WHEN migrating a valid username, THE Username_Database SHALL add it with the specified default source account
4. WHEN migrating an invalid username, THE Username_Database SHALL skip it and count it as invalid
5. WHEN migrating a duplicate username, THE Username_Database SHALL skip it and count it as duplicate
6. WHEN migration completes, THE Username_Database SHALL return statistics including added, skipped, duplicates, and invalid counts
7. WHEN migration completes, THE Username_Database SHALL save the database to disk

### Requirement 6: Username Metadata Management

**User Story:** As a developer, I want to store and update metadata for usernames, so that I can track additional information like tags, notes, and access history.

#### Acceptance Criteria

1. WHEN adding a username, THE Username_Database SHALL accept optional metadata as a JSON-serializable dictionary
2. WHEN updating metadata for a username, THE Username_Database SHALL merge new metadata with existing metadata
3. WHEN a username is accessed during an operation, THE Username_Database SHALL update the last_accessed timestamp
4. THE Username_Database SHALL store following_status as a mapping of account names to boolean values
5. WHEN querying a username record, THE Username_Database SHALL return all stored metadata including timestamps and following status

### Requirement 7: Batch Processing with Smart Routing

**User Story:** As a system operator, I want batch processing to automatically route usernames to optimal accounts, so that I can efficiently process large lists without manual account management.

#### Acceptance Criteria

1. WHEN processing a batch of usernames, THE system SHALL classify the operation type
2. WHEN processing a PUBLIC operation batch, THE system SHALL assign all usernames to a single available account
3. WHEN processing a FOLLOWING_REQUIRED operation batch, THE system SHALL group usernames by accounts that follow them
4. WHEN processing a batch, THE system SHALL check account availability before assignment
5. WHEN processing a batch, THE system SHALL apply rate limiting between each username
6. WHEN a rate limit exception occurs during batch processing, THE system SHALL mark remaining usernames as failed and switch to next account
7. WHEN batch processing completes, THE system SHALL return statistics including total, success count, and failed count
8. WHEN batch processing completes, THE system SHALL update metadata for all successfully processed usernames

### Requirement 8: Error Recovery and Resilience

**User Story:** As a system operator, I want robust error handling and recovery, so that temporary failures don't cause data loss or system crashes.

#### Acceptance Criteria

1. WHEN all accounts are in cooldown, THE system SHALL wait for the shortest cooldown to expire before resuming
2. WHEN a rate limit is hit mid-batch, THE system SHALL apply emergency cooldown and switch to a different account
3. WHEN the database file is corrupted, THE system SHALL attempt to load from backup
4. WHEN no backup exists and database is corrupted, THE system SHALL initialize an empty database and log the error
5. WHEN an invalid source account is provided, THE system SHALL raise a descriptive error and suggest valid accounts
6. WHEN no following relationship is found for a FOLLOWING_REQUIRED operation, THE system SHALL log a warning and skip the username
7. WHEN database save fails, THE system SHALL retry with exponential backoff up to 3 times

### Requirement 9: Backward Compatibility

**User Story:** As a system administrator, I want backward compatibility with existing flat file format, so that I can gradually transition without breaking existing workflows.

#### Acceptance Criteria

1. THE Username_Database SHALL support exporting all usernames to flat file format
2. WHEN exporting to flat file, THE Username_Database SHALL write one username per line
3. WHEN exporting to flat file, THE Username_Database SHALL preserve username order by added_timestamp
4. THE system SHALL continue to support reading from flat file format for migration purposes
5. WHEN both database and flat file exist, THE system SHALL prefer the database format

### Requirement 10: Performance and Scalability

**User Story:** As a system operator, I want efficient performance with large username lists, so that I can manage thousands of usernames without slowdowns.

#### Acceptance Criteria

1. WHEN querying usernames by source account, THE Username_Database SHALL use an in-memory index for O(1) lookup performance
2. WHEN loading the database, THE Username_Database SHALL build the source account index in a single pass
3. WHEN saving the database, THE Username_Database SHALL use atomic writes to prevent corruption
4. WHEN processing batches, THE system SHALL save database updates every 10 operations rather than after each operation
5. WHEN selecting accounts for batch operations, THE Smart_Account_Selector SHALL complete selection in under 100ms for batches up to 1000 usernames

### Requirement 11: Audit and Logging

**User Story:** As a system administrator, I want comprehensive logging of account usage and operations, so that I can troubleshoot issues and monitor system behavior.

#### Acceptance Criteria

1. WHEN a username is added, THE system SHALL log the username, source account, and timestamp
2. WHEN an operation is executed, THE system SHALL log the operation type, target username, and selected account
3. WHEN a rate limit is hit, THE system SHALL log the affected account, operation, and cooldown duration
4. WHEN migration occurs, THE system SHALL log detailed statistics including counts and backup file path
5. WHEN an error occurs, THE system SHALL log the error type, context, and recovery action taken
6. THE system SHALL NOT log sensitive information such as account passwords or session tokens

### Requirement 12: Configuration Management

**User Story:** As a developer, I want centralized operation configuration, so that I can easily add new operations or modify existing ones without code changes.

#### Acceptance Criteria

1. THE system SHALL maintain an operation registry in configuration mapping operation names to metadata
2. WHEN the system starts, THE Operation_Classifier SHALL load and validate the operation registry
3. WHEN adding a new operation to the registry, THE system SHALL validate that rate_limit_weight is between 1 and 10
4. WHEN adding a new operation to the registry, THE system SHALL validate that operation_type is a valid OperationType enum value
5. THE operation registry SHALL include at minimum: operation name, operation type, rate limit weight, and description

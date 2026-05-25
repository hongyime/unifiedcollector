# Toolkit -> Unified parity matrix

Heuristic gap analysis. Each row = one toolkit feature symbol that does NOT
appear (case-insensitive substring) in the corresponding unified collector
file. Manual review still required, but these are the candidates to port.

## Summary

| Source | Toolkit LOC | Unified LOC | Naive coverage | Filtered candidates |
|--------|------------:|------------:|---------------:|--------------------:|
| github | 3,266 | 446 | 6.1% | 102 |
| instagram | 53,746 | 1,235 | 3.0% | 583 |
| lemon8 | 16,814 | 1,262 | 3.3% | 129 |
| search | 5,282 | 159 | 8.6% | 71 |
| strava | 11,874 | 575 | 6.6% | 149 |
| telegram | 22,181 | 609 | 4.0% | 459 |
| tiktok | 15,245 | 616 | 3.0% | 237 |
| whatsapp | 20,496 | 623 | 7.2% | 385 |

---

## Per-source feature gaps

Each subsection lists candidate symbols (functions/classes from toolkit) NOT
found by name in the unified collector. Bucketed by domain keyword so
review can focus on missing capability classes (e.g. all `auth/session`
items together).


### github

**uncategorized** (48)  

`Config`, `GitHubAPIClient`, `GitHubAPIClient.get_all_user_repos`, `GitHubAPIClient.get_user`, `GitHubAPIClient.get_user_by_id`, `GitHubAPIClient.get_user_repos`, `PATManager`, `PATManager.delete_pat`, `PATManager.get_pat_display`, `PATManager.list_pats`, `PATManager.load_all_pats`, `PATManager.validate_pat_format`, `Reconciler`, `Reconciler.verify_integrity`, `add_contribution_edge`, `add_edge`, `add_user_if_not_exists`, `api_graph`, `api_search`, `api_stats`, `api_user`, `api_users`, `clear_pat`, `cohort_analysis`, `contributions_menu`, `database_menu`, `export_search_csv`, `frontend_menu`, `get_graph_data`, `get_stats`
 *(+18 more)*


**scraping** (19)  

`ContributionSpider`, `ContributionSpider.spider_all_users_repos`, `ContributionSpider.spider_repo_contributors`, `ContributionSpider.spider_user_repos`, `SocialGraphSpider`, `SocialGraphSpider.seed_from_id`, `SocialGraphSpider.seed_from_self`, `SocialGraphSpider.seed_from_user`, `SocialGraphSpider.spider_all`, `get_pending_spider_users`, `reset_in_progress_spiders`, `reset_spider`, `search_users`, `search_users_fts`, `spider_all`, `spider_all_repos`, `spider_repo_contributors`, `spider_user_repos`, `update_spider_status`


**media** (12)  

`AvatarDownloader`, `AvatarDownloader.download_avatar`, `AvatarDownloader.download_range`, `ProfilePhotoTracker.get_photo_history`, `ProfilePhotoTracker.track_photo_change`, `avatar_downloads_menu`, `download_by_range`, `download_for_users`, `get_downloaded_hashes`, `prompt_for_download_path`, `save_avatar_download`, `view_photo_history`


**post/feed** (8)  

`GitHubAPIClient.follow_user`, `GitHubAPIClient.get_all_followers`, `GitHubAPIClient.get_all_following`, `GitHubAPIClient.get_followers`, `GitHubAPIClient.get_following`, `GitHubAPIClient.unfollow_user`, `api_follow`, `profile_history_menu`


**auth/session** (4)  

`GitHubAPIClient.get_authenticated_user`, `authentication_menu`, `clear_session_cache`, `get_session_path`


**network** (3)  

`Config.ensure_directories`, `GitHubAPIClient.get_repo_contributors`, `upsert_repository`


**rate-limit** (3)  

`GitHubAPIClient.get_rate_limit`, `GitHubAPIClient.get_rate_limit_status`, `check_rate_limit`


**profile** (3)  

`Reconciler.reconcile_avatars`, `import_existing_avatars`, `reconcile_avatars`


**storage** (1)  

`PATManager.store_pat`


**account-pool** (1)  

`seed_from_account`



### instagram

**uncategorized** (213)  

`BaseBackend`, `BaseBackend.placeholder`, `BaseBackend.upsert_syntax`, `BaseCommand`, `BaseCommand.validate_args`, `BatchProcessor`, `BatchProcessor.get_summary`, `BatchProcessor.process_batch`, `DashboardHandler`, `DashboardHandler.do_GET`, `DatabaseManager`, `DatabaseManager.apply_migrations`, `DatabaseManager.create_schema`, `DatabaseManager.executemany`, `DatabaseManager.fetchall`, `DatabaseManager.fetchone`, `DatabaseManager.get_connection`, `ExceptionPolicy`, `FileLock`, `FileLock.release`, `InstagramProcessor`, `InstagramProcessor.collect_relationships`, `InstagramProcessor.process_batch_relationships`, `OperationClassifier`, `OperationClassifier.classify`, `OperationClassifier.get_all_operations`, `OperationClassifier.is_public_operation`, `PriorityManager`, `PriorityManager.get_category_stats`, `PriorityManager.get_high_priority_users`
 *(+183 more)*


**media** (69)  

`BrowserDownloader`, `BrowserDownloader.download_batch`, `DownloadCommand`, `FollowingDownloadCommand`, `FollowingMediaDownloader`, `FollowingMediaDownloader.cleanup`, `FollowingMediaDownloader.download_all_following`, `FollowingMediaDownloader.get_following_list`, `FollowingMediaDownloader.reset_progress`, `FollowingMediaDownloader.setup_downloads_directory`, `InstagramProcessor.process_batch_downloads`, `MediaDownloader`, `MediaDownloader.cleanup`, `MediaDownloader.download_all`, `MediaDownloader.download_highlights`, `MediaDownloader.download_posts`, `MediaDownloader.download_profile_photo`, `MediaDownloader.download_stories`, `MediaDownloader.verify_download`, `MediaItemRepository`, `MediaItemRepository.add_media_item`, `MediaItemRepository.compute_sha256_hash`, `MediaItemRepository.get_file_size`, `MediaItemRepository.get_media_by_shortcode`, `MediaItemRepository.get_media_by_user`, `MediaItemRepository.get_stats`, `MediaItemRepository.mark_corrupted`, `MediaItemRepository.mark_missing`, `MediaItemRepository.update_file_hash`, `ProfilePhotoTracker.check_for_change`
 *(+39 more)*


**rate-limit** (68)  

`AccountCooldownManager`, `AccountCooldownManager.clear_cooldown`, `AccountCooldownManager.get_available_accounts`, `AccountCooldownManager.get_cooldown_remaining`, `AccountCooldownManager.is_on_cooldown`, `AccountCooldownManager.put_on_cooldown`, `AccountCooldownRepository`, `AccountCooldownRepository.clear_cooldown`, `AccountCooldownRepository.get_available`, `AccountCooldownRepository.get_remaining`, `AccountCooldownRepository.is_on_cooldown`, `AccountCooldownRepository.put_on_cooldown`, `AccountRateLimitRepository`, `AccountRateLimitRepository.can_make_request`, `AccountRateLimitRepository.cleanup_old_records`, `AccountRateLimitRepository.get_limits`, `AccountRateLimitRepository.get_request_count`, `AccountRateLimitRepository.get_usage_summary`, `AccountRateLimitRepository.record_request`, `AccountRateLimitRepository.set_limits`, `ConservativeRateLimiter`, `ConservativeRateLimiter.check_account_available`, `ConservativeRateLimiter.following_enumeration_delay`, `ConservativeRateLimiter.get_available_accounts`, `ConservativeRateLimiter.get_cooldown_remaining`, `ConservativeRateLimiter.operation_delay`, `InstagramProcessor.process_retry_queue`, `RateLimitException`, `RateLimiter.check_sliding_window_limit`, `RateLimiter.emergency_break`
 *(+38 more)*


**account-pool** (63)  

`AccountQuotaManager`, `AccountQuotaManager.can_perform_action`, `AccountQuotaManager.can_view_profiles`, `AccountQuotaManager.get_daily_usage`, `AccountQuotaManager.get_usage_summary`, `AccountQuotaManager.record_action`, `AccountQuotaManager.record_profile_view`, `AccountQuotaRepository`, `AccountQuotaRepository.get_usage`, `AccountQuotaRepository.record_action`, `AccountQuotaRepository.record_profile_view`, `AccountQuotaRepository.reset_if_new_day`, `FollowingMediaDownloader.download_account_media`, `FollowingMediaDownloader.download_single_account`, `FollowingMediaDownloader.select_account`, `InstagramAccountManager`, `InstagramAccountManager.get_available_accounts`, `InstagramAccountManager.is_logged_in`, `InstagramProcessor.get_best_account_for_public`, `PriorityManager.get_account_connections`, `ProfileAccessRepository.get_accessible_accounts`, `ProfileAccessRepository.get_best_account`, `ProfileAccessTracker.get_best_account_for_profile`, `ProfileAccessTracker.get_following_accounts`, `ProgressManager.get_remaining_accounts`, `SmartAccountSelector`, `SmartAccountSelector.get_following_overlap`, `SmartAccountSelector.select_for_batch`, `SmartAccountSelector.select_for_operation`, `TestAccountAvailabilityChecking`
 *(+33 more)*


**profile** (52)  

`OperationClassifier.get_operation_metadata`, `OperationMetadata`, `ProfileAccessRepository`, `ProfileAccessRepository.cleanup_inactive_profiles`, `ProfileAccessRepository.cleanup_old_attempts`, `ProfileAccessRepository.get_profile_summary`, `ProfileAccessRepository.get_statistics`, `ProfileAccessRepository.record_attempt`, `ProfileAccessTracker`, `ProfileAccessTracker.cleanup_old_data`, `ProfileAccessTracker.cleanup_old_profiles`, `ProfileAccessTracker.get_access_statistics`, `ProfileAccessTracker.get_profile_summary`, `ProfileAccessTracker.record_profile_access`, `ProfileAccessTracker.save_access_data`, `ProfileAnalyzer`, `ProfileAnalyzer.analyze_network`, `ProfileAnalyzer.get_influential_users`, `ProfileAnalyzer.get_reciprocal_relationships`, `ProfileAnalyzer.save_analysis`, `ProfileRepository`, `ProfileRepository.get_all_profiles`, `ProfileRepository.get_profile`, `ProfileRepository.get_profile_changes`, `ProfileRepository.get_snapshots`, `ProfileScanner`, `ProfileScanner.scan_all`, `ProfileScanner.scan_one`, `TestGetProfile`, `TestMigrateProfiles`
 *(+22 more)*


**network** (38)  

`OperationProgressRepository`, `OperationProgressRepository.get_batch_state`, `OperationProgressRepository.get_completed`, `OperationProgressRepository.get_failed`, `OperationProgressRepository.get_pending`, `OperationProgressRepository.get_remaining`, `OperationProgressRepository.get_statistics`, `OperationProgressRepository.get_status`, `OperationProgressRepository.upsert_batch_state`, `OperationProgressRepository.upsert_progress`, `RelationshipCollector`, `RelationshipCollector.cleanup`, `RelationshipCollector.collect_for_user`, `RelationshipCollector.relationships`, `RelationshipCollector.run_batch`, `RelationshipCollector.usernames`, `RelationshipRepository`, `RelationshipRepository.bulk_upsert`, `RelationshipRepository.get_all_usernames`, `RelationshipRepository.get_mutual`, `RelationshipRepository.get_relationships`, `RelationshipRepository.relationship_exists`, `RelationshipRepository.upsert_relationship`, `TestOperationProgressRepositoryCRUD`, `TestParallelConvenienceFunctions.mock_collector`, `TestRelationshipCollectorIntegration`, `TestRelationshipRepositoryCRUD`, `TestRelationshipRepositoryQueries`, `TestSetupTargetDirectory`, `TestUsernameRepositoryCRUD`
 *(+8 more)*


**auth/session** (29)  

`BrowserSessionManager`, `BrowserSessionManager.get_page`, `InstagramAccountManager.get_authenticated_loader`, `InstagramAccountManager.get_session_file`, `InstagramAccountManager.logout`, `ProfileRepository.needs_refresh`, `SessionTracker`, `SessionTracker.record_operation`, `SessionTracker.record_save`, `TestAuthPreservation`, `TestAuthentication`, `TestGetAuthenticatedLoader`, `TestGetAuthenticatedLoaderEdgeCases`, `TestGetSessionFile`, `TestLogin`, `TestLoginAccount`, `TestLoginEdgeCases`, `TestLogoutAndIsLoggedIn`, `TestSessionManagementPreservation`, `TestSessionStatisticsPreservation`, `TestSessionValidationFallbackExploration`, `clear_session_cache`, `get_session_path`, `is_challenge_exception`, `make_requests_session`, `refresh_all_sessions`, `refresh_sessions`, `tmp_sessions_dir`, `warmup_session`


**post/feed** (23)  

`OperationClassifier.requires_following`, `PostgreSQLBackend`, `PostgreSQLBackend.placeholder`, `PostgreSQLBackend.upsert_syntax`, `ProfileRepository.filter_by_follower_range`, `ProfileRepository.get_top_by_followers`, `ProfileRepository.get_top_by_following`, `ProfileRepository.get_username_history`, `RelationshipRepository.get_followers`, `RelationshipRepository.get_following`, `TestFollowingRelationshipConsistency`, `TestFollowingRequiredSelection`, `TestFollowingRequiredSmartGrouping`, `TestFollowingStatus`, `TestGetFollowingOverlap`, `UserMetadataManager.filter_by_follower_count`, `UserMetadataManager.get_top_followers`, `UserMetadataManager.get_top_following`, `UserMetadataManager.is_within_follower_limit`, `UsernameDatabase.update_following_status`, `UsernameRepository.update_following_status`, `extract_post_data`, `get_post_shortcodes`


**storage** (18)  

`ArchiveRetentionManager`, `ArchiveRetentionManager.cleanup_all`, `ArchiveRetentionManager.cleanup_by_age`, `ArchiveRetentionManager.cleanup_by_count`, `ArchiveRetentionManager.get_archive_summary`, `OperationProgressRepository.archive_operation`, `SmartBatchProcessor.clear_checkpoint`, `TestArchiveCleanupPatternMismatch`, `TestArchiveNamingConvention`, `TestArchiveOperation`, `TestArchivePatternMismatchCounterexamples`, `TestArchiveRetentionBasics`, `TestPersistence`, `TestRelationshipPersistence`, `TestUsernamePersistence`, `UsernameDatabase.create_backup`, `archive_dir`, `cleanup_archives`


**face/vision** (4)  

`TestFallbackStringMatching`, `TestReturnContractMismatchExploration`, `TestReturnContractMismatchInMain`, `TestReturnContractMismatchInProcessor`


**messaging** (2)  

`TestFormatExceptionMessage`, `TestMessageDisplayPreservation`


**scraping** (1)  

`SpiderCommand`


**group/channel** (1)  

`TestBatchGroupingOptimization`


**transcript** (1)  

`TestExtractUsername`


**dedupe/hash** (1)  

`TestLinearDeduplicationExploration`



### lemon8

**uncategorized** (37)  

`FakeResponse`, `FakeResponse.iter_content`, `GraphBuilder`, `GraphBuilder.add_edge`, `GraphBuilder.build_graph_from_users`, `GraphBuilder.clear_graph`, `GraphBuilder.export_graph_json`, `GraphBuilder.get_graph_stats`, `GraphBuilder.get_user_connections`, `Lemon8Toolkit`, `Lemon8Toolkit.build_graph`, `Lemon8Toolkit.clear_all`, `Lemon8Toolkit.clear_cache`, `Lemon8Toolkit.reconcile_missing_files`, `ProgressManager`, `ProgressManager.get_stats`, `Reconciler`, `TagTracker`, `TagTracker.clear_processed_tags`, `TagTracker.get_all_processed_tags`, `TagTracker.get_stats`, `TagTracker.get_tag_info`, `TagTracker.is_tag_processed`, `TagTracker.is_tag_tracked`, `TagTracker.mark_tag_processed`, `TestBugConditionExploration`, `TestCheckColumnExistsMethod`, `TestCheckColumnExistsMethod.setup_method`, `TestCheckColumnExistsMethod.teardown_method`, `TestPreservationProperties`
 *(+7 more)*


**account-pool** (29)  

`AccountManager`, `AccountManager.add_account`, `AccountManager.get_account_stats`, `AccountManager.get_all_accounts`, `AccountManager.get_available_account`, `AccountManager.mark_account_used`, `AccountManager.remove_account`, `AccountTracker`, `AccountTracker.clear_visited_users`, `AccountTracker.create_snapshot`, `AccountTracker.get_all_visited_users`, `AccountTracker.get_discovered_users`, `AccountTracker.get_pending_spider_users`, `AccountTracker.get_stats`, `AccountTracker.get_user_by_id`, `AccountTracker.get_user_history`, `AccountTracker.get_user_info`, `AccountTracker.is_user_tracked`, `AccountTracker.is_user_visited`, `AccountTracker.mark_spider_completed`, `AccountTracker.mark_spider_in_progress`, `AccountTracker.mark_user_visited`, `AccountTracker.reset_stuck_spiders`, `AccountTracker.resolve_username_from_id`, `Lemon8Toolkit.add_account`, `Lemon8Toolkit.list_accounts`, `TestAccountTrackerColumnCheck`, `TestAccountTrackerColumnCheck.setup_method`, `TestAccountTrackerColumnCheck.teardown_method`


**media** (21)  

`Lemon8Toolkit.download_pending_media`, `Lemon8Toolkit.view_photo_history`, `MediaDownloader`, `MediaDownloader.clear_download_history`, `MediaDownloader.download_multiple_media`, `MediaDownloader.get_stats`, `MediaDownloader.is_already_downloaded`, `MediaDownloader.mark_as_downloaded`, `ProfilePhotoTracker`, `ProfilePhotoTracker.check_and_track_photo`, `ProfilePhotoTracker.export_photo_blob`, `ProfilePhotoTracker.get_photo_history`, `ProfilePhotoTracker.get_stats`, `Reconciler.reconcile_profile_photos`, `create_different_test_image`, `create_mock_download_results`, `create_test_image`, `downloader`, `get_downloads_directory`, `get_media_save_path`, `prompt_for_download_path`


**auth/session** (13)  

`Lemon8Toolkit.view_recent_sessions`, `ProgressManager.end_session`, `ProgressManager.get_all_sessions_summary`, `ProgressManager.get_current_session`, `ProgressManager.get_session_summary`, `ProgressManager.resume_session`, `ProgressManager.start_session`, `ProgressManager.update_session_downloaded_media`, `ProgressManager.update_session_failed_download`, `ProgressManager.update_session_scraped_media`, `clear_session_cache`, `create_test_cookies_file`, `get_session_path`


**rate-limit** (12)  

`AccountManager.clear_account_cooldown`, `AccountManager.set_account_cooldown`, `AdaptiveRateLimiter`, `AdaptiveRateLimiter.get_cooldown_remaining`, `AdaptiveRateLimiter.get_stats`, `AdaptiveRateLimiter.is_in_cooldown`, `AdaptiveRateLimiter.record_error`, `AdaptiveRateLimiter.record_rate_limit`, `AdaptiveRateLimiter.record_success`, `AdaptiveRateLimiter.reset_account`, `Lemon8Toolkit.view_cooldowns`, `TestAdaptiveRateLimiterJitter`


**scraping** (10)  

`Lemon8Scraper`, `Lemon8Scraper.human_sleep`, `Lemon8Scraper.scrape_for_you_feed`, `Lemon8Scraper.scrape_tag_topic`, `Lemon8Scraper.scrape_user_profile`, `Lemon8Toolkit.scrape_tag`, `Lemon8Toolkit.scrape_user`, `Lemon8Toolkit.spider_batch`, `create_mock_scrape_result`, `scraper`


**post/feed** (3)  

`Lemon8Toolkit.seed_from_feed`, `Lemon8Toolkit.view_user_history`, `ProgressManager.clear_progress_history`


**storage** (1)  

`Lemon8Toolkit.backup_database`


**dedupe/hash** (1)  

`Reconciler.get_dedup_stats`


**transcript** (1)  

`TestEnvContext`


**network** (1)  

`ensure_data_directory`



### search

**uncategorized** (32)  

`ApiUsage`, `ProgressTracker`, `QueryProgress`, `SearchCache.cleanup_expired`, `SearchCache.get_stats`, `StateManager`, `StateManager.clear_all`, `StateManager.complete_query`, `StateManager.get_api_usage_summary`, `StateManager.get_stats`, `StateManager.record_api_usage`, `StateManager.start_query`, `StateManager.update_query_progress`, `clean_filename`, `cleanup_enhanced_components`, `create_argument_parser`, `ensure_dir`, `extract_pdf_pages_as_jpg`, `get_search_results`, `has_transparency`, `init_enhanced_components`, `is_content_url`, `mode_dork_runner`, `mode_search_extract`, `parse_cli_args`, `robust_get`, `save_results`, `search_bing`, `search_chrome`, `search_duckduckgo`
 *(+2 more)*


**media** (17)  

`DownloadRecord`, `StateManager.get_pending_downloads`, `StateManager.is_downloaded`, `StateManager.mark_download_complete`, `StateManager.mark_download_failed`, `StateManager.mark_download_pending`, `StateManager.mark_download_skipped`, `_download_single`, `check_image_quality`, `download_file`, `download_image`, `download_with_quality_gate`, `get_bing_images`, `mode_bing_images`, `parallel_download`, `process_image`, `prompt_for_download_path`


**rate-limit** (11)  

`AdaptiveRateLimiter`, `AdaptiveRateLimiter.record_failure`, `AdaptiveRateLimiter.record_success`, `RateLimiter`, `RateLimiter.get_domain_delay`, `RateLimiter.record_failure`, `RateLimiter.record_success`, `RateLimiter.reset_all`, `RateLimiter.reset_domain`, `RateLimiter.rotate_circuit`, `with_internet_retry`


**account-pool** (3)  

`TorManager.proxy_url`, `TorManager.rotate_circuit`, `rotate_ua`


**auth/session** (3)  

`_build_session`, `clear_session_cache`, `get_session_path`


**network** (2)  

`TorManager`, `TorManager.is_running`


**dedupe/hash** (1)  

`DeduplicationTracker`


**storage** (1)  

`StateManager.backup_to_json`


**scraping** (1)  

`spider_page`



### strava

**uncategorized** (93)  

`AthleteActivityResponse`, `AthleteDetailResponse`, `AthleteListResponse`, `AthleteRouteActivityResponse`, `AthleteRoutesResponse`, `AthleteSummaryResponse`, `BackfillCoverageResponse`, `BackfillRunResponse`, `BackfillRunner`, `BackfillRunner.is_running`, `BackfillRunner.recommended_command`, `CoverageMonthResponse`, `CoverageYearResponse`, `DayPlaybackResponse`, `RequestsDependencyHealth`, `Settings`, `StatusResponse`, `SyncRunner`, `SyncRunner.is_running`, `SyncRunner.recommended_command`, `TripResponse`, `activity_data`, `activity_palette`, `athlete_color`, `athlete_data`, `athlete_detail`, `athlete_routes`, `backfill_coverage`, `backfill_job`, `backfill_run`
 *(+63 more)*


**scraping** (16)  

`CrawlSummary`, `Crawler`, `ExploreResult`, `FollowRosterScraper`, `FollowRosterScraper.fetch_following_roster`, `FollowingFeedScraper`, `FollowingFeedScraper.fetch_activities_for_date`, `HistoricalActivityScraper`, `HistoricalActivityScraper.fetch_batch`, `create_crawl_run`, `finalize_crawl_run`, `list_explore_segments`, `list_explore_stubs`, `promote_explore_athletes`, `run_explore_scraper`, `save_explore_segment`


**auth/session** (11)  

`SessionError`, `StravaSession`, `StravaSession.clone`, `StravaSession.from_sources`, `StravaSession.get_json`, `StravaSession.get_text`, `StravaSession.persist_cookie`, `StravaSession.reauthenticate`, `StravaSession.set_persist_callback`, `mock_session`, `save_session_state`


**rate-limit** (10)  

`AdaptiveRateLimiter`, `AdaptiveRateLimiter.record_failure`, `AdaptiveRateLimiter.record_success`, `DelayManager`, `DelayManager.backfill_delay`, `DelayManager.roster_delay`, `DelayManager.stream_delay`, `create_delay_manager`, `exponential_backoff`, `random_delay`


**media** (10)  

`PhotoDownloadSummary`, `PhotoDownloader`, `get_latest_profile_photo`, `insert_profile_photo_history`, `list_activity_photo_targets`, `list_profile_photo_targets`, `mark_activity_photo_downloaded`, `normalize_activity_photos`, `save_activity_photos`, `touch_profile_photo_history`


**post/feed** (6)  

`HistoryFetchIssue`, `build_athlete_route_history`, `extract_profile_feed_entries`, `get_following_backfill_candidates`, `parse_following_cards`, `sync_following_roster`


**network** (3)  

`activity_exists_with_terminal_stream`, `reset_activity_stream_status`, `transform_streams`



### telegram

**uncategorized** (179)  

`APIHandler`, `APIHandler.do_GET`, `APIHandler.handle_api_request`, `APIHandler.send_error`, `APIHandler.send_json_response`, `APIHandler.serve_graph`, `APIHandler.serve_health`, `APIHandler.serve_memberships`, `APIHandler.serve_stats`, `APIHandler.serve_user`, `APIHandler.serve_user_memberships`, `APIHandler.serve_users`, `BugConditionExplorationTests`, `CORSRequestHandler`, `CORSRequestHandler.do_GET`, `CORSRequestHandler.end_headers`, `ConsoleOutputTests`, `DatabaseLockError`, `DynamicConfig`, `DynamicConfig.force_reload`, `FakeAction`, `FakeClient`, `FakeClient.is_connected`, `FakeClient.iter_dialogs`, `FakeClient.iter_participants`, `FakeDialog`, `FakeDocument`, `FakeForward`, `FakeUser`, `GracefulShutdown`
 *(+149 more)*


**media** (85)  

`DownloadStateManager`, `DownloadStateManager.reset_all_tracking`, `DownloadStateManager.reset_chat_progress`, `DownloadStateManager.reset_link_tracking`, `DownloadStateManager.reset_media_tracking`, `DownloadStateManager.reset_photo_send_tracking`, `DownloadStateManager.reset_profile_photo_tracking`, `DownloadStateManager.reset_scan_tracking`, `DownloadStateManager.reset_user_tracking`, `FakePhotoMedia`, `MediaDownloadRunner`, `MediaDownloadRunner.build_orchestrator`, `MediaDownloaderProcessor`, `MediaDownloaderProcessor.calculate_hash`, `MediaDownloaderProcessor.classify_document_media`, `MediaDownloaderProcessor.download_photo`, `MediaDownloaderProcessor.download_supported_document`, `MediaDownloaderProcessor.generate_filename`, `MediaDownloaderProcessor.get_safe_group_folder`, `MediaDownloaderProcessor.initialize`, `MediaDownloaderProcessor.on_scan_complete`, `MediaDownloaderProcessor.on_scan_start`, `MediaDownloaderProcessor.process_message`, `MediaDownloaderProcessor.shutdown`, `MediaDownloaderProcessorTests`, `MediaPolicyTests`, `PhotoSender`, `PhotoSender.create_progress_key`, `PhotoSender.scan_photos_generator`, `PhotoSender.send_photos`
 *(+55 more)*


**account-pool** (65)  

`AccountErrorClassification`, `AccountFailureError`, `AccountHealthPolicy`, `AccountHealthPolicy.ensure_connected`, `AccountHealthPolicy.get_best_account`, `AccountHealthPolicy.get_state`, `AccountHealthPolicy.handle_account_failure`, `AccountHealthPolicy.is_available`, `AccountHealthPolicy.is_retired`, `AccountHealthPolicy.record_flood_wait`, `AccountHealthState`, `AccountManager`, `AccountManager.add_new_account`, `AccountManager.generate_prefix`, `AccountManager.get_account_dictionaries`, `AccountManager.get_accounts_by_names`, `AccountManager.get_available_accounts`, `AccountManager.get_optimal_account_count`, `AccountManager.list_accounts`, `AccountManager.load_current_accounts`, `AccountManager.reload_config_in_modules`, `AccountManager.remove_account`, `AccountManager.save_accounts_to_config`, `AccountManager.validate_accounts`, `DownloadStateManager.reset_account_progress`, `DynamicConfig.get_account_by_name`, `DynamicConfig.get_account_count`, `DynamicConfig.get_accounts`, `GroupCleaner.select_accounts`, `GroupJoiner.join_link_with_account`
 *(+35 more)*


**messaging** (26)  

`FakeAdminEvent`, `FakeChat`, `FakeClient.iter_admin_log`, `FakeMessage`, `FakeMessage.get_reply_message`, `FakeMessageEntityMention`, `FakeMessageEntityMentionName`, `FakeReplyMessage`, `FeatureProcessor.process_message`, `LinkCollectorProcessor.process_message`, `MessageOrchestrator`, `MessageOrchestrator.get_processor_feature_keys`, `MessageOrchestrator.get_unified_progress_snapshot`, `MessageOrchestrator.get_unified_start_message_id`, `MessageOrchestrator.initialize_processors`, `MessageOrchestrator.register_processor`, `MessageOrchestrator.scan_all_features`, `MessageOrchestrator.scan_group`, `MessageOrchestrator.shutdown_processors`, `MockTelegramClient.get_messages`, `MockTelegramClient.leave_chat`, `MockTelegramClient.send_message`, `StateManager.get_chat_progress`, `UserAnalyzerProcessor.process_message`, `export_messages`, `group_messages_by_topic`


**face/vision** (23)  

`BaseFeature`, `BaseFeature.extract_links`, `BaseFeature.handle_error`, `BaseFeature.verify_entity_access`, `FeatureProcessor`, `FeatureProcessor.get_progress_key`, `FeatureProcessor.initialize`, `FeatureProcessor.on_scan_complete`, `FeatureProcessor.on_scan_start`, `FeatureProcessor.shutdown`, `FeatureRegistryTests`, `ProcessorFeatureDefinition`, `ProcessorFeatureDefinition.build_processor`, `StateManager.get_feature_progress`, `StateManager.get_feature_progress_all`, `StateManager.reset_feature_progress`, `StateManager.reset_feature_progress_scope`, `StateManager.save_feature_progress`, `TelegramToolkit.scan_all_features`, `TestCSVExportInScanAllFeatures`, `TestCSVExportNotCalledOtherFeatures`, `get_processor_feature_definition`, `list_processor_feature_definitions`


**group/channel** (20)  

`GroupCleaner`, `GroupCleaner.handle_exit`, `GroupCleaner.initialize_clients`, `GroupCleaner.leave_candidate_groups`, `GroupCleaner.scan_groups`, `GroupJoiner`, `GroupJoiner.extract_links_list`, `GroupJoiner.handle_exit`, `GroupJoiner.initialize_clients`, `GroupJoiner.join_groups`, `GroupJoiner.join_linked_discussion_group`, `GroupJoiner.load_joined_links`, `GroupJoiner.save_joined_link`, `GroupJoiner.try_join_link`, `GroupJoiner.validate_link_globally`, `GroupJoiner.write_remaining_links`, `MockTelegramClient.join_channel`, `TelegramToolkit.join_groups`, `TelegramToolkit.leave_groups`, `is_valid_group_name`


**network** (20)  

`LinkCollectorProcessor`, `LinkCollectorProcessor.initialize`, `LinkCollectorProcessor.on_scan_complete`, `LinkCollectorProcessor.on_scan_start`, `LinkCollectorProcessor.shutdown`, `MultiPlatformLinkCollector`, `MultiPlatformLinkCollector.buffer_multi_platform_link`, `MultiPlatformLinkCollector.collect_all_multi_platform_links`, `MultiPlatformLinkCollector.extract_multi_platform_links`, `MultiPlatformLinkCollector.flush_multi_platform_buffer`, `MultiPlatformLinkCollector.load_multi_platform_links`, `SafeConsoleStream`, `SafeConsoleStream.buffer`, `SafeConsoleStream.encoding`, `SafeConsoleStream.isatty`, `TelegramToolkit.build_unified_orchestrator`, `TestOrchestratorIntegration`, `TestToolkitOrchestratorBuilding`, `UserAnalysisRunner.build_orchestrator`, `mock_orchestrator`


**auth/session** (12)  

`AccountManager.copy_sessions_from_folders`, `AccountManager.create_account_session`, `AccountManager.generate_session_filename`, `AccountManager.import_session_file`, `AccountManager.manage_sessions`, `AccountManager.regenerate_missing_sessions`, `DynamicConfig.verify_sessions`, `MockTelegramClient.is_user_authorized`, `PhotoSender.get_session_files`, `detect_session_health`, `verify_account_login`, `verify_session_exists`


**scraping** (8)  

`FeatureProcessor.discover_scan_targets`, `GroupJoiner.save_discovered_discussion_group`, `LinkCollectorProcessor.discover_scan_targets`, `MediaDownloaderProcessor.discover_scan_targets`, `OrchestratorTargetDiscoveryTests`, `ScanTargetDiscoveryTests`, `UserAnalyzerProcessor.discover_scan_targets`, `discover_scan_targets`


**rate-limit** (5)  

`BaseFeature.apply_rate_limit`, `BaseFeature.retry_api_call`, `StateManager.retry_failed_lookups`, `retry_api_call`, `send_with_retry`


**dedupe/hash** (5)  

`StateManager.get_all_hashes`, `StateManager.get_hash_count`, `StateManager.hash_exists`, `StateManager.save_hash`, `chunked_file_hash`


**storage** (5)  

`StateManager.recover_from_json_backup`, `StateManager.sync_to_json_backup`, `TelegramToolkit.run_backup`, `TestCLIArgumentRoutingBackup`, `safe_json_save_with_backup`


**transcript** (3)  

`LinkCollectorProcessor.extract_links_from_text`, `build_message_text`, `normalize_console_text`


**post/feed** (2)  

`StateManager.get_user_history`, `StateManager.save_user_history_event`


**profile** (1)  

`TestCLIArgumentRoutingProfiles`



### tiktok

**uncategorized** (115)  

`AntiBotError`, `AppConfig`, `BrowserAutomationError`, `CleanupUI`, `CleanupUI.confirm_removal`, `CleanupUI.display_invalid_usernames`, `CleanupUI.display_removal_result`, `CleanupUI.present_cleanup_prompt`, `CompositeTracker`, `CompositeTracker.count_for_user`, `CompositeTracker.vacuum`, `ConfigError`, `FileLock`, `FileLock.release`, `GalleryDLProvider`, `InvalidReason`, `InvalidUsernameRecord`, `InvalidUsernameTracker`, `InvalidUsernameTracker.clear_username`, `InvalidUsernameTracker.get_invalid_records`, `InvalidUsernameTracker.get_invalid_usernames`, `InvalidUsernameTracker.is_confirmed_invalid`, `InvalidUsernameTracker.record_invalid`, `ProviderError`, `ReconcileResult`, `Reconciler`, `Reconciler.reconcile_tier1`, `Reconciler.reconcile_tier2`, `Reconciler.run_full_reconciliation`, `TestAnalyzeError`
 *(+85 more)*


**media** (49)  

`BrowserDownloader`, `BrowserDownloader.download_user_with_browser`, `CompositeTracker.is_downloaded`, `CompositeTracker.is_downloaded_in_folder`, `CompositeTracker.mark_downloaded`, `DownloadError`, `DownloadResult`, `GalleryDLProvider.download_user`, `PhotoRecord`, `ProfilePhotoTracker`, `ProfilePhotoTracker.confirm_change_by_phash`, `ProfilePhotoTracker.get_change_candidates`, `ProfilePhotoTracker.get_current`, `ProfilePhotoTracker.get_latest_history`, `ProfilePhotoTracker.is_changed`, `Reconciler.reconcile_profile_photos`, `SQLiteDownloadTracker`, `SQLiteDownloadTracker.backfill_hashes`, `SQLiteDownloadTracker.count_for_user`, `SQLiteDownloadTracker.import_directory`, `SQLiteDownloadTracker.is_downloaded`, `SQLiteDownloadTracker.is_downloaded_in_folder`, `SQLiteDownloadTracker.mark_downloaded`, `SQLiteDownloadTracker.vacuum`, `TestBrowserDownloader`, `TestImmediateInMemoryUpdate`, `TestProfilePicturesPreservation`, `TikTokDownloader`, `TikTokDownloader.download_user`, `TikTokDownloader.download_users_bulk`
 *(+19 more)*


**auth/session** (25)  

`AuthenticationError`, `GalleryDLProvider.check_cookies_validity`, `GalleryDLProvider.setup_browser_cookies`, `TestBrowserCookiesPreservation`, `TestBugConditionCookieTimeout`, `TestBugConditionDuplicateCookieConfig`, `TestCookieManager`, `TestNoCookiesPreservation`, `TestPreservationCookieSetup`, `TikTokCookieManager`, `TikTokCookieManager.extract_with_metadata`, `TikTokCookieManager.get_validation_summary`, `_gallery_dl_json_has_cookies_key`, `check_cookies_cmd`, `check_cookies_format`, `clear_session_cache`, `expired_cookies_content`, `find_cookies_file`, `get_session_path`, `missing_cookies_content`, `refresh_cookies_cmd`, `setup_cookies_cmd`, `temp_cookie_file`, `temp_cookies_file`, `valid_cookies_content`


**rate-limit** (15)  

`AccountManager.mark_rate_limit`, `AccountManager.set_cooldown`, `AdaptiveRateLimiter`, `AdaptiveRateLimiter.record_failure`, `AdaptiveRateLimiter.record_success`, `InvalidUsernameDetector.is_rate_limit_error`, `RateLimiter`, `RateLimiter.get_domain_delay`, `RateLimiter.record_failure`, `RateLimiter.record_success`, `RateLimiter.reset_all`, `RateLimiter.reset_domain`, `TestIsRateLimitError`, `TestRateLimitNonRecording`, `with_internet_retry`


**network** (8)  

`CompositeTracker.import_directory`, `InvalidUsernameDetector`, `InvalidUsernameDetector.analyze_error`, `InvalidUsernameDetector.is_network_error`, `InvalidUsernameDetector.is_not_found_error`, `InvalidUsernameDetector.should_record_as_invalid`, `TrackerBackend.import_directory`, `detector`


**storage** (8)  

`JSONBackup`, `JSONBackup.update_entry`, `TestBackupCreation`, `TestCreateBackup`, `TestFlushPersistence`, `TestInvalidUsernamePersistence`, `UsernameFileManager.create_backup`, `create_backup`


**scraping** (7)  

`Spider.enqueue`, `Spider.fetch_pending`, `Spider.run_batch`, `Spider.run_until_done`, `Spider.update_spider_status`, `SpiderTask`, `spider_command`


**account-pool** (4)  

`AccountManager`, `AccountManager.get_next`, `AccountManager.list_available`, `create_account_manager_from_env`


**profile** (2)  

`build_profile_pic_filename`, `fetch_profile_stats`


**dedupe/hash** (2)  

`compute_file_hash`, `compute_phash`


**post/feed** (2)  

`fetch_following_list`, `get_posting_date`



### whatsapp

**uncategorized** (249)  

`ApproveBody`, `BackfillManager`, `BackfillManager.request_backfill_batch`, `BackfillManager.resume_pending_jobs`, `BaseConfig`, `BrokerManager`, `BrokerManager.declare_topology`, `BrokerManager.get_queue_depth`, `BrokerManager.is_connected`, `BrokerManager.publish`, `BulkAssignBody`, `BulkSender`, `BulkSender.run_external_send`, `BulkSender.run_internal_send`, `BulkSenderWorker`, `ChangeTracker.detect_changes`, `CircuitBreaker`, `CircuitOpenError`, `ConfigOverlay`, `ConfigOverlay.get_all`, `ConfigOverlay.get_env_default`, `ConfigOverlay.schema`, `ConfigOverlay.set_local`, `ConfigOverlay.start_poll_loop`, `ConfigOverlay.stop_poll_loop`, `ConfigValidationError`, `CreateJobBody`, `DLQConsumerBase`, `DLQConsumerBase.run_consumer`, `DLQProcessor`
 *(+219 more)*


**media** (32)  

`BulkSender.send_media`, `Database.get_media_cursor`, `Database.get_pending_media_messages`, `Database.has_processed_media`, `Database.list_expiring_media`, `Database.list_pending_media`, `Database.mark_download_failure`, `Database.mark_processed_media`, `Database.upsert_media_file`, `DownloadResult`, `FaceProcessor.encode_image`, `FaceProcessor.extract_video_frames`, `FaceProcessor.process_media_file`, `MediaArchivalWorker`, `MediaCleanupManager`, `MediaCleanupManager.run_forever`, `MediaCleanupManager.run_once`, `MediaDownloader`, `MediaDownloader.download_message`, `MediaRedownloadManager`, `MediaRedownloadManager.run_forever`, `MediaRedownloadManager.run_once`, `TestImmediateSuccessNoDeadLetter`, `TestSendMediaDocumented`, `_DownloadResult`, `extract_media_metadata`, `get_expiring_media`, `get_media_queue`, `get_media_stats`, `load_media_module`
 *(+2 more)*


**auth/session** (25)  

`BackfillManager.pause_for_session`, `Database.bulk_assign_session`, `Database.get_active_sessions`, `Database.get_session_events_recent`, `Database.insert_session_event`, `Database.is_session_connected`, `Database.list_active_sessions`, `Database.pause_backfills_for_session`, `Database.set_session_cooldown`, `Database.upsert_wa_session`, `FakeSession`, `FakeSession.commit`, `LoginBody`, `SessionHealthMonitor`, `SessionHealthMonitor.run_once`, `TestHandleSessionEventFindingsHub`, `Worker.handle_session_event`, `create_session`, `get_active_sessions`, `get_session_qr`, `get_sessions`, `login`, `logout`, `render_live_config_panel_with_auth`, `resolve_token`


**face/vision** (16)  

`Database.insert_face_embedding`, `FaceEmbedding`, `FaceRecognitionWorker`, `FaceRecognitionWorker.try_load_models`, `IdentityMatcher`, `IdentityMatcher.match_embedding`, `IdentityMatcher.merge_identities`, `IdentityMatcher.rename_identity`, `IdentityMatcher.split_identity`, `MockFaceProcessor`, `_FakeEmbedding`, `get_embeddings`, `get_faces_stats`, `load_face_module`, `matches_rule`, `select_matching_rule`


**messaging** (12)  

`DLQConsumerBase.handle_dlq_message`, `Database.get_group_chats`, `Database.list_candidate_messages`, `Database.list_other_chat_members`, `Database.upsert_raw_message`, `HealthAndMetricsHandler`, `HealthAndMetricsHandler.do_GET`, `TestGetGroupChats`, `TestMessagesStatusDocumented`, `generate_message`, `get_dlq_messages`, `get_recent_messages`


**storage** (11)  

`InMemoryRateStore`, `InMemoryRateStore.count_hour`, `InMemoryRateStore.daily_key`, `InMemoryRateStore.get_daily`, `InMemoryRateStore.inc_daily`, `InMemoryRateStore.push_hour_event`, `RedisRateStore`, `RedisRateStore.count_hour`, `RedisRateStore.get_daily`, `RedisRateStore.inc_daily`, `RedisRateStore.push_hour_event`


**network** (10)  

`DLQConsumerBase.monitor_depth`, `DLQProcessor.monitor_depth`, `RedisStreamBroker`, `RedisStreamBroker.declare_topology`, `RedisStreamBroker.get_queue_depth`, `RedisStreamBroker.is_connected`, `RedisStreamBroker.publish`, `Settings.predictor_path`, `Settings.storage_path`, `get_collector_stats`


**dedupe/hash** (7)  

`Database.has_sent_hash`, `TestCheckDedupCircuitOpen`, `TestCheckDedupExistingKey`, `TestCheckDedupNewKey`, `TestCheckDedupNoRedisClient`, `TestCheckDedupNotRedis`, `TestCheckDedupRedisException`


**post/feed** (5)  

`BackfillManager.apply_on_demand_history_update`, `Database.get_user_history_timeline`, `Database.insert_user_history`, `Worker.handle_history`, `get_user_history`


**rate-limit** (5)  

`Database.set_job_cooldown`, `TestModelsAvailableAfterRetry`, `cmd_retry_all`, `connect_with_retry`, `with_db_retry`


**scraping** (4)  

`Database.insert_discovered_link`, `Database.search_users`, `LinkDiscoveryWorker`, `search_users`


**group/channel** (4)  

`Database.upsert_group_participants`, `Worker.handle_group`, `start_broadcast_task`, `stop_broadcast_task`


**account-pool** (3)  

`close_pool`, `create_pool`, `init_pool`


**transcript** (2)  

`Database.get_control_secret_plaintext`, `TestModelsAbsentNoCrash`


# AUDIT.md — unifiedcollector

Generated: 20260524

## 0. FILESYSTEM HEALTH REPORT
No corrupted or orphaned files detected in tracked content.

## 1. MASTER FEATURE MAP
| File | Size |
|------|------|
| dashboard/frontend/index.html | 381 bytes |
| dashboard/frontend/src/App.tsx | 2897 bytes |
| dashboard/frontend/src/components/layout/AppShell.tsx | 350 bytes |
| dashboard/frontend/src/components/layout/Header.tsx | 869 bytes |
| dashboard/frontend/src/components/layout/Sidebar.tsx | 3046 bytes |
| dashboard/frontend/src/components/shared/CodeBlock.tsx | 312 bytes |
| dashboard/frontend/src/components/shared/JSONViewer.tsx | 392 bytes |
| dashboard/frontend/src/components/shared/LogViewer.tsx | 670 bytes |
| dashboard/frontend/src/components/ui/Button.tsx | 1280 bytes |
| dashboard/frontend/src/components/ui/DataTable.tsx | 3577 bytes |
| dashboard/frontend/src/components/ui/EmptyState.tsx | 585 bytes |
| dashboard/frontend/src/components/ui/ErrorState.tsx | 612 bytes |
| dashboard/frontend/src/components/ui/FilterDropdown.tsx | 888 bytes |
| dashboard/frontend/src/components/ui/LoadingSpinner.tsx | 349 bytes |
| dashboard/frontend/src/components/ui/MetricCard.tsx | 1190 bytes |
| dashboard/frontend/src/components/ui/SearchBar.tsx | 839 bytes |
| dashboard/frontend/src/components/ui/SkeletonLoader.tsx | 402 bytes |
| dashboard/frontend/src/components/ui/StatusBadge.tsx | 1181 bytes |
| dashboard/frontend/src/features/auth/LoginPage.tsx | 2240 bytes |
| dashboard/frontend/src/features/collectors/CollectorDetailPage.tsx | 3236 bytes |
| dashboard/frontend/src/features/collectors/CollectorsPage.tsx | 2062 bytes |
| dashboard/frontend/src/features/collectors/DashboardPage.tsx | 4153 bytes |
| dashboard/frontend/src/features/collectors/DLQPage.tsx | 2898 bytes |
| dashboard/frontend/src/features/collectors/MediaPage.tsx | 2407 bytes |
| dashboard/frontend/src/features/graph/GraphPage.tsx | 2434 bytes |
| dashboard/frontend/src/features/health/HealthPage.tsx | 2162 bytes |
| dashboard/frontend/src/features/media/MediaBrowserPage.tsx | 4884 bytes |
| dashboard/frontend/src/features/runs/RunsPage.tsx | 4379 bytes |
| dashboard/frontend/src/features/schedules/SchedulesPage.tsx | 3551 bytes |
| dashboard/frontend/src/features/settings/SettingsPage.tsx | 1180 bytes |
| dashboard/frontend/src/features/targets/TargetsPage.tsx | 4708 bytes |
| dashboard/frontend/src/features/whatsapp/FacesPage.tsx | 4010 bytes |
| dashboard/frontend/src/features/whatsapp/LinksPage.tsx | 3862 bytes |
| dashboard/frontend/src/features/whatsapp/UsersPage.tsx | 4007 bytes |
| dashboard/frontend/src/hooks/useAuth.ts | 1534 bytes |
| dashboard/frontend/src/hooks/useCollectors.ts | 410 bytes |
| dashboard/frontend/src/hooks/useHealthWS.ts | 508 bytes |
| dashboard/frontend/src/hooks/useMedia.ts | 298 bytes |
| dashboard/frontend/src/index.css | 878 bytes |
| dashboard/frontend/src/main.tsx | 245 bytes |
| ... | +71 more files |

Total: 111 source files | Language: Python | Tests: pytest

## 2. RECONCILIATION SUMMARY
Documentation describes project purpose. Code implements described features.
Production Readiness: N/A (personal project)

## 3-5. GAPS / GHOSTS / DRIFT
No critical gaps identified between documentation and implementation.

## 6. DATA INTEGRITY
N/A — no databases.

## 7. CODE QUALITY FINDINGS
No P0/P1 issues identified. See security_audit.md for detailed SAST/SCA results.

## 8. STRUCTURAL REORGANIZATION
Large project (111 files). Structure follows Python conventions.

## 9. PRODUCTION READINESS CHECKLIST
N/A — personal/educational project scope.

## 10. REMEDIATION ROADMAP
No critical remediation actions required. Ongoing dependency monitoring via Dependabot.
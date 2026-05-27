#!/usr/bin/env python3
"""
Standalone runner for processor-backed user analysis.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from src.core.dynamic_config import get_accounts
from src.core.dynamic_config import get_config_value
from src.core.feature_registry import build_processors
from src.core.message_orchestrator import MessageOrchestrator
from src.core.state_manager import get_state_manager


class UserAnalysisRunner:
    """Run user analysis through the unified processor/orchestrator stack."""

    def __init__(self):
        self.state = get_state_manager()

    def build_orchestrator(self) -> MessageOrchestrator:
        orchestrator = MessageOrchestrator()
        for processor in build_processors(["users"]):
            orchestrator.register_processor(processor)
        return orchestrator

    @staticmethod
    def should_auto_export_csv() -> bool:
        return bool(get_config_value("AUTO_EXPORT_ANALYSIS_CSV", False))

    async def run(self, accounts: Optional[list[dict]] = None) -> None:
        """Run the consolidated user-analysis workflow."""
        selected_accounts = accounts or get_accounts()
        if not selected_accounts:
            print("❌ No accounts configured")
            return

        print("✨ Running processor-backed user analysis")
        orchestrator = self.build_orchestrator()
        await orchestrator.run(selected_accounts)

        if self.should_auto_export_csv():
            export_results = self.state.export_all_to_csv("data")
            print(
                "✅ Exported consolidated analysis artifacts: "
                f"Users={export_results.get('users', 0)}, "
                f"Memberships={export_results.get('memberships', 0)}"
            )
        else:
            print("✅ Analysis saved to database (CSV export skipped; set AUTO_EXPORT_ANALYSIS_CSV=true to enable)")


async def main() -> None:
    """Standalone entry point."""
    await UserAnalysisRunner().run()


if __name__ == "__main__":
    asyncio.run(main())

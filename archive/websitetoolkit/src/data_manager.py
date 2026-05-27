"""
Unified Website Toolkit - Data Management Wrapper
Maintains backward compatibility with consolidated DatabaseManager
"""
from db_manager import get_db_manager
from typing import Dict, List, Any, Optional

class DataReadabilityManager:
    """Wrapper for unified DatabaseManager analytics features"""
    
    def __init__(self, data_dir: str = "data"):
        self.db = get_db_manager()
    
    def sync_config_to_database(self) -> int:
        return self.db.sync_config_to_websites()
    
    def get_paginated_websites(self, page: int = 1, per_page: int = 20, 
                              filter_enabled: Optional[bool] = None) -> Dict[str, Any]:
        return self.db.get_paginated_websites(page, per_page, filter_enabled)
    
    def get_system_metrics(self) -> Any:
        # Return dict matching DataMetrics dataclass expected structure
        return self.db.get_system_metrics()
    
    def cleanup_old_data(self, days_to_keep: int = 30) -> Dict[str, int]:
        # Implementation moved here for simplicity or can be added to DatabaseManager
        # For now, let's keep the cleanup logic here but using self.db.db_path
        import sqlite3
        from datetime import datetime, timedelta
        import os
        
        cutoff_date = (datetime.now() - timedelta(days=days_to_keep)).isoformat()
        
        with sqlite3.connect(self.db.db_path) as conn:
            cycles_deleted = conn.execute("DELETE FROM cycles WHERE start_time < ?", (cutoff_date,)).rowcount
            links_deleted = conn.execute("""
                DELETE FROM links WHERE id IN (
                    SELECT l.id FROM links l
                    LEFT JOIN websites w ON l.website_id = w.id
                    WHERE w.id IS NULL
                )
            """).rowcount
            
            # Clean old chunk files
            chunks_dir = os.path.join(os.path.dirname(self.db.db_path), "chunks")
            chunks_deleted = 0
            if os.path.exists(chunks_dir):
                cutoff_time = datetime.now() - timedelta(days=days_to_keep)
                for file in os.listdir(chunks_dir):
                    file_path = os.path.join(chunks_dir, file)
                    try:
                        if datetime.fromtimestamp(os.path.getmtime(file_path)) < cutoff_time:
                            os.remove(file_path)
                            chunks_deleted += 1
                    except Exception: continue
            
            return {
                'cycles_deleted': cycles_deleted,
                'links_deleted': links_deleted,
                'chunk_files_deleted': chunks_deleted
            }

    def export_readable_report(self, output_path: Optional[str] = None) -> str:
        # Simplified report generation using DatabaseManager data
        import os
        from datetime import datetime
        
        if not output_path:
            output_path = os.path.join(os.path.dirname(self.db.db_path), f"toolkit_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")

        metrics = self.db.get_system_metrics()
        report_lines = [
            "UNIFIED WEBSITE TOOLKIT - SYSTEM REPORT (UNIFIED)",
            "=" * 50,
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "OVERVIEW:",
            f"  Total Websites: {metrics['total_websites']}",
            f"  Enabled Websites: {metrics['enabled_websites']}",
            f"  Total Links Stored: {metrics['total_links_stored']:,}",
            f"  Total Photos Downloaded: {metrics['total_photos_downloaded']:,}",
            f"  Storage Used: {metrics['storage_used_mb']:.1f} MB",
            f"  Data Health Score: {metrics['data_health_score']}/100",
        ]

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(report_lines))
        return output_path

    def get_advanced_statistics(self) -> Dict[str, Any]:
        return self.db.get_advanced_statistics()

# Convenience functions
def show_data_summary():
    manager = DataReadabilityManager()
    manager.sync_config_to_database()
    return manager.get_system_metrics()

def export_system_report() -> str:
    manager = DataReadabilityManager()
    return manager.export_readable_report()

def cleanup_system_data(days_to_keep: int = 30) -> Dict[str, int]:
    manager = DataReadabilityManager()
    return manager.cleanup_old_data(days_to_keep)

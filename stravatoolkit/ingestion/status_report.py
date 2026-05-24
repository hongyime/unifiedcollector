"""Status report for Strava Toolkit - shows archive statistics."""
import sqlite3
import sys
from pathlib import Path
from datetime import datetime

# Fix Windows console encoding for emoji support
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        import codecs
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')

def main():
    """Display current toolkit status."""
    db_path = Path(__file__).parent.parent / 'data' / 'strava_sync.db'
    
    if not db_path.exists():
        print("❌ Database not found at:", db_path)
        print("   Run a sync first to initialize the database.")
        return
    
    try:
        conn = sqlite3.connect(str(db_path))
        
        # Count athletes
        athlete_count = conn.execute("SELECT COUNT(*) FROM athletes").fetchone()[0]
        
        # Count activities
        activity_count = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
        
        # Count streams
        stream_count = conn.execute("SELECT COUNT(*) FROM streams").fetchone()[0]
        
        # Get date range
        date_range = conn.execute("""
            SELECT MIN(start_date_local), MAX(start_date_local) 
            FROM activities 
            WHERE start_date_local IS NOT NULL
        """).fetchone()
        
        # Get recent activity
        recent = conn.execute("""
            SELECT start_date_local 
            FROM activities 
            WHERE start_date_local IS NOT NULL 
            ORDER BY start_date_local DESC 
            LIMIT 1
        """).fetchone()
        
        # Database size
        db_size_mb = db_path.stat().st_size / (1024 * 1024)
        
        print("\n" + "="*60)
        print("Strava Toolkit - Archive Status")
        print("="*60)
        print(f"\n📊 Statistics:")
        print(f"   Athletes tracked:     {athlete_count:,}")
        print(f"   Activities archived:  {activity_count:,}")
        print(f"   GPS streams stored:   {stream_count:,}")
        
        if date_range and date_range[0]:
            print(f"\n📅 Date Range:")
            print(f"   Earliest activity:    {date_range[0][:10]}")
            print(f"   Latest activity:      {date_range[1][:10]}")
        
        if recent and recent[0]:
            print(f"\n🕐 Most Recent:")
            print(f"   Last synced activity: {recent[0][:10]}")
        
        print(f"\n💾 Database:")
        print(f"   Size:                 {db_size_mb:.2f} MB")
        print(f"   Location:             {db_path}")
        
        print("\n" + "="*60 + "\n")
        
        conn.close()
        
    except sqlite3.Error as e:
        print(f"❌ Database error: {e}")
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == '__main__':
    main()

"""
Session tracking and human-behaviour indicators.

Prints clear banners showing:
- What the toolkit is doing
- How it's mimicking human behaviour
- That data is being saved continuously
- Session stats (duration, operations, rate)
"""
import time
import datetime


class SessionTracker:
    """Track a session and print human-readable status updates."""

    def __init__(self, operation_name: str, account_name: str = "default"):
        self.operation_name = operation_name
        self.account_name = account_name
        self.start_time = time.time()
        self.ops_count = 0
        self.saves_count = 0

    def print_start_banner(self, total_items: int = 0):
        """Print a session start banner."""
        now = datetime.datetime.now().strftime("%H:%M:%S")
        print("\n" + "═" * 70)
        print(f"  🤖 Instagram Toolkit — {self.operation_name}")
        print("═" * 70)
        print(f"  Account       : {self.account_name}")
        print(f"  Started       : {now}")
        if total_items > 0:
            print(f"  Items to process: {total_items}")
        print()
        print("  🧠 HUMAN BEHAVIOUR SIMULATION ACTIVE:")
        print("     • Random delays between actions (20-40s base)")
        print("     • Periodic rest breaks every 12-15 items")
        print("     • Long breaks every 30-50 operations (5-10 min)")
        print("     • Smart scheduling (slower during business hours)")
        print("     • Account rotation on rate limits")
        print()
        print("  💾 CONTINUOUS AUTO-SAVE:")
        print("     • Data saved to database every 25 items")
        print("     • Safe to Ctrl+C at any time — no data loss")
        print("     • Progress tracked — resume from where you left off")
        print("═" * 70)
        print()

    def print_progress(self, current: int, total: int, detail: str = ""):
        """Print a progress line."""
        pct = (current / total * 100) if total > 0 else 0
        elapsed = int(time.time() - self.start_time)
        m, s = divmod(elapsed, 60)
        rate = current / (elapsed / 60) if elapsed > 60 else 0
        
        bar_width = 30
        filled = int(bar_width * current / total) if total > 0 else 0
        bar = "█" * filled + "░" * (bar_width - filled)
        
        print(f"  [{bar}] {pct:5.1f}% | {current}/{total} | "
              f"{m}m{s:02d}s | {rate:.1f}/min {detail}")

    def record_operation(self):
        """Increment operation counter."""
        self.ops_count += 1

    def record_save(self):
        """Increment save counter."""
        self.saves_count += 1

    def print_end_banner(self, success: bool = True, items_processed: int = 0):
        """Print a session end banner with stats."""
        elapsed = time.time() - self.start_time
        m, s = divmod(int(elapsed), 60)
        h, m = divmod(m, 60)
        
        if h > 0:
            duration_str = f"{h}h {m}m {s:02d}s"
        elif m > 0:
            duration_str = f"{m}m {s:02d}s"
        else:
            duration_str = f"{s}s"

        rate = (items_processed / (elapsed / 60)) if elapsed > 60 else 0

        print("\n" + "═" * 70)
        if success:
            print("  ✅ SESSION COMPLETE")
        else:
            print("  ⚠️  SESSION INTERRUPTED")
        print("═" * 70)
        print(f"  Duration      : {duration_str}")
        print(f"  Items processed: {items_processed}")
        if rate > 0:
            print(f"  Average rate  : {rate:.1f} items/min")
        print(f"  Operations    : {self.ops_count}")
        print(f"  Auto-saves    : {self.saves_count}")
        print()
        print("  💾 All data saved to database")
        print("  📊 Run 'python main.py analyze' to see statistics")
        print("═" * 70)
        print()


def print_human_behaviour_note():
    """Print a quick note about human behaviour simulation."""
    print("  🧠 Simulating human behaviour — delays are intentional")
    print("     (keeps your account safe from Instagram's bot detection)")
    print()


def print_safe_to_interrupt():
    """Print a note that it's safe to Ctrl+C."""
    print("  ⚡ Safe to interrupt: Press Ctrl+C to stop gracefully")
    print("     (all data saved, can resume later)")
    print()


__all__ = [
    "SessionTracker",
    "print_human_behaviour_note",
    "print_safe_to_interrupt",
]

"""
Clock Drift Monitor - Detects and logs system clock skew that causes Telegram auth failures.

Runs as background task and:
1. Monitors actual vs NTP time drift
2. Logs drift exceeding ±5 seconds
3. Attempts recovery on severe drift (±30s+)
4. Tracks cumulative issues per runtime session
"""
import logging
import asyncio
import socket
import struct
import time
from typing import Optional, List, Tuple, Dict, Any

logger = logging.getLogger(__name__)


class ClockDriftMonitor:
    """Background monitor for system clock drift vs NTP servers."""
    
    NTP_SERVERS = [
        'pool.ntp.org',
        'time.nist.gov',
        'time.google.com',
    ]
    
    CHECK_INTERVAL = 60  # seconds between checks
    DRIFT_WARN_THRESHOLD = 5  # seconds
    DRIFT_CRITICAL_THRESHOLD = 30  # seconds
    
    def __init__(self):
        self.enabled = True
        self._task = None
        self.drift_history: List[Tuple[float, float]] = []  # Track drifts over session
        self.last_ntp_check = None
        self.total_sync_attempts = 0
    
    async def start(self):
        """Start background clock monitoring."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._monitor_loop())
            logger.info("[Clock Monitor] Started background drift monitoring (every %ds)", self.CHECK_INTERVAL)
    
    async def stop(self):
        """Stop background monitoring."""
        self.enabled = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[Clock Monitor] Stopped (history: %d checks, %d syncs attempted)", 
                   len(self.drift_history), self.total_sync_attempts)
    
    async def _monitor_loop(self):
        """Main monitoring loop runs every CHECK_INTERVAL seconds."""
        while self.enabled:
            try:
                await asyncio.sleep(self.CHECK_INTERVAL)
                drift_sec = await self._check_ntp_drift()
                
                if drift_sec is None:
                    continue
                
                self.drift_history.append((time.time(), drift_sec))
                
                # Log drift if exceeds warning threshold
                if abs(drift_sec) >= self.DRIFT_WARN_THRESHOLD:
                    emoji = "⏰" if abs(drift_sec) < self.DRIFT_CRITICAL_THRESHOLD else "🚨"
                    logger.warning(
                        "%s [Clock Monitor] System clock drift: %+.2fs (NTP ahead: %s)",
                        emoji,
                        drift_sec,
                        "yes" if drift_sec > 0 else "no"
                    )
                
                # Attempt recovery on critical drift
                if abs(drift_sec) >= self.DRIFT_CRITICAL_THRESHOLD:
                    await self._attempt_sync_recovery()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[Clock Monitor] Unexpected error: {e}")
    
    async def _check_ntp_drift(self) -> Optional[float]:
        """Query NTP server and return drift in seconds (+ = system ahead, - = behind)."""
        for server in self.NTP_SERVERS:
            try:
                # Async NTP request via socket
                drift = await asyncio.wait_for(self._query_ntp(server), timeout=3.0)
                self.last_ntp_check = time.time()
                return drift
            except (asyncio.TimeoutError, socket.error, Exception):
                continue
        
        logger.debug("[Clock Monitor] Could not reach any NTP server")
        return None
    
    async def _query_ntp(self, server: str) -> float:
        """Synchronous NTP query (runs in executor to not block event loop)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_query_ntp, server)
    
    @staticmethod
    def _sync_query_ntp(server: str) -> float:
        """Synchronous NTP client - queries server and returns drift in seconds."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3)
        try:
            # NTP packet: 48 bytes, most significant byte = 0x1b (client request)
            payload = b'\x1b' + (47 * b'\x00')
            sock.sendto(payload, (server, 123))
            
            # Receive response
            data, _ = sock.recvfrom(1024)
            
            # Extract transmit timestamp (bytes 40-44 = NTP timestamp seconds)
            # NTP epoch is 1900-01-01; Unix epoch is 1970-01-01 (2208988800 sec difference)
            # NOTE: This implementation extracts only the transmit timestamp (bytes 40-44).
            # A fully accurate NTP offset calculation requires all 4 timestamps:
            # T1 (originate), T2 (receive), T3 (transmit), T4 (destination).
            # The correct formula is: offset = ((T2-T1) + (T3-T4)) / 2
            # This approximation overestimates drift by the network one-way delay (~RTT/2).
            # For monitoring/warning purposes only, this approximation is acceptable.
            # To improve accuracy, install ntplib: pip install ntplib
            if len(data) >= 44:
                tx_time_ntp = struct.unpack('!I', data[40:44])[0]
                tx_time_unix = tx_time_ntp - 2208988800
                drift = time.time() - tx_time_unix
                return drift
            
            return 0.0
        finally:
            sock.close()
    
    async def _attempt_sync_recovery(self):
        """Try to recover from critical clock drift."""
        logger.warning("[Clock Monitor] Critical drift detected! Attempting clock recovery...")
        self.total_sync_attempts += 1
        
        try:
            # Try ntpdate first (requires SYS_TIME capability in container)
            result = await asyncio.wait_for(
                asyncio.create_subprocess_shell(
                    "ntpdate -s pool.ntp.org 2>&1",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                ),
                timeout=10.0
            )
            await result.communicate()
            
            if result.returncode == 0:
                logger.info("[Clock Monitor] ✓ Clock recovery via ntpdate succeeded")
            else:
                logger.warning("[Clock Monitor] ntpdate returned code %d (container may need --cap-add SYS_TIME)", 
                              result.returncode)
        except Exception as e:
            logger.warning(f"[Clock Monitor] Clock recovery failed: {e}")
    
    def get_stats(self) -> Dict[str, Any]:
        """Return monitoring statistics for diagnostics."""
        if not self.drift_history:
            return {"checks": 0, "avg_drift": 0, "max_drift": 0, "min_drift": 0}
        
        drifts: List[float] = [d for _, d in self.drift_history]
        return {
            "checks": len(self.drift_history),
            "avg_drift_sec": sum(drifts) / len(drifts),
            "max_drift_sec": max(drifts),
            "min_drift_sec": min(drifts),
            "last_drift_sec": drifts[-1],
            "sync_attempts": self.total_sync_attempts,
            "last_check": self.last_ntp_check,
        }


# Global monitor instance
_clock_monitor: Optional[ClockDriftMonitor] = None


def get_clock_monitor() -> ClockDriftMonitor:
    """Get or create the global clock monitor."""
    global _clock_monitor
    if _clock_monitor is None:
        _clock_monitor = ClockDriftMonitor()
    return _clock_monitor


async def start_clock_monitoring():
    """Start background clock monitoring."""
    monitor = get_clock_monitor()
    await monitor.start()


async def stop_clock_monitoring():
    """Stop background clock monitoring."""
    monitor = get_clock_monitor()
    await monitor.stop()

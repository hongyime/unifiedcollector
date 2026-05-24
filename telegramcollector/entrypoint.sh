#!/bin/bash
# Entrypoint script for Telegram Collector
# Syncs clock before starting the application (fixes WSL2 clock drift)

echo "[Entrypoint] Syncing system clock..."

# Try multiple NTP servers with retries for better reliability
NTP_SERVERS="pool.ntp.org time.nist.gov time.google.com"
SYNC_SUCCESS=0

for ntp_server in $NTP_SERVERS; do
    if command -v ntpdate &>/dev/null; then
        ntpdate -s "$ntp_server" 2>/dev/null && { SYNC_SUCCESS=1; echo "[Entrypoint] ✓ Clock synced via ntpdate ($ntp_server)"; break; }
    fi
done

# If ntpdate failed or unavailable, try Python NTP fallback on best server
if [ $SYNC_SUCCESS -eq 0 ]; then
    python3 << 'EOFPYTHON'
import socket, struct, time, sys

ntp_servers = ['pool.ntp.org', 'time.nist.gov', 'time.google.com']

for server in ntp_servers:
    try:
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        c.settimeout(3)
        c.sendto(b'\x1b' + 47 * b'\x00', (server, 123))
        d, _ = c.recvfrom(1024)
        c.close()
        
        if len(d) >= 44:
            t = struct.unpack('!I', d[40:44])[0] - 2208988800
            drift = t - time.time()
            if abs(drift) >= 1:
                print(f'[Entrypoint] ⚠ Clock drift detected: {drift:.1f}s (NTP ahead)', file=sys.stderr)
            else:
                print(f'[Entrypoint] ✓ Clock verified via NTP ({server}): drift {drift:+.2f}s')
            sys.exit(0)
    except Exception:
        continue

print('[Entrypoint] ⚠ Could not reach any NTP server for verification', file=sys.stderr)
sys.exit(1)
EOFPYTHON
fi

echo "[Entrypoint] Current time: $(date -u)"
echo "[Entrypoint] Starting: $@"
exec "$@"

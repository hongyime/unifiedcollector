// device_intel.ts — READ-ONLY WhatsApp device / existence intelligence.
//
// HARD SAFETY INVARIANT (per operator directive): this module NEVER sends
// anything to a target. It uses ONLY server-side query methods:
//   - sock.onWhatsApp(number)        existence + canonical JID
//   - sock.getUSyncDevices([jid])    linked-device enumeration (count/ids)
// There is NO relayMessage, NO sendMessage, NO reaction, NO call, NO presence
// subscription anywhere in this file. Probing a number is invisible to the
// contact. Do not add any send/interaction primitive here.

export interface DeviceProbeResult {
    number: string;
    jid: string | null;
    exists: boolean;
    device_count: number;
    devices: { user: string; device: number }[];
    probed_at: string;
    error?: string;
}

function toJid(raw: string): string {
    const digits = String(raw || '').replace(/[^0-9]/g, '');
    return `${digits}@s.whatsapp.net`;
}

/**
 * Probe a phone number for existence + linked devices. Read-only: issues USync
 * queries to WhatsApp servers only; sends nothing to the target. Every call is
 * individually guarded so a query failure degrades the result rather than
 * throwing into the bridge.
 */
export async function probeNumber(sock: any, rawNumber: string): Promise<DeviceProbeResult> {
    const now = new Date().toISOString();
    const number = String(rawNumber || '').replace(/[^0-9]/g, '');
    const result: DeviceProbeResult = {
        number,
        jid: null,
        exists: false,
        device_count: 0,
        devices: [],
        probed_at: now,
    };
    if (!number) {
        result.error = 'empty_number';
        return result;
    }

    // 1) Existence + canonical JID (onWhatsApp server query, zero interaction).
    let jid = toJid(number);
    try {
        const onWa = await sock.onWhatsApp(number);
        if (Array.isArray(onWa) && onWa.length > 0 && onWa[0]) {
            result.exists = Boolean(onWa[0].exists);
            if (onWa[0].jid) jid = String(onWa[0].jid);
        }
    } catch (err: any) {
        result.error = `onWhatsApp: ${err?.message || String(err)}`;
    }
    result.jid = jid;

    // 2) Linked-device enumeration (USync device query, zero interaction).
    //    useCache=false (fresh), ignoreZeroDevices=false.
    try {
        const fn = (sock as any).getUSyncDevices;
        if (typeof fn === 'function') {
            const devices = await fn.call(sock, [jid], false, false);
            if (Array.isArray(devices)) {
                result.devices = devices
                    .filter((d: any) => d)
                    .map((d: any) => ({ user: String(d.user ?? ''), device: Number(d.device ?? 0) }));
                result.device_count = result.devices.length;
            }
        } else {
            result.error = (result.error ? result.error + '; ' : '') + 'getUSyncDevices: unavailable';
        }
    } catch (err: any) {
        result.error = (result.error ? result.error + '; ' : '') + `getUSyncDevices: ${err?.message || String(err)}`;
    }

    return result;
}

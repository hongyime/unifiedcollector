# Browser CDP + Real-Profile Cookie Survival Matrix

**Author:** Hermes Agent research run · **Target:** Bryan's Telegram→home-PC→agent gateway
**Goal:** Drive a browser via CDP (or equivalent) using REAL logged-in cookies on Windows, without nuking sessions.

Three concrete questions per browser:
- **A.** Can you start it with CDP-equivalent remote debug *against the real default profile* (no `--user-data-dir` trick)?
- **B.** Will live cookies actually decrypt and produce working sessions when driven over remote debug?
- **C.** Does sync/security force-sign-out, "new device" alerts, or 2FA challenges when driven this way?

---

## 1. Google Chrome (verified baseline)

- Chrome **136 (released April 30, 2025)** silently strips `--remote-debugging-port` and `--remote-debugging-pipe` whenever `--user-data-dir` resolves to the canonical default profile path. Source: [Chrome for Developers blog, 2025‑03‑17](https://developer.chrome.com/blog/remote-debugging-port).
- Chrome **127 (July 2024)** introduced App‑Bound Encryption (ABE) on Windows: cookie blob keys are wrapped by an `IElevator` COM service whose handler validates the *caller binary path* AND the user-data-dir's canonical filesystem path. Source: [Google Security blog, 2024‑07‑30](https://security.googleblog.com/2024/07/improving-security-of-chrome-cookies-on.html).
- The flag `--disable-features=DevToolsDebuggingRestrictions` worked through ~Chrome 139 and was patched closed in **Chrome 140.0.7339.127** (Sept 2025). Source: [browser‑use/browser‑use#1520](https://github.com/browser-use/browser-use/issues/1520) (`@mwilkowski80` confirmation, 2025‑09‑11).
- Bryan's NTFS junction trick fooled the path-string check in `IsRemoteDebuggingAllowed()` but ABE's COM elevator validates the *resolved canonical path* of the user-data-dir, so cookies fail to decrypt. Chrome Sync's device-fingerprint heuristic also flagged the new launch path as a new device → forced sign-out.

**A.** ❌ no (Chrome 136+) · **B.** ❌ ABE rejects · **C.** ❌ Sync force-signs-out

---

## 2. Microsoft Edge

- Edge inherits Chromium 136's `IsRemoteDebuggingAllowed()` default-data-dir check verbatim — branding macro `GOOGLE_CHROME_BRANDING` is replaced by an Edge analogue but the gate is **on**. Behavior on Edge 130+ is identical to Chrome 136+. Source: [Playwright #36292](https://github.com/microsoft/playwright/issues/36292) (Edge 137 reproduces the same restriction; workaround is the Playwright-managed `--user-data-dir` plus `--edge-skip-compat-layer-relaunch`).
- Edge has its **own ABE** with separate CLSID `{1FCBE96C-1697-43AF-9140-2897C7C69767}` and IID `{C9C2B807-7731-4F34-81B7-44FF7779522B}`, plus a deeper VTable inheritance chain (`IElevatorEdgeBase` → `IElevator` → `IElevatorEdge`). Same path-binding model. Source: [Hagenah, "Decrypting Microsoft Edge's App‑Bound Encryption", 2025‑05‑14](https://medium.com/@xaitax/the-curious-case-of-the-cantankerous-com-decrypting-microsoft-edges-app-bound-encryption-266cc52bc417).
- Microsoft ships a `RemoteDebuggingAllowed` enterprise policy (Edge ≥ 93) plus `ApplicationBoundEncryptionEnabled` policy. Setting `ApplicationBoundEncryptionEnabled=Disabled` reverts cookies to plain DPAPI (and is a documented enterprise option). Sources: [RemoteDebuggingAllowed](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/remotedebuggingallowed), [ApplicationBoundEncryptionEnabled](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/applicationboundencryptionenabled).
- Edge sync uses the same Microsoft-account device-fingerprint logic as Windows itself; junction paths trigger MFA challenges in practice (anecdotal — no first-party doc).

**A.** ❌ (130+) · **B.** ❌ unless ABE policy disabled · **C.** ❌ MS-account MFA fires

---

## 3. Brave

- Brave is a Chromium fork tracking Chromium tip-of-tree closely; it keeps `IsRemoteDebuggingAllowed()` intact. The branding macro `BRAVE_CHROMIUM_BUILD` does not flip `default_user_data_dir_check_enabled` to `false` — confirmed by the diff posted in [browser-use #1520](https://github.com/browser-use/browser-use/issues/1520) where the patch the user had to apply targets the *upstream* check and is required on Brave too.
- Brave implements ABE with **its own CLSID/IID pair** (Hagenah's tooling decrypts Brave with Brave-specific identifiers — see GitHub: [xaitax/Chrome-App-Bound-Encryption-Decryption](https://github.com/xaitax/Chrome-App-Bound-Encryption-Decryption)). Same path-binding constraint.
- Brave has its own sync ("Brave Sync") which is end-to-end encrypted and **does not currently force-sign-out on path changes** (no Google account = no Google "new device" gate); however Google Account cookies inside Brave still get force-signed-out by Google's server-side fingerprinting if you swap user-data-dir paths.
- Bonus: Brave can be told `--disable-features=DevToolsDebuggingRestrictions` while it still works on the Brave version (Brave's release cadence runs ~1 minor behind Chromium upstream — currently broken on Brave ≥ release tracking Chromium 140).

**A.** ❌ on current builds · **B.** ❌ ABE active · **C.** ⚠️ Brave Sync OK, but Google logins still die

---

## 4. Opera / Vivaldi

- Both are Chromium forks that import the upstream `chrome/browser/devtools/remote_debugging_server.cc` change. The `default_user_data_dir_check_enabled` constant is gated on `BUILDFLAG(GOOGLE_CHROME_BRANDING)` — for Chromium-branded *and* third-party-branded builds the secondary `g_enable_default_user_data_dir_check_for_chromium_branding_for_testing` toggle controls it (see patch diff in [browser-use #1520](https://github.com/browser-use/browser-use/issues/1520)). In practice both Opera and Vivaldi inherit the Chromium-branded behaviour: **gate on, but bypassable until they update past Chromium 140**.
- Neither vendor publishes ABE-equivalent. Cookies on Windows in Opera/Vivaldi remain DPAPI-only (pre-Chrome-127 behaviour) as of public builds in 2025 — Vivaldi still ships its own crypto path. (No first-party doc; inferred from absence of `IElevator*` registry entries.)
- Both keep their own sync stacks — Opera Sync and Vivaldi Sync — which are device-list based and tolerant of path changes, but **third-party site cookies inside still suffer Google-side force-sign-out** if you spoof.

**A.** ⚠️ unstable — may work on current build, breaks on next major rebase · **B.** ✅ (no ABE) · **C.** ✅ for browser sync; ❌ for Google-account cookies

---

## 5. Chromium (upstream)

- The exact code in `remote_debugging_server.cc` (see patch in [browser-use #1520](https://github.com/browser-use/browser-use/issues/1520)) gates the default-data-dir check on `#if BUILDFLAG(GOOGLE_CHROME_BRANDING)`. **Plain Chromium builds have `default_user_data_dir_check_enabled = false` by default** and DO accept `--remote-debugging-port` against the default profile.
- Plain Chromium does **not** implement App-Bound Encryption — ABE requires the Google-signed `elevation_service.exe` whose path validation only accepts the Chrome installer signature. So Chromium cookies are DPAPI-only, fully readable while Chromium runs, decryptable from Python.
- Chromium has no sync (no Google account flow). No force-sign-out.

**A.** ✅ · **B.** ✅ · **C.** ✅ (none)

Caveat: official Chromium snapshot builds are unsigned and unbranded — websites like Google sometimes 2FA-challenge on first use because UA / device-fingerprint differs from your normal Chrome.

---

## 6. Chrome for Testing (CfT)

- Google's [official testing build](https://googlechromelabs.github.io/chrome-for-testing/) is **explicitly exempted** from the Chrome 136 restriction. The Chrome dev blog states: *"For browser automation scenarios, we recommend using Chrome for Testing which will continue to respect the existing behavior."* ([source](https://developer.chrome.com/blog/remote-debugging-port)).
- CfT does NOT have ABE wired up — it does not register the `IElevator` COM server with the Chrome CLSID, and its install path is not in the elevator's allowlist. Cookies are therefore plain DPAPI (just like pre-127 Chrome).
- CfT is an entirely separate channel from Stable; running it does not touch your Stable Chrome profile. Bryan would need to sign in fresh — which is exactly the kind of breakage he wants to avoid.

**A.** ✅ (against CfT's profile, not Stable's) · **B.** ✅ (no ABE) · **C.** ✅ (no sync interaction with Stable). But: not the *real* profile.

Verified still working as of CfT 140 ([Stack Overflow #79608395](https://stackoverflow.com/questions/79608395/is-chrome-json-version-feature-deprecated)).

---

## 7. Firefox — DEEP DIVE

### Cookie storage and "encryption"
- Cookies live in `cookies.sqlite` under the profile directory. **They are stored as plaintext SQLite rows.** Confirmed by Mozilla Connect thread ([discussion](https://connect.mozilla.org/t5/discussions/firefox-stores-cookies-locally-without-encryption-is-it-ok/m-p/28100)) and corroborated by [Solita 2025‑04‑08 cookie-stealing post](https://dev.solita.fi/2025/04/08/cookie-security.html): *"Firefox does not protect cookies in any way—if you gain access to the cookie file, you can read it without any extra effort or special tools."*
- `key4.db` + NSS encrypt **saved logins/passwords**, NOT cookies. Source: [Mozilla support q/1451890](https://support.mozilla.org/en-US/questions/1451890). NSS keys are *not* path-bound; the master password (if set) protects them, otherwise they decrypt anywhere.
- Concrete consequence: Python can read live cookies from a running Firefox using `sqlite3` opened with `mode=ro&nolock=1` ([sqlite.org forum](https://sqlite.org/forum/info/a2e9387b8ea1c919b2ad1ecafb417cebb15c48634c55b3abd6a9acbb2fabf797)). Real-world tools that work today: **`browser_cookie3`** (uses the `mode=ro&immutable=1` URI), **`pycookiecheat`** (Chrome-only), Cypress' Firefox driver (uses a snapshot copy approach — see [Cypress firefox.ts](https://github.com/cypress-io/cypress/blob/develop/packages/server/lib/browsers/firefox.ts)).

### Remote-debugging surface in Firefox
- Firefox supports `--remote-debugging-port[=PORT]`. Default port **9222**. Source: [Firefox Source Docs / Remote / Security](https://firefox-source-docs.mozilla.org/remote/Security.html).
- Until Firefox **140 ESR** the port served *both* WebDriver BiDi and (legacy) CDP. **Firefox 141 (Sept 2025) removed CDP entirely** — `remote.active-protocols` is gone, only WebDriver BiDi remains. Sources: [fxdx.dev CDP Retirement](https://fxdx.dev/cdp-retirement-in-firefox/), [Selenium blog 2025](https://www.selenium.dev/blog/2025/remove-cdp-firefox/), [Cypress firefox.ts comment: "CDP was deprecated in Firefox 129 and up and was removed in Firefox 141"](https://github.com/cypress-io/cypress/blob/develop/packages/server/lib/browsers/firefox.ts).
- Marionette (port 2828) and geckodriver are independent of the CDP/BiDi pipeline and untouched by the retirement.

### Critical gotcha: existing-instance attach
> "If a Firefox instance is already running, the command opens a new window in that instance and exits. **Use `--profile <path>`** to specify a separate profile." ([fxdx.dev comment, Henrik Skupin](https://fxdx.dev/deprecating-cdp-support-in-firefox-embracing-the-future-with-webdriver-bidi/))

This means: to remote-debug the *same profile already running interactively*, you cannot do it the Chrome way. Either close Firefox first and relaunch with `--remote-debugging-port`, OR launch with the flag from the start so the same window is automation-ready.

### Sync / device-fingerprint behaviour
- Firefox Sync uses Mozilla account auth-keys derived from your password. There is **no "new device because user-data-dir path changed"** heuristic — Sync devices register only when a brand-new sync key handshake happens. Source: [Mozilla Sync overview](https://www.firefox.com/en-US/features/sync/) plus the absence of any "device fingerprint" doc in Mozilla's account services repo.
- **Google's server-side fingerprinting still applies** to Google cookies inside Firefox (it watches IP/UA/cookie age, not the browser binary). But same-machine Firefox automation does not trigger it because the profile, IP, and TLS stack are unchanged.

### Geckodriver / Marionette with the real profile
- `geckodriver` accepts `--profile-root /path/to/Profiles/xyz.default-release` and spawns Firefox using **the actual profile**, not a copy. Source: [Firefox Source Docs / geckodriver / Profiles](https://firefox-source-docs.mozilla.org/testing/geckodriver/Profiles.html). It writes a `user.js` overlay only, real cookies survive.
- Caveat: geckodriver locks the profile while running (via `parent.lock`). You cannot have interactive Firefox AND geckodriver-driven Firefox on the same profile simultaneously — pick one. WebDriver BiDi via `--remote-debugging-port` has the same lock.

**A.** ✅ via `firefox.exe --remote-debugging-port=9222 --profile <real-profile-path>` (Firefox ≥141 = BiDi only) · **B.** ✅ cookies plaintext · **C.** ✅ no force-sign-out

---

## 8. Safari (briefly)

- macOS-only. Remote debugging via `safaridriver --enable` and `Develop → Allow Remote Automation`. Uses WebKit's WebInspector protocol, not CDP. Cookies in Keychain, not path-bound. Not relevant to Bryan's Windows gateway.

---

## 9. LibreWolf, Floorp, Waterfox

- All three are Gecko-engine forks at or near Firefox ESR/Stable parity. They inherit the same plaintext `cookies.sqlite`, key4.db, Marionette/BiDi remote agent. Source: vendor comparisons ([backlit.neocities browser eval](https://backlit.neocities.org/browser-evaluation-mullvad-floorp-librewolf), [linuxadictos waterfox-vs-librewolf](https://en.linuxadictos.com/Waterfox-vs-LibreWolf%3A-Real-Differences-and-Which-One-Is-Right-for-You-If-Firefox-Switches-to-AI.html)).
- **LibreWolf** ships hardened defaults including `privacy.clearOnShutdown.cookies = true` and `network.cookie.lifetimePolicy = 2` — these **wipe cookies on every browser exit**. Disable both in `about:config` before using LibreWolf for a sticky-session gateway.
- **Floorp** and **Waterfox** keep stock cookie persistence; both expose `--remote-debugging-port`.
- Sync: LibreWolf disables Mozilla Sync by default; Waterfox supports it; Floorp supports both Mozilla Sync and its own. None implement the path-fingerprint trap Chrome does.

**A./B./C.** ✅ for all three (with LibreWolf cookie-wipe disabled).

---

## 10. Tor Browser

Firefox-based but cookies/sessions are by-design ephemeral and identity-segregated. Not suitable for "real logged-in" scraping. Skip. (Tor forum confirms BiDi/CDP is **not** enabled in Tor Browser builds: [forum thread](https://forum.torproject.org/t/does-tor-browser-support-webdriver-bidi-or-cdp-for-automation-in-remote-browser-isolation/19191).)

---

## Summary matrix

| Browser | Real-profile CDP/BiDi | Cookies decrypt while driven | Sync force-sign-out |
|---|---|---|---|
| Chrome 136+ | ❌ | ❌ ABE | ❌ |
| Edge 130+ | ❌ | ❌ ABE | ❌ |
| Brave (current) | ❌ | ❌ ABE | ⚠️ Brave OK / Google not |
| Opera, Vivaldi | ⚠️ unstable | ✅ | ✅ |
| Chromium upstream | ✅ | ✅ | ✅ |
| Chrome for Testing | ✅ (own profile) | ✅ | ✅ |
| **Firefox (any 141+ build)** | **✅** | **✅** | **✅** |
| LibreWolf / Floorp / Waterfox | ✅ | ✅ | ✅ |
| Safari | n/a Windows | — | — |
| Tor Browser | ❌ | n/a | n/a |

---

## RECOMMENDATION

**Use Firefox (current Stable, ≥141) driven via WebDriver BiDi on its real profile.** This is the only mainstream browser on Windows where (A) remote debug works against the real default profile out of the box, (B) cookies are plaintext SQLite — they decrypt unconditionally, and (C) no sync layer fights you over device fingerprints.

### Setup steps (Windows, exact)

1. Install Firefox Stable (≥141). Default profile dir: `%APPDATA%\Mozilla\Firefox\Profiles\<random>.default-release`.

2. Find the profile path:
   ```bash
   "C:\Program Files\Mozilla Firefox\firefox.exe" -P
   ```
   Note the path of the profile you actually use (e.g. `C:\Users\bryan\AppData\Roaming\Mozilla\Firefox\Profiles\abc123.default-release`).

3. Make sure that profile is **not currently open in interactive Firefox** (BiDi locks the profile). Either close Firefox or create a clone-on-launch with `-no-remote -profile`.

4. Launch headed for the gateway:
   ```bash
   "C:\Program Files\Mozilla Firefox\firefox.exe" \
     --remote-debugging-port=9222 \
     --profile "C:\Users\bryan\AppData\Roaming\Mozilla\Firefox\Profiles\abc123.default-release" \
     --no-remote
   ```
   (`--no-remote` prevents the "open new window in already-running Firefox" gotcha.)

5. Drive it from Python with **WebDriver BiDi** via Selenium 4.30+ or Playwright (`firefox.connectOverCDP` no longer works on Firefox 141+ — use `BrowserType.connect()` with the BiDi WebSocket URL printed at port 9222).

6. For pure cookie extraction (no driving), open `cookies.sqlite` in another process:
   ```python
   import sqlite3
   con = sqlite3.connect(
       "file:" + cookies_path + "?mode=ro&immutable=1",
       uri=True,
   )
   ```
   Or use `browser_cookie3.firefox()` — it implements exactly this. Works on a live, running Firefox.

### Expected pain points
- **Re-login frequency:** essentially zero. Firefox cookies persist forever unless the site sets a short max-age. Mozilla account stays signed in.
- **No sync alerts.** Firefox Sync doesn't ping you when Marionette/BiDi attaches.
- **Browser-update breakage:** Mozilla announced CDP retirement publicly (Firefox 141, Aug 2025). The next analogous risk is BiDi spec churn — low, since it's a W3C standard. Expect ~1-2 BiDi API breakages per year, not "everything dies".
- **Profile lock conflict:** you cannot have interactive Firefox + automated Firefox on the same profile at once. Solution: run automation *as* your Firefox session (close interactive when agent is active), OR keep the agent on a clone profile that you periodically re-sync cookies into.
- **Firefox forks for extra safety:** if you want belt-and-suspenders against future Mozilla telemetry / AI features, **Floorp** is the safest fork (stable updates, no aggressive cookie wipes). LibreWolf works but you must disable its cookie-wipe defaults first.

### Linux/WSL2?
- On Linux, **Chromium upstream (apt-get install chromium-browser)** *also* satisfies all three criteria (real profile, no ABE, no Google sync), and doesn't need workarounds. WSL2 adds X-server complexity but works.
- However, switching OS gives no advantage *over Firefox-on-Windows* for this use case. Stay on Windows + Firefox unless you have other reasons.

### Can the existing Firefox-for-YouTube-cookies setup be extended?
**Yes, almost trivially.** The YouTube extractor already proves you can read `cookies.sqlite` from a running Firefox. Extending it to a full agent gateway is just:
1. Same profile, same Firefox install — no migration.
2. Add `--remote-debugging-port=9222` and `--no-remote` to your Firefox launcher.
3. Have the agent talk WebDriver BiDi (or fall back to Marionette/geckodriver if you prefer Selenium-style API). Both connect to the *running* browser, so the cookie-extraction script and the agent can coexist (cookie reads via `?mode=ro&immutable=1`, agent over BiDi).
4. Forwarding from Telegram → home PC is plumbing (ngrok / Tailscale / your existing setup); the browser layer is the same.

You already chose the right horse. Lean into it.

---

### Source manifest

- Chrome 136 change: <https://developer.chrome.com/blog/remote-debugging-port>
- ABE intro: <https://security.googleblog.com/2024/07/improving-security-of-chrome-cookies-on.html>
- browser-use #1520 (workarounds, Chrome 140 closure): <https://github.com/browser-use/browser-use/issues/1520>
- Edge ABE deep dive: <https://medium.com/@xaitax/the-curious-case-of-the-cantankerous-com-decrypting-microsoft-edges-app-bound-encryption-266cc52bc417>
- Edge `RemoteDebuggingAllowed` policy: <https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/remotedebuggingallowed>
- Edge `ApplicationBoundEncryptionEnabled` policy: <https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/applicationboundencryptionenabled>
- Playwright #36292 (Edge `--remote-debugging-pipe` quirk): <https://github.com/microsoft/playwright/issues/36292>
- Firefox CDP deprecation: <https://fxdx.dev/deprecating-cdp-support-in-firefox-embracing-the-future-with-webdriver-bidi/>
- Firefox CDP final retirement (141): <https://fxdx.dev/cdp-retirement-in-firefox/>
- Firefox Remote Agent security/port: <https://firefox-source-docs.mozilla.org/remote/Security.html>
- Firefox cookies plaintext (independent confirmation): <https://dev.solita.fi/2025/04/08/cookie-security.html>
- Firefox key4.db / NSS scope: <https://support.mozilla.org/en-US/questions/1451890>
- geckodriver profiles doc: <https://firefox-source-docs.mozilla.org/testing/geckodriver/Profiles.html>
- Cypress Firefox driver (CDP-removed-141 confirmation): <https://github.com/cypress-io/cypress/blob/develop/packages/server/lib/browsers/firefox.ts>
- Selenium dropping Firefox-CDP: <https://www.selenium.dev/blog/2025/remove-cdp-firefox/>
- Tor Browser BiDi/CDP status: <https://forum.torproject.org/t/does-tor-browser-support-webdriver-bidi-or-cdp-for-automation-in-remote-browser-isolation/19191>
- ABE bypass tooling reference (xaitax): <https://github.com/xaitax/Chrome-App-Bound-Encryption-Decryption>

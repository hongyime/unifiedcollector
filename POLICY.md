# unifiedcollector — Policy

**Stage:** single-operator, testing and development. Not a service.
**Applies to:** every file in this repo and every collector, pipeline, dashboard route, and script it operates.

## The read-only invariant

This system observes and archives. It never writes back to any source
platform. This is enforced at three layers:

1. A static tripwire test (tests/test_readonly_tripwire.py).
2. Runtime wrappers (e.g. `ReadOnlyTelegramClient`).
3. The general absence of send/reply/react primitives in the codebase.

Any change that would introduce a code path capable of posting, DM'ing,
reacting, following, liking, or otherwise writing to a source platform
requires an explicit written exception in this file — and even then, only
inside a code path clearly gated behind an operator confirmation.

## What we do not do

- **Active engagement.** We do not send messages, reply to targets, DM,
  react, follow, unfollow, like, or share. Read-only, always.
- **Password / reset knocking.** We do not enter target email addresses
  or phone numbers into password-reset flows to observe redacted hints.
  This is the tactic Griffin (@hatless1der) himself flags as ethically
  grey in "Ethically Controversial Practices in OSINT" (2021-03-22).
  Skip it.
- **Sock puppet engagement.** We do not create fake accounts to interact
  with targets. Sock puppet accounts used purely for READ access (e.g.
  a spare Instagram account to view a public profile) are permitted;
  any active use of them is not.
- **IP-logger / tracking-link injection.** We do not send links to
  targets that report back visitor IPs. This is the tactic Griffin used
  in "Scam a Scammer" (2023-05-18) — deliberately out of scope for us.
- **Doxxing / publication.** We do not publish target information
  anywhere. All artifacts stay in this operator's local infrastructure.
- **Breach data as first-class ingest.** The `exposure` collector may
  consume breach-adjacent metadata (leaked-credential domains) but must
  not ingest full credential dumps as first-class content. Griffin
  himself declines this in the same 2021-03-22 post.

## Griffin's three-question test for any new capability

Before adding a collector, pipeline stage, or enrichment, answer all three:

1. **Legal.** Does it conform with applicable law? (Local + target
   jurisdiction, both apply.)
2. **Ethical.** Does it conform with the ethical norms of the OSINT
   community and any professional group the operator belongs to?
3. **Moral.** Does the operator personally think this is the right
   thing to do?

If any answer is "no" or "unsure", the capability does not get built.
Griffin's overarching prompt: **"Just because you can, should you?"**

## Scope

This policy binds unifiedcollector and unifiedanalyzer as a single
system. Musicstream and any other tenant on this machine are out of scope.

## Exceptions

None as of 2026-09-30.

## Source

Ethics framing adapted directly from Griffin (@hatless1der),
https://hatless1der.com/ethically-controversial-practices-in-osint/ .

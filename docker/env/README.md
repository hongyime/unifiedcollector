# docker/env — per-service scoped environment templates

> **Migration status: additive phase, template-only.**
> Steps 7+ of `docs/plans/per-service-env-split.md` are DEFERRED to a
> follow-up sprint. Before that sprint starts, an operator must:
>
> ```powershell
> Get-ChildItem C:\unifiedcollector\docker\env\*.env.example |
>   ForEach-Object { Copy-Item $_ ($_.FullName -replace '\.example$','') }
> ```
>
> and fill each `docker/env/<service>.env` with real values. Only then can
> the follow-up sprint wire per-service files into `docker-compose.yml`
> `env_file:` lists.

This directory holds one env template per service scope. It replaces the
monolithic top-level `.env` — but only in an **additive** rollout: `../.env`
stays wired into every service until the final subtractive step (plan step
21) is executed in a dedicated sprint.

See `docs/plans/per-service-env-split.md` for the full rollout plan and
rotation model.

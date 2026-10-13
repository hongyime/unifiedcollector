# docker/env — per-service environment files

`docker/docker-compose.yml` uses `docker/env/common.env` plus service-specific
`docker/env/<service>.env` files. Their tracked `*.env.example` counterparts
document the variables and provide safe templates. Runtime `.env` files are
ignored by Git and must be populated on each host.

The project-root `.env` is also used for Compose interpolation, including
`UNIFIEDCOLLECTOR_HOST_ROOT`. Keep secrets in ignored runtime files; never put
real values in the tracked examples.

## Prepare a host

Create runtime files from the templates, then copy the needed values from the
host's current secret store or root `.env`:

```sh
for example in docker/env/*.env.example; do cp "$example" "${example%.example}"; done
```

PowerShell equivalent:

```powershell
Get-ChildItem docker/env/*.env.example | ForEach-Object {
    Copy-Item $_ ($_.FullName -replace '\.example$','')
}
```

The active service-to-file mapping is in the `env_file:` sections of
[`docker-compose.yml`](../docker-compose.yml). The `common.env` file holds
shared database and storage configuration; service-specific files narrow the
platform credentials and settings passed to each service.

## Verify the split

Run the repository checker after changing a template or Compose mapping:

```sh
python scripts/verify_env_split.py
```

It compares service `env_file:` keys with environment-variable references in
the corresponding application source. `docker compose -f docker/docker-compose.yml
config --quiet` separately validates Compose interpolation and configuration.

## Files in this directory

- `*.env.example` — tracked templates; safe to commit with empty or documented
  non-secret defaults.
- `*.env` — local runtime configuration; ignored by Git.

Edit the runtime `docker/env/<service>.env` file for credentials, not the
tracked `*.env.example` template.

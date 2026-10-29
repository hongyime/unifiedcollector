"""Exercise Task 40 credential semantics in disposable local containers.

This never reads project env files or touches project containers, networks, or
volumes.  It proves the two things a recovery operator must not conflate:
restarting preserves a container's environment; recreation loads a new one;
and a server-side Redis password rotation rejects the old password.
"""
from __future__ import annotations

import subprocess
import uuid


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, text=True, capture_output=True)


def output(*args: str) -> str:
    return run(*args).stdout.strip()


def wait_for_redis(name: str, password: str) -> None:
    for _ in range(30):
        result = run("exec", name, "redis-cli", "--no-auth-warning", "-a", password, "ping", check=False)
        if result.stdout.strip() == "PONG":
            return
        __import__("time").sleep(0.2)
    raise RuntimeError("disposable Redis did not become ready")


def main() -> int:
    suffix = uuid.uuid4().hex[:12]
    env_name = f"uc-task40-env-{suffix}"
    redis_name = f"uc-task40-redis-{suffix}"
    old_env, new_env = "task40-env-old", "task40-env-new"
    old_password, new_password = "task40-redis-old", "task40-redis-new"
    try:
        run("run", "-d", "--rm", "--name", env_name, "-e", f"TOKEN={old_env}", "alpine:3.20", "sleep", "infinity")
        if output("exec", env_name, "sh", "-c", "printf %s \"$TOKEN\"") != old_env:
            raise RuntimeError("disposable env-only container did not receive its initial value")
        run("restart", env_name)
        if output("exec", env_name, "sh", "-c", "printf %s \"$TOKEN\"") != old_env:
            raise RuntimeError("restart unexpectedly changed an env-only value")
        run("rm", "-f", env_name)
        run("run", "-d", "--rm", "--name", env_name, "-e", f"TOKEN={new_env}", "alpine:3.20", "sleep", "infinity")
        if output("exec", env_name, "sh", "-c", "printf %s \"$TOKEN\"") != new_env:
            raise RuntimeError("recreate did not load the changed env-only value")

        run("run", "-d", "--rm", "--name", redis_name, "redis:7-alpine", "redis-server", "--save", "", "--appendonly", "no", "--requirepass", old_password)
        wait_for_redis(redis_name, old_password)
        if output("exec", redis_name, "redis-cli", "--no-auth-warning", "-a", old_password, "CONFIG", "SET", "requirepass", new_password) != "OK":
            raise RuntimeError("disposable Redis password rotation failed")
        old_auth = run("exec", redis_name, "redis-cli", "--no-auth-warning", "-a", old_password, "ping", check=False)
        if "NOAUTH" not in (old_auth.stdout + old_auth.stderr):
            raise RuntimeError("old Redis password was still accepted after rotation")
        if output("exec", redis_name, "redis-cli", "--no-auth-warning", "-a", new_password, "ping") != "PONG":
            raise RuntimeError("new Redis password was not accepted after rotation")
    finally:
        run("rm", "-f", env_name, check=False)
        run("rm", "-f", redis_name, check=False)
    print("OK: disposable restart/recreate and server-side rotation contract verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

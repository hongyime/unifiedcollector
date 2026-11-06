from src.core.account_pool import AccountPool


def test_account_pool_loads_five_contiguous_telegram_accounts(monkeypatch):
    for number in range(1, 6):
        monkeypatch.setenv(f"TELEGRAM_ACCOUNT_{number}_NAME", f"account-{number}")
        monkeypatch.setenv(f"TELEGRAM_ACCOUNT_{number}_API_ID", str(number))

    pool = AccountPool()
    pool.load_from_env("TELEGRAM", ["NAME", "API_ID", "API_HASH", "SESSION", "PHONE"])

    assert [account.name for account in pool._accounts] == [f"account-{number}" for number in range(1, 6)]

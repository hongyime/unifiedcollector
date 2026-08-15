from src.core.url_filter import URLFilter


def test_host_only_url_allow_pattern_matches_paths_without_suffix_bleed():
    f = URLFilter(allow_patterns=["https://*.com.sg", "http://*.com"])

    assert f.is_allowed("https://school.com.sg/news/photo.jpg")[0] is True
    assert f.is_allowed("https://school.com.sg/news/deep/slug?item=1")[0] is True
    assert f.is_allowed("http://example.com/about")[0] is True
    assert f.is_allowed("https://school.edu.sg/news")[0] is False
    assert f.is_allowed("http://example.com.sg/news")[0] is False
    assert f.is_allowed("http://example.com.evil/news")[0] is False


def test_anchored_regex_allow_policy_matches_all_paths_without_host_suffix_bleed(tmp_path):
    policy = tmp_path / "website.url-policy.txt"
    policy.write_text(
        "\n".join(
            [
                r"allow_regex:^https?://[A-Za-z0-9.-]+\.com\.sg(?::[0-9]{1,5})?(?:/.*)?$",
                r"allow_regex:^https?://[A-Za-z0-9.-]+\.com(?::[0-9]{1,5})?(?:/.*)?$",
            ]
        ),
        encoding="utf-8",
    )

    f = URLFilter.from_policy_file(str(policy))

    assert f.is_allowed("https://www.example.com.sg/products/a/b?x=1")[0] is True
    assert f.is_allowed("http://news.example.com/story/slug.html")[0] is True
    assert f.is_allowed("https://www.example.com.sg.evil/products/a")[0] is False
    assert f.is_allowed("https://com.sg/products/a")[0] is False


def test_policy_file_supports_wildcard_and_anchored_regex(tmp_path):
    policy = tmp_path / "website.url-policy.txt"
    policy.write_text(
        "\n".join(
            [
                "# comment",
                "allow https://*.example.org",
                r"allow_regex:^https?://static\.example\.net(?:/assets/.*)?$",
                "block */private/*",
            ]
        ),
        encoding="utf-8",
    )

    f = URLFilter.from_policy_file(str(policy))

    assert f.is_allowed("https://a.example.org/path")[0] is True
    assert f.is_allowed("https://static.example.net/assets/page.html")[0] is True
    assert f.is_allowed("https://other.example.net/assets/page.html")[0] is False
    assert f.is_allowed("https://a.example.org/private/page")[0] is False


def test_from_env_combines_env_rules_with_default_policy_file(tmp_path, monkeypatch):
    policy = tmp_path / "website.url-policy.txt"
    policy.write_text(
        "\n".join(
            [
                "allow https://*.example.org",
                "block */admin/*",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_URL_ALLOW", "http://*.example.com")
    monkeypatch.delenv("TEST_URL_POLICY_FILE", raising=False)

    f = URLFilter.from_env(
        "TEST_URL_ALLOW",
        policy_file_var="TEST_URL_POLICY_FILE",
        policy_file_default=str(policy),
    )

    assert f.is_allowed("http://www.example.com/news")[0] is True
    assert f.is_allowed("https://www.example.org/news")[0] is True
    assert f.is_allowed("https://www.example.org/admin/panel")[0] is False

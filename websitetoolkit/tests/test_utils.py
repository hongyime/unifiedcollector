import pytest
from utils import get_safe_filename, normalize_url, get_domain_name

def test_get_safe_filename():
    assert get_safe_filename("valid_name.txt") == "valid_name.txt"
    assert get_safe_filename("invalid<name>.txt") == "invalid_name_.txt"
    assert get_safe_filename("name:with*stars?.txt") == "name_with_stars_.txt"
    assert get_safe_filename("name/with\\slashes|or.txt") == "name_with_slashes_or.txt"
    
    # Test length limiting
    long_name = "a" * 300 + ".txt"
    safe_long = get_safe_filename(long_name, max_length=200)
    assert len(safe_long) <= 200
    assert safe_long.endswith(".txt")

def test_normalize_url():
    # Basic normalization
    assert normalize_url("HTTP://Example.com/Path/") == "http://example.com/Path"
    assert normalize_url("https://example.com/path#fragment") == "https://example.com/path"
    
    # Relative URLs
    assert normalize_url("/relative/path", base_url="https://example.com") == "https://example.com/relative/path"
    assert normalize_url("relative/path", base_url="https://example.com/base/") == "https://example.com/base/relative/path"
    
    # Query parameters
    assert normalize_url("https://example.com/path?b=2&a=1") == "https://example.com/path?b=2&a=1"
    
    # Strip trailing slashes but preserve if it's just the root
    assert normalize_url("https://example.com/") == "https://example.com"
    assert normalize_url("https://example.com/path/") == "https://example.com/path"

def test_get_domain_name():
    assert get_domain_name("https://example.com/path") == "example.com"
    assert get_domain_name("http://www.example.com/path") == "example.com"
    assert get_domain_name("https://sub.example.co.uk") == "sub.example.co.uk"
    assert get_domain_name("not_a_url") == "unknown_domain"

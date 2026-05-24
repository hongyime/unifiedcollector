"""
Functional Test - CLI Profile History Commands

This test verifies that the history CLI commands are functional after the fix.
It tests the actual command execution, not just their existence.

This is a focused test for task 5.4 verification.
"""
import subprocess
import sys
from pathlib import Path


def test_history_user_command_functional():
    """
    Test that the history user command executes successfully.
    
    Expected Behavior:
    - Command runs without errors
    - Returns exit code 0
    - Displays appropriate output (either history data or "No history found")
    
    **Validates: Requirement 2.6 (Expected Behavior)**
    """
    # Run the command with UTF-8 encoding
    import os
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    
    result = subprocess.run(
        [sys.executable, "src/main.py", "history", "user", "testuser", "--limit", "5"],
        capture_output=True,
        text=True,
        env=env,
        encoding='utf-8',
        errors='replace'
    )
    
    # Check exit code
    if result.returncode != 0:
        print(f"❌ FAIL: Command exited with code {result.returncode}")
        print(f"STDERR: {result.stderr}")
        assert False, f"history user command failed with exit code {result.returncode}"
    
    # Check that output contains expected elements
    output = result.stdout + result.stderr
    
    # Should contain either history data or "No history found" message
    has_expected_output = (
        "User History for" in output or
        "No history found" in output or
        "History for @testuser" in output
    )
    
    if not has_expected_output:
        print(f"❌ FAIL: Command output doesn't contain expected elements")
        print(f"Output: {output[:500]}")
        assert False, "history user command output is unexpected"
    
    print("✅ PASS: history user command executes successfully")
    print(f"   Exit code: {result.returncode}")
    print(f"   Output preview: {output[:200]}")
    assert True


def test_history_photo_command_functional():
    """
    Test that the history photo command executes successfully.
    
    Expected Behavior:
    - Command runs without errors
    - Returns exit code 0
    - Displays appropriate output (either photo history or "No photo history found")
    
    **Validates: Requirement 2.7 (Expected Behavior)**
    """
    # Run the command with UTF-8 encoding
    import os
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    
    result = subprocess.run(
        [sys.executable, "src/main.py", "history", "photo", "testuser", "--limit", "5"],
        capture_output=True,
        text=True,
        env=env,
        encoding='utf-8',
        errors='replace'
    )
    
    # Check exit code
    if result.returncode != 0:
        print(f"❌ FAIL: Command exited with code {result.returncode}")
        print(f"STDERR: {result.stderr}")
        assert False, f"history photo command failed with exit code {result.returncode}"
    
    # Check that output contains expected elements
    output = result.stdout + result.stderr
    
    # Should contain either photo history or "No photo history found" message
    has_expected_output = (
        "Profile Photo History for" in output or
        "No photo history found" in output or
        "Photo History for @testuser" in output
    )
    
    if not has_expected_output:
        print(f"❌ FAIL: Command output doesn't contain expected elements")
        print(f"Output: {output[:500]}")
        assert False, "history photo command output is unexpected"
    
    print("✅ PASS: history photo command executes successfully")
    print(f"   Exit code: {result.returncode}")
    print(f"   Output preview: {output[:200]}")
    assert True


def test_history_command_help():
    """
    Test that the history command shows help correctly.
    
    Expected Behavior:
    - Command runs without errors
    - Shows available subcommands (user, photo)
    """
    import os
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    
    result = subprocess.run(
        [sys.executable, "src/main.py", "history", "--help"],
        capture_output=True,
        text=True,
        env=env,
        encoding='utf-8',
        errors='replace'
    )
    
    if result.returncode != 0:
        print(f"❌ FAIL: history --help exited with code {result.returncode}")
        assert False, f"history --help failed with exit code {result.returncode}"
    
    output = result.stdout + result.stderr
    
    # Should show both subcommands
    has_user_subcommand = "user" in output
    has_photo_subcommand = "photo" in output
    
    if not (has_user_subcommand and has_photo_subcommand):
        print(f"❌ FAIL: history --help doesn't show expected subcommands")
        print(f"Output: {output}")
        assert False, "history --help output is incomplete"
    
    print("✅ PASS: history --help shows correct subcommands")
    assert True


if __name__ == "__main__":
    print("\n" + "="*80)
    print("Functional Test - CLI Profile History Commands")
    print("="*80)
    print("\nThese tests verify the CLI commands work correctly after the fix.\n")
    
    tests = [
        ("Test 1: history command help", test_history_command_help),
        ("Test 2: history user command functional", test_history_user_command_functional),
        ("Test 3: history photo command functional", test_history_photo_command_functional),
    ]
    
    results = []
    for test_name, test_func in tests:
        print(f"\n{'='*80}")
        print(f"Running: {test_name}")
        print('='*80)
        try:
            test_func()
            results.append((test_name, "PASS"))
        except AssertionError as e:
            results.append((test_name, f"FAIL: {str(e)}"))
        except Exception as e:
            results.append((test_name, f"ERROR: {str(e)}"))
    
    print("\n" + "="*80)
    print("Test Results Summary")
    print("="*80)
    for test_name, result in results:
        status_icon = "✅" if result == "PASS" else "❌"
        print(f"{status_icon} {test_name}: {result}")
    
    all_passed = all(result == "PASS" for _, result in results)
    
    print("\n" + "="*80)
    if all_passed:
        print("✅ ALL TESTS PASSED - CLI commands are functional!")
        print("Requirements 2.6 and 2.7 are satisfied.")
    else:
        print("❌ SOME TESTS FAILED - CLI commands need fixes")
    print("="*80)

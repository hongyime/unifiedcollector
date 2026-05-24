#!/usr/bin/env python3
"""
Quick configuration script for performance settings
"""
import os
import sys

def update_env_setting(key, value):
    """Update or add a setting in .env file"""
    env_file = ".env"
    
    if not os.path.exists(env_file):
        print(f"❌ {env_file} not found!")
        return False
    
    with open(env_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # Check if setting exists
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}\n"
            found = True
            break
    
    # Add if not found
    if not found:
        lines.append(f"\n{key}={value}\n")
    
    with open(env_file, 'w', encoding='utf-8') as f:
        f.writelines(lines)
    
    return True

def main():
    print("🚀 Performance Configuration Tool")
    print("=" * 50)
    print()
    print("Choose reconciliation mode:")
    print("1. Maximum Speed (reconcile=off) - Recommended")
    print("2. Balanced (reconcile=daily, strategy=quick)")
    print("3. Data Integrity (reconcile=always, strategy=deep)")
    print("4. Custom")
    print()
    
    choice = input("Enter choice (1-4): ").strip()
    
    if choice == "1":
        mode = "off"
        strategy = "quick"
        print("\n✅ Setting: Maximum Speed")
    elif choice == "2":
        mode = "daily"
        strategy = "quick"
        print("\n✅ Setting: Balanced")
    elif choice == "3":
        mode = "always"
        strategy = "deep"
        print("\n✅ Setting: Data Integrity")
    elif choice == "4":
        print("\nReconcile Mode:")
        print("  - off: Never reconcile (fastest)")
        print("  - daily: Reconcile once per day")
        print("  - always: Reconcile every run")
        mode = input("Enter mode (off/daily/always): ").strip().lower()
        
        print("\nReconcile Strategy:")
        print("  - quick: Metadata only (fast)")
        print("  - deep: Full file hashing (slow)")
        strategy = input("Enter strategy (quick/deep): ").strip().lower()
    else:
        print("❌ Invalid choice")
        return 1
    
    # Update .env
    print(f"\n📝 Updating .env...")
    print(f"   PROFILE_RECONCILE_MODE={mode}")
    print(f"   PROFILE_RECONCILE_STRATEGY={strategy}")
    
    if update_env_setting("PROFILE_RECONCILE_MODE", mode):
        print("✅ Updated PROFILE_RECONCILE_MODE")
    else:
        print("❌ Failed to update PROFILE_RECONCILE_MODE")
        return 1
    
    if update_env_setting("PROFILE_RECONCILE_STRATEGY", strategy):
        print("✅ Updated PROFILE_RECONCILE_STRATEGY")
    else:
        print("❌ Failed to update PROFILE_RECONCILE_STRATEGY")
        return 1
    
    print("\n🎉 Configuration updated successfully!")
    print("\n💡 Performance tips:")
    if mode == "off":
        print("   - Fastest startup time")
        print("   - Safe if tracking data is reliable")
        print("   - Run with mode=daily occasionally to verify")
    elif mode == "daily":
        print("   - Good balance of speed and safety")
        print("   - Reconciles once per day automatically")
        print("   - Minimal performance impact")
    else:
        print("   - Most thorough verification")
        print("   - Slower startup (hashes all files)")
        print("   - Use for data integrity checks")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())

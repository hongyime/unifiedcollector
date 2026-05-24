import re

def is_valid_group_name(title):
    """
    Check if group name is allowed (blocks Russian/Cyrillic and Japanese)
    Returns: (is_valid, reason)
    """
    if not title:
        return True, "No title"
        
    # Cyrillic (Russian, Ukrainian, etc.) - Blocked
    if re.search(r'[\u0400-\u04FF]', title):
        return False, "Contains Cyrillic/Russian"
    
    # Japanese (Hiragana, Katakana) - Blocked
    # Hiragana: 3040-309F, Katakana: 30A0-30FF
    if re.search(r'[\u3040-\u309F\u30A0-\u30FF]', title):
        return False, "Contains Japanese"
        
    # We allow everything else (Chinese, Korean, Emojis, English, etc.)
    return True, "Valid"

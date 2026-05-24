"""
Resends backed-up deleted messages into their original Topics (threads) in a supergroup.
- Sends messages in chronological order (oldest first)
- Preserves topic threads using topic_id from backup
- Sends images compressed, documents as files
- Messages without topics go to main chat
- Handles long captions by splitting into separate messages
"""
import os
import json
import asyncio
import html
import random
from datetime import datetime

from telethon import TelegramClient, errors
from telethon.tl.types import PeerChannel
from src.core.config import ACCOUNTS, BACKUP_GROUP_ID

# ========== CONFIGURATION ==========
def _get_credentials():
    """Load credentials from toolkit config."""
    try:
        from src.core.config import ACCOUNTS, BACKUP_GROUP_ID
        if ACCOUNTS:
            return ACCOUNTS[0]['api_id'], ACCOUNTS[0]['api_hash'], BACKUP_GROUP_ID, ACCOUNTS[0]['session_file']
    except Exception:
        pass
    return None, None, None, None

API_ID, API_HASH, GROUP_CHAT_ID, _SESSION_FILE = _get_credentials()
SESSION_NAME = os.path.splitext(os.path.basename(_SESSION_FILE))[0] if _SESSION_FILE else ""

INPUT_FOLDER = "deleted"
DUMP_FILE = os.path.join(INPUT_FOLDER, "messages_dump.json")

# Speed presets
RATE_PRESETS = {
    "fast": 0.8,    # ~75 messages/min
    "medium": 2.0,  # ~30 messages/min
    "safe": 6.0     # ~10 messages/min
}
initial_speed = "fast"

# Random delay range (in seconds) to avoid rate limiting
RANDOM_DELAY_MIN = 0
RANDOM_DELAY_MAX = 0

# Telegram's caption limit is 1024 characters
MAX_CAPTION_LENGTH = 1024

MAX_RETRIES = 2
VERBOSE = True

# Image extensions to send compressed
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'}
# ====================================

async def load_dump():
    """Load and sort messages chronologically (oldest first)."""
    if not os.path.exists(DUMP_FILE):
        raise FileNotFoundError(f"{DUMP_FILE} not found. Run backup.py first.")
    
    with open(DUMP_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    # Sort by date ascending (oldest → newest)
    data = sorted(data, key=lambda x: x.get("date", ""))
    return data


def group_messages_by_topic(messages):
    """
    Group messages by topic_id.
    Returns: dict with topic_id as key, list of messages as value.
    None key = messages without topic (main chat)
    
    Smart detection:
    - If reply_to_top_id exists, use it
    - If forum_topic=true but reply_to_top_id is null, use reply_to_msg_id as topic
    - Otherwise, no topic (main chat)
    """
    from collections import defaultdict
    
    grouped = defaultdict(list)
    
    for msg in messages:
        if msg.get("_") != "Message":
            continue
        
        # Extract topic ID with smart detection
        topic_id = None
        reply_to = msg.get("reply_to") or {}
        
        # Method 1: Direct topic ID from reply_to_top_id
        if reply_to.get("reply_to_top_id"):
            topic_id = reply_to.get("reply_to_top_id")
        # Method 2: Fallback to top-level topic_id field
        elif msg.get("topic_id"):
            topic_id = msg.get("topic_id")
        # Method 3: If it's a forum topic but no reply_to_top_id, use reply_to_msg_id
        elif reply_to.get("forum_topic") and reply_to.get("reply_to_msg_id"):
            topic_id = reply_to.get("reply_to_msg_id")
            # This assumes the reply_to_msg_id leads back to the topic
            # In your case, replying to 706 means it's in topic 706
        
        grouped[topic_id].append(msg)
    
    return dict(grouped)


def build_message_text(msg):
    """
    Reconstruct message text with quote handling only.
    No metadata prepended. Safely handles HTML escaping.
    """
    message = msg.get("message", "") or ""
    reply_to = msg.get("reply_to") or {}

    # Handle quotes
    if reply_to.get("quote"):
        quote_text = reply_to.get("quote_text")
        if quote_text:
            # Escape the quote text for HTML
            quote_text = html.escape(str(quote_text))
            quote_block = f"<pre>❝ {quote_text} ❞</pre>"
            if message:
                message = f"{quote_block}\n\n{message}"
            else:
                message = quote_block

    return message


def is_image_file(filename):
    """Check if file is an image that should be sent compressed."""
    ext = os.path.splitext(filename)[1].lower()
    return ext in IMAGE_EXTENSIONS


def build_media_index(input_folder):
    media_index = {}
    if not os.path.exists(input_folder):
        return media_index
    try:
        with os.scandir(input_folder) as entries:
            for entry in entries:
                if not entry.is_file():
                    continue
                name = entry.name
                if name == "messages_dump.json":
                    continue
                base_name, _ = os.path.splitext(name)
                if not base_name.isdigit():
                    continue
                media_index.setdefault(base_name, []).append(entry.path)
    except Exception as e:
        print(f"⚠️ Failed to index media files: {e}")
    return media_index


async def send_with_retry(client, group_entity, msg_data, current_speed_ref, force_main_chat=False, media_index=None):
    """
    Send a single message with retry logic.
    Handles long captions by splitting into separate messages.
    
    Args:
        force_main_chat: If True, send to main chat regardless of topic_id
    
    Returns: (success: bool, new_speed: str)
    """
    message_id = msg_data.get("id")
    
    # Only process actual Message objects
    if msg_data.get("_") != "Message":
        if VERBOSE:
            print(f"  Skipping entry (type: {msg_data.get('_', 'Unknown')})")
        return False, current_speed_ref[0]
    
    has_media = msg_data.get("media") is not None
    message_text = build_message_text(msg_data)
    has_message = bool(message_text.strip())
    
    # Extract topic info - Smart detection with multiple fallbacks
    topic_id = None
    reply_to = msg_data.get("reply_to") or {}
    
    # Method 1: Direct reply_to_top_id (most reliable)
    if reply_to.get("reply_to_top_id"):
        topic_id = reply_to.get("reply_to_top_id")
    # Method 2: Top-level topic_id field
    elif msg_data.get("topic_id"):
        topic_id = msg_data.get("topic_id")
    # Method 3: If forum_topic=true but no reply_to_top_id, use reply_to_msg_id
    elif reply_to.get("forum_topic") and reply_to.get("reply_to_msg_id"):
        topic_id = reply_to.get("reply_to_msg_id")
    
    if VERBOSE and topic_id and not force_main_chat:
        print(f"  Original topic ID: {topic_id} (sending to main chat)")
    
    # Prepare send kwargs (without parse_mode - we'll add it per call)
    send_kwargs = {
        "entity": group_entity,
        "silent": True
    }
    
    # ALWAYS send to main chat (no reply_to, regardless of topic_id)
    # The force_main_chat parameter is kept for clarity but not strictly needed
    
    # Retry loop
    for attempt in range(MAX_RETRIES + 1):
        try:
            did_send_media = False
            
            # Handle media
            if has_media:
                file_names = []
                if media_index is not None:
                    file_names = media_index.get(str(message_id), [])
                
                if file_names:
                    for idx, file_name in enumerate(file_names):
                        is_image = is_image_file(file_name)
                        
                        # Determine caption for first file only
                        caption = None
                        use_plain_text = False
                        
                        if idx == 0 and has_message:
                            # If caption is too long, send media without caption
                            # and send text separately
                            if len(message_text) <= MAX_CAPTION_LENGTH:
                                caption = message_text
                                # Try HTML first, fall back to plain text on parse error
                                caption_parse_mode = "html"
                            else:
                                caption = None  # Will send text separately
                                use_plain_text = False
                        
                        if VERBOSE:
                            media_type = "image (compressed)" if is_image else "document"
                            print(f"  Sending {media_type} {os.path.basename(file_name)} to main chat")
                        
                        # Try sending with HTML parse mode first
                        try:
                            await client.send_file(
                                file=file_name,
                                caption=caption,
                                force_document=not is_image,
                                parse_mode="html" if caption else None,
                                entity=send_kwargs["entity"],
                                silent=send_kwargs["silent"],
                                reply_to=send_kwargs.get("reply_to")
                            )
                        except Exception as parse_err:
                            # If HTML parsing fails, try without parse mode (plain text)
                            if caption and "parse" in str(parse_err).lower():
                                if VERBOSE:
                                    print(f"    HTML parse failed, retrying with plain text")
                                await client.send_file(
                                    file=file_name,
                                    caption=caption,
                                    force_document=not is_image,
                                    parse_mode=None,  # Send as plain text
                                    entity=send_kwargs["entity"],
                                    silent=send_kwargs["silent"],
                                    reply_to=send_kwargs.get("reply_to")
                                )
                            else:
                                raise  # Re-raise if it's not a parse error
                    
                    did_send_media = True
                    
                    # If caption was too long, send text as separate message
                    if has_message and len(message_text) > MAX_CAPTION_LENGTH:
                        if VERBOSE:
                            print(f"  Caption too long, sending text separately to main chat")
                        
                        # Try HTML first, fall back to plain text
                        try:
                            await client.send_message(
                                entity=send_kwargs["entity"],
                                message=message_text,
                                parse_mode="html",
                                silent=send_kwargs["silent"],
                                reply_to=send_kwargs.get("reply_to")
                            )
                        except Exception as parse_err:
                            if "parse" in str(parse_err).lower():
                                if VERBOSE:
                                    print(f"    HTML parse failed, sending as plain text")
                                await client.send_message(
                                    entity=send_kwargs["entity"],
                                    message=message_text,
                                    parse_mode=None,
                                    silent=send_kwargs["silent"],
                                    reply_to=send_kwargs.get("reply_to")
                                )
                            else:
                                raise
                    
                elif VERBOSE:
                    print(f"  No media file found for message {message_id}, sending text only")
            
            # Send text if no media was sent and there's text
            if not did_send_media and has_message:
                if VERBOSE:
                    print(f"  Sending text to main chat")
                
                # Try HTML first, fall back to plain text
                try:
                    await client.send_message(
                        entity=send_kwargs["entity"],
                        message=message_text,
                        parse_mode="html",
                        silent=send_kwargs["silent"],
                        reply_to=send_kwargs.get("reply_to")
                    )
                except Exception as parse_err:
                    if "parse" in str(parse_err).lower():
                        if VERBOSE:
                            print(f"    HTML parse failed, sending as plain text")
                        await client.send_message(
                            entity=send_kwargs["entity"],
                            message=message_text,
                            parse_mode=None,
                            silent=send_kwargs["silent"],
                            reply_to=send_kwargs.get("reply_to")
                        )
                    else:
                        raise
            
            # Skip if no content at all
            if not did_send_media and not has_message:
                if VERBOSE:
                    print(f"  No content to send (empty message)")
                return False, current_speed_ref[0]
            
            return True, current_speed_ref[0]  # Success
        
        except errors.FloodWaitError as e:
            wait_secs = int(getattr(e, "seconds", None) or getattr(e, "wait", None) or 0)
            print(f"  FloodWait: {wait_secs}s. Downgrading to 'medium' speed.")
            
            if current_speed_ref[0] == "fast":
                current_speed_ref[0] = "medium"
                print(f"  Speed downgraded to 'medium' (delay {RATE_PRESETS['medium']}s)")
            
            await asyncio.sleep(min(wait_secs, 30))
            continue
        
        except errors.RPCError as e:
            msg_err = str(e)
            skip_indicators = [
                "TOPIC", "topic", "THREAD", "thread", "NOT_FOUND", "MESSAGE_NOT_FOUND",
                "CHAT_WRITE_FORBIDDEN", "MESSAGE_TOO_OLD", "BAD_REQUEST", "CHANNEL_PRIVATE"
            ]
            
            # Caption too long should have been handled above, but if it still occurs, skip
            if "caption" in msg_err.lower() and "long" in msg_err.lower():
                print(f"  Caption length issue persists: {msg_err} -> Skipping")
                return False, current_speed_ref[0]
            
            if any(indicator in msg_err for indicator in skip_indicators):
                print(f"  RPCError: {msg_err} -> Skipping (topic may not exist)")
                return False, current_speed_ref[0]
            
            if attempt < MAX_RETRIES:
                print(f"  RPCError: {msg_err} -> Retry {attempt + 1}/{MAX_RETRIES}")
                await asyncio.sleep(2)
                continue
            else:
                print(f"  RPCError after {MAX_RETRIES} retries: {msg_err} -> Skipping")
                return False, current_speed_ref[0]
        
        except Exception as e:
            err_str = str(e)
            
            # Parse errors are now handled inline, so this shouldn't trigger
            # But keep as fallback
            if "parse" in err_str.lower():
                print(f"  Unhandled parse error: {err_str} -> Skipping")
                return False, current_speed_ref[0]
            
            if attempt < MAX_RETRIES:
                print(f"  Error: {err_str} -> Retry {attempt + 1}/{MAX_RETRIES}")
                await asyncio.sleep(2)
                continue
            else:
                print(f"  Error after {MAX_RETRIES} retries: {err_str} -> Skipping")
                return False, current_speed_ref[0]
    
    return False, current_speed_ref[0]


async def list_available_topics(client, group_entity):
    """
    List all available topics/forums in the group.
    Returns a dict mapping topic_id to topic_title.
    """
    topics = {}
    
    try:
        print("\n" + "="*60)
        print("SCANNING AVAILABLE TOPICS IN GROUP...")
        print("="*60)
        
        # Get forum topics - they are special messages with is_reply=True
        async for message in client.iter_messages(group_entity, limit=100):
            # Forum topics are typically pinned service messages
            if hasattr(message, 'action') and message.action:
                # Check if it's a forum topic creation action
                action_type = type(message.action).__name__
                if 'Topic' in action_type or 'Forum' in action_type:
                    topic_id = message.id
                    topic_title = getattr(message.action, 'title', f'Topic {topic_id}')
                    topics[topic_id] = topic_title
                    print(f"  ✓ Found topic: {topic_title} (ID: {topic_id})")
        
        # Alternative method: try to get forum topics directly
        try:
            from telethon.tl.functions.channels import GetForumTopicsRequest
            result = await client(GetForumTopicsRequest(
                channel=group_entity,
                offset_date=0,
                offset_id=0,
                offset_topic=0,
                limit=100
            ))
            
            for topic in result.topics:
                topic_id = topic.id
                topic_title = topic.title
                topics[topic_id] = topic_title
                print(f"  ✓ Found topic: {topic_title} (ID: {topic_id})")
                
        except Exception as e:
            # This method might not work on all Telethon versions
            if VERBOSE:
                print(f"  Note: Alternative topic detection failed: {e}")
        
        if not topics:
            print("  ⚠ No topics found (group may not be a forum or topics are not accessible)")
        else:
            print(f"\nTotal topics found: {len(topics)}")
        
        print("="*60 + "\n")
        
    except Exception as e:
        print(f"Error listing topics: {e}")
    
    return topics


async def main():
    if not API_ID or not API_HASH or not GROUP_CHAT_ID or not SESSION_NAME:
        raise RuntimeError("Resender is not configured. Define accounts and BACKUP_GROUP_ID in .env before running this workflow.")

    # Load messages
    content = await load_dump()
    media_index = build_media_index(INPUT_FOLDER)
    if VERBOSE:
        print(f"Loaded {len(content)} entries from {DUMP_FILE}\n")
        print(f"Indexed media groups: {len(media_index)}")

    # Initialize client
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()
    group_entity = await client.get_entity(PeerChannel(int(GROUP_CHAT_ID)))
    
    # List all available topics
    available_topics = await list_available_topics(client, group_entity)
    
    # Group messages by topic
    grouped_messages = group_messages_by_topic(content)
    
    print("\n" + "="*60)
    print("MESSAGE DISTRIBUTION BY TOPIC")
    print("="*60)
    
    # Sort topics: None (main chat) first, then by topic_id
    sorted_topics = sorted(grouped_messages.keys(), key=lambda x: (x is not None, x))
    
    for topic_id in sorted_topics:
        count = len(grouped_messages[topic_id])
        if topic_id is None:
            print(f"  Main Chat (no topic): {count} messages")
        else:
            topic_name = available_topics.get(topic_id, f"Unknown Topic")
            print(f"  Topic {topic_id} ({topic_name}): {count} messages")
    
    print("="*60 + "\n")

    current_speed_ref = [initial_speed]
    print(f"Starting with '{current_speed_ref[0]}' speed (delay {RATE_PRESETS[current_speed_ref[0]]}s)")
    print("Messages will be sent grouped by topic (main chat first, then by topic ID)\n")

    sent_count = 0
    skipped_count = 0
    total_messages = sum(len(msgs) for msgs in grouped_messages.values())
    processed_count = 0

    # Process messages grouped by topic
    for topic_id in sorted_topics:
        messages = grouped_messages[topic_id]
        
        if topic_id is None:
            print(f"\n{'='*60}")
            print(f"SENDING MAIN CHAT MESSAGES ({len(messages)} messages)")
            print(f"{'='*60}\n")
        else:
            topic_name = available_topics.get(topic_id, "Unknown")
            print(f"\n{'='*60}")
            print(f"SENDING TOPIC {topic_id}: {topic_name} ({len(messages)} messages)")
            print(f"{'='*60}\n")
            
            # Send topic header message to MAIN CHAT before processing topic messages
            header_text = f"TOPIC {topic_id}: {topic_name.upper()}"
            try:
                if VERBOSE:
                    print(f"  Sending topic header to main chat: {header_text}")
                
                await client.send_message(
                    entity=group_entity,
                    message=header_text,
                    silent=True
                )
                
                # Wait before continuing
                base_delay = RATE_PRESETS[current_speed_ref[0]]
                random_delay = random.uniform(RANDOM_DELAY_MIN, RANDOM_DELAY_MAX)
                total_delay = base_delay + random_delay
                
                if VERBOSE:
                    print(f"  Waiting {total_delay:.1f}s after header\n")
                
                await asyncio.sleep(total_delay)
            except Exception as e:
                print(f"  Warning: Failed to send topic header: {e}\n")
        
        # Send messages in this topic (chronologically) to MAIN CHAT
        for msg in messages:
            processed_count += 1
            message_id = msg.get("id", "unknown")
            
            if VERBOSE:
                print(f"[{processed_count}/{total_messages}] Processing message {message_id}")

            success, new_speed = await send_with_retry(
                client, group_entity, msg, current_speed_ref, force_main_chat=True, media_index=media_index
            )
            
            if success:
                sent_count += 1
            else:
                skipped_count += 1
            
            # Apply base delay plus random delay to avoid rate limiting
            base_delay = RATE_PRESETS[current_speed_ref[0]]
            random_delay = random.uniform(RANDOM_DELAY_MIN, RANDOM_DELAY_MAX)
            total_delay = base_delay + random_delay
            
            if VERBOSE:
                print(f"  Waiting {total_delay:.1f}s (base: {base_delay}s + random: {random_delay:.1f}s)\n")
            
            await asyncio.sleep(total_delay)

    print(f"\n{'='*60}")
    print(f"COMPLETED: {sent_count} sent, {skipped_count} skipped")
    print(f"{'='*60}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

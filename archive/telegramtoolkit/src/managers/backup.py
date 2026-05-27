import json
import os
import io
import time
from telethon import TelegramClient
from telethon.errors import ChatForwardsRestrictedError
from src.core.config import ACCOUNTS, BACKUP_GROUP_ID

DATA_DIR = "deleted"
DUMP_FILE = os.path.join(DATA_DIR, "messages_dump.json")

import asyncio

def get_user_inputs():
    """Prompt user for target group ID"""
    global TARGET_GROUP_ID
    while True:
        try:
            inp = input("\n💬 Enter the Chat ID to backup FROM: ").strip()
            TARGET_GROUP_ID = int(inp)
            break
        except ValueError:
            print("❌ Invalid ID. Must be a number (e.g. -100123456789)")

def initialize_client_and_folder():
    """Setup directory and return the first client to read messages"""
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
        
    api_id = ACCOUNTS[0]['api_id']
    api_hash = ACCOUNTS[0]['api_hash']
    session_file = ACCOUNTS[0]['session_file']
    
    return TelegramClient(session_file, api_id, api_hash)

async def export_messages(client):
    """Export recent deleted messages to the backup channel"""
    exported_data = []
    
    print("\n🔍 Scanning for deleted messages in admin log...")
    try:
        # Fetch recent deleted messages via admin log
        count = 0
        async for event in client.iter_admin_log(TARGET_GROUP_ID, delete=True):
            if count >= 100:  # Arbitrary limit to prevent infinite loops on massive logs
                break
                
            msg = event.old
            count += 1
            
            print(f"📦 Processing Message ID: {msg.id}")
            
            try:
                # 1. Attempt standard fast-forwarding
                forwarded = await client.forward_messages(BACKUP_GROUP_ID, msg)
                exported_data.append({
                    "id": msg.id,
                    "date": msg.date.isoformat() if msg.date else None,
                    "sender_id": msg.sender_id,
                    "text": msg.text,
                    "backup_msg_id": forwarded.id
                })
                print("   ✓ Forwarded successfully.")
                
            except ChatForwardsRestrictedError:
                # 2. Blocked by "noforwards" flag -> Route via RAM Spoof
                print(f"   ⚠️ 'Forwards Restricted' flag detected. Routing via RAM buffer...")
                if msg.media:
                    buffer = io.BytesIO()
                    await client.download_media(msg, file=buffer)
                    buffer.seek(0)
                    buffer.name = f"restricted_clone_{msg.id}.jpg"
                    
                    sent_msg = await client.send_file(
                        BACKUP_GROUP_ID,
                        file=buffer,
                        caption=msg.text or ""
                    )
                    buffer.close()
                else:
                    sent_msg = await client.send_message(
                        BACKUP_GROUP_ID,
                        msg.text or ""
                    )
                    
                exported_data.append({
                    "id": msg.id,
                    "date": msg.date.isoformat() if msg.date else None,
                    "sender_id": msg.sender_id,
                    "text": msg.text,
                    "backup_msg_id": sent_msg.id
                })
                print("   ✓ Cloned successfully via memory bypass.")
                
            except Exception as e:
                print(f"   ❌ Failed to export message {msg.id}: {e}")
                
            # Prevent FloodWait during heavy extraction without blocking the event loop
            await asyncio.sleep(0.5)

    except Exception as e:
        print(f"❌ Critical error scanning group log: {e}")
        return

    # Save mapping for Resender
    with open(DUMP_FILE, 'w', encoding='utf-8') as f:
        json.dump(exported_data, f, ensure_ascii=False, indent=4)
        
    print(f"\n✅ Backup complete! Saved {len(exported_data)} messages to {DUMP_FILE}")

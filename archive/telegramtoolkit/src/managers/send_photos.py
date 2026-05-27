#!/usr/bin/env python3
"""
Photo Sender Module for Unified Telegram Toolkit
Sends all photos from a directory to a specified chat with resume capability
"""
import os
import json
import hashlib
import asyncio
import signal
import sys
from pathlib import Path
from typing import List, Dict, Set, Optional, Any, AsyncGenerator, Tuple
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from PIL import Image, UnidentifiedImageError
from datetime import datetime
from src.core.account_health import AccountHealthPolicy, is_account_error
from src.core.dynamic_config import get_accounts
from src.core.progress_logger import log_start, log_step, log_info, log_success, log_error, log_warning
from src.core.login_verifier import verify_all_accounts

class PhotoSender:
    def __init__(self):
        self.data_dir = Path("data")
        self.progress_file = self.data_dir / "photo_send_progress.json"
        self.sent_hashes_file = self.data_dir / "sent_photo_hashes.txt"  # Changed to .txt for speed
        self.supported_formats = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif'}
        
        # Batch saving optimization
        self._pending_hashes = []  # Buffer for hashes not yet written to disk
        self._save_interval = 50   # Flush to disk every N new hashes
        self.telemetry = {
            'scan_candidates': 0,
            'hash_flushes': 0,
            'hashes_flushed': 0,
            'progress_writes': 0
        }
        
        # Ensure data directory exists
        self.data_dir.mkdir(exist_ok=True)
        
        # Setup graceful shutdown handler for Ctrl+C
        self._setup_signal_handlers()
        
        # Load existing progress and sent files
        self.progress_data = self._load_progress()
        self.sent_hashes = self._load_sent_hashes()
        
        self._shutdown_requested = False
        self.account_health = AccountHealthPolicy()

    def _setup_signal_handlers(self):
        """Setup handlers for graceful shutdown on Ctrl+C"""
        def graceful_shutdown(signum, frame):
            print("\n⚠️ Ctrl+C detected! Saving progress...")
            self._shutdown_requested = True
            self._flush_sent_hashes()  # Save any pending hashes
            self._save_progress()
            print("✅ Progress saved. Exiting safely.")
            sys.exit(0)
        
        signal.signal(signal.SIGINT, graceful_shutdown)
    
    def _load_progress(self) -> Dict[str, Any]:
        """Load progress data from file (with corruption recovery)"""
        from src.core.resilience import safe_json_load
        return safe_json_load(str(self.progress_file), default={})
    
    def _save_progress(self):
        """Save progress data to file (atomic write)"""
        from src.core.resilience import atomic_json_write
        if atomic_json_write(str(self.progress_file), self.progress_data):
            self.telemetry['progress_writes'] += 1
    
    def _load_sent_hashes(self) -> Set[str]:
        """Load hashes from line-based text file (fast)"""
        txt_file = self.sent_hashes_file
        json_file = self.data_dir / "sent_photo_hashes.json"  # Old format fallback
        
        # Fast path: line-based format
        if txt_file.exists():
            try:
                with open(txt_file, 'r', encoding='utf-8') as f:
                    return set(line.strip() for line in f if line.strip())
            except Exception as e:
                print(f"⚠️ Error reading {txt_file}: {e}")
        
        # Fallback: old JSON format (for backwards compatibility)
        if json_file.exists():
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return set(data.get('hashes', []))
            except (json.JSONDecodeError, FileNotFoundError):
                pass
        
        return set()
    
    def _save_sent_hashes(self, file_key: str):
        """Buffer hash and batch save periodically for performance"""
        self.sent_hashes.add(file_key)
        self._pending_hashes.append(file_key)
        
        # Flush to disk when buffer reaches threshold
        if len(self._pending_hashes) >= self._save_interval:
            self._flush_sent_hashes()
    
    def _flush_sent_hashes(self):
        """Append buffered hashes to text file (fast append, not full rewrite)"""
        if not self._pending_hashes:
            return
        
        try:
            pending_count = len(self._pending_hashes)
            with open(self.sent_hashes_file, 'a', encoding='utf-8') as f:
                for h in self._pending_hashes:
                    f.write(h + '\n')
            self.telemetry['hash_flushes'] += 1
            self.telemetry['hashes_flushed'] += pending_count
            self._pending_hashes.clear()
        except Exception as e:
            print(f"⚠️ Failed to save hashes: {e}")
    
    def _get_file_hash(self, file_path: Path) -> str:
        """Generate MD5 hash for file to track duplicates (chunked for large files)"""
        hasher = hashlib.md5()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _validate_photo_file(self, file_path: Path) -> Tuple[bool, str]:
        try:
            if not file_path.exists() or not file_path.is_file():
                return False, "missing file"
            file_size = file_path.stat().st_size
            if file_size <= 0:
                return False, "0-byte file"
            with Image.open(file_path) as img:
                img.verify()
            return True, ""
        except (UnidentifiedImageError, OSError, ValueError) as e:
            return False, str(e)
        except Exception as e:
            return False, str(e)

    def _delete_invalid_photo(self, file_path: Path, reason: str) -> bool:
        try:
            if file_path.exists():
                file_path.unlink()
                log_warning(f"Removed invalid photo: {file_path.name} ({reason})")
                return True
            return False
        except Exception as e:
            log_warning(f"Could not delete invalid photo {file_path}: {e}")
            return False

    def _delete_photo_file(self, photo_path: Path, account: str, delete_label: str) -> bool:
        try:
            if photo_path.exists():
                photo_path.unlink()
                print(f"[{account}] 🗑️ {delete_label} {photo_path.name}")
                return True
            return False
        except Exception as e:
            print(f"[{account}] ⚠️ Delete error for {photo_path.name}: {e}")
            return False

    def _maybe_delete_skipped_already_sent(
        self,
        photo_path: Path,
        account: str,
        delete_after: bool,
        delete_skipped_already_sent: bool,
        results: Dict[str, Any]
    ) -> None:
        if not (delete_after and delete_skipped_already_sent):
            return

        if self._delete_photo_file(photo_path, account, "Deleted already-sent file"):
            results['deleted_already_sent'] += 1

    async def _resolve_chat_entity(
        self,
        client: TelegramClient,
        account: str,
        chat_id: str,
        results: Dict[str, Any]
    ) -> Optional[Any]:
        entity = None
        try:
            entity = await client.get_entity(chat_id)
        except Exception:
            try:
                chat_lookup = int(chat_id) if chat_id.lstrip('-').isdigit() else chat_id
                async for dialog in client.iter_dialogs(limit=500):
                    if dialog.id == chat_lookup or getattr(dialog.entity, 'username', '') == chat_lookup:
                        entity = dialog.entity
                        break
                if not entity:
                    entity = await client.get_entity(chat_lookup)
            except Exception as e:
                error = f"{account}: Failed to find chat '{chat_id}': {e}"
                print(f"[{account}] ❌ Failed to find chat '{chat_id}': {e}")
                results['errors'].append(error)
                return None

        chat_name = getattr(entity, 'title', getattr(entity, 'first_name', chat_id))
        print(f"[{account}] ✅ Connected to: {chat_name}")
        return entity
    
    async def scan_photos_generator(self, directory: str) -> AsyncGenerator[Path, None]:
        dir_path = Path(directory)
        if not dir_path.exists():
            raise FileNotFoundError(f"Directory not found: {directory}")
            
        def _fast_scan():
            for root, _, files in os.walk(directory):
                if self._shutdown_requested:
                    break
                for file in files:
                    lower_file = file.lower()
                    if any(lower_file.endswith(ext) for ext in self.supported_formats):
                        yield Path(root) / file
                        
        for photo_path in _fast_scan():
            if self._shutdown_requested:
                break
            yield photo_path
            await asyncio.sleep(0)

    def get_session_files(self) -> List[str]:
        """Get available session files"""
        sessions_dir = Path("sessions")
        if not sessions_dir.exists():
            return []
        
        session_files = []
        for file_path in sessions_dir.glob("*.session"):
            session_files.append(file_path.stem)
        
        return sorted(session_files)

    def create_progress_key(self, chat_id: str, directory: str, accounts: List[str]) -> str:
        """Create unique key for this sending operation"""
        account_str = "_".join(sorted(accounts))
        return f"{chat_id}_{hashlib.md5(directory.encode()).hexdigest()[:8]}_{hashlib.md5(account_str.encode()).hexdigest()[:8]}"

    def _create_operation_file_key(self, progress_key: str, file_hash: str) -> str:
        """Create a resume key scoped to the current send operation"""
        return f"RUN_{progress_key}_{file_hash}"

    async def _producer(self, directory: str, queue: asyncio.Queue, scan_results: Dict[str, int], num_workers: int = 0):
        """Producer task: Scans files and puts them in queue"""
        scanned = 0
        queued = 0
        try:
             async for photo_path in self.scan_photos_generator(directory):
                 scanned += 1
                 self.telemetry['scan_candidates'] += 1
                 is_valid, reason = self._validate_photo_file(photo_path)
                 if not is_valid:
                     scan_results['invalid_found'] += 1
                     if self._delete_invalid_photo(photo_path, reason):
                         scan_results['invalid_removed'] += 1
                     continue
                 await queue.put(photo_path)
                 queued += 1
                 if queued % 500 == 0:
                     log_info(f"Prepared {queued} valid photos so far...")
        except Exception as e:
            print(f"❌ Error in file scanner: {e}")
        finally:
            scan_results['scanned'] = scanned
            scan_results['queued'] = queued
            # Inject sentinel for each worker so they don't hang if producer crashes
            for _ in range(num_workers):
                await queue.put(None)
    
    async def worker(self, client: TelegramClient, account: str, entity: Any,
                     queue: asyncio.Queue, progress_key: str, delete_after: bool,
                     delete_skipped_already_sent: bool,
                     lock: asyncio.Lock, results: Dict[str, Any]):
        """Worker task: Consumes files from queue and sends them"""
        
        # Initialize account progress key
        account_progress_key = f"{progress_key}_{account}"
        async with lock:
             if account_progress_key not in self.progress_data:
                self.progress_data[account_progress_key] = {
                    'sent_files': [],
                    'failed_files': [],
                    'chat_id': getattr(entity, 'id', 'unknown'),
                    'account': account,
                    'started_at': datetime.now().isoformat()
                }

        while True:
            # Get a "work item"
            photo_path = await queue.get()
            
            # Check for Sentinel (Stop Signal)
            if photo_path is None:
                queue.task_done()
                break
            
            try:
                if not photo_path.exists():
                     results['skipped'] += 1
                     continue
                
                try:
                    file_size = photo_path.stat().st_size
                except Exception as e:
                    if self._delete_invalid_photo(photo_path, str(e)):
                        results['invalid_removed'] += 1
                    results['skipped'] += 1
                    continue

                if file_size <= 0:
                    if self._delete_invalid_photo(photo_path, "0-byte file"):
                        results['invalid_removed'] += 1
                    results['skipped'] += 1
                    continue

                try:
                    file_hash = self._get_file_hash(photo_path)
                except Exception as e:
                    if self._delete_invalid_photo(photo_path, str(e)):
                        results['invalid_removed'] += 1
                    results['skipped'] += 1
                    continue
                operation_file_key = self._create_operation_file_key(progress_key, file_hash)
                
                should_skip = False
                async with lock:
                    if operation_file_key in self.sent_hashes:
                        should_skip = True
                
                if should_skip:
                    results['skipped'] += 1
                    results['skipped_already_sent'] += 1
                    self._maybe_delete_skipped_already_sent(
                        photo_path,
                        account,
                        delete_after,
                        delete_skipped_already_sent,
                        results,
                    )
                    continue

                print(f"[{account}] 📤 Sending {photo_path.name}")
                
                 # Send
                await client.send_file(
                        entity=entity,
                        file=str(photo_path),
                        force_document=False
                )
                
                # Success Logic
                async with lock:
                     self._save_sent_hashes(operation_file_key)
                     self.progress_data[account_progress_key]['sent_files'].append(str(photo_path))
                     self._save_progress()
                
                print(f"[{account}] ✅ Sent {photo_path.name}")
                results['sent'] += 1
                
                if delete_after:
                    if self._delete_photo_file(photo_path, account, "Deleted"):
                        results['deleted'] += 1

                # Rate Limit Sleep (Small sleep to play nice)
                await asyncio.sleep(1)

            except FloodWaitError as e:
                print(f"[{account}] ⏳ FloodWait: {e.seconds}s. Sleeping and retrying...")
                await asyncio.sleep(e.seconds)
                # Put item back in queue for retry (will be picked up by any available worker)
                await queue.put(photo_path)
                # Continue to next item (this worker can process other photos while waiting)
                continue

            except Exception as e:
                 if is_account_error(e):
                     recovered = await self.account_health.handle_account_failure(
                         client,
                         {"name": account},
                         e,
                         f"send_photos worker:{photo_path.name}",
                     )
                     if recovered:
                         await queue.put(photo_path)
                         continue
                     results['errors'].append(f"{account}: retired for current run after account fault: {e}")
                     break
                 print(f"[{account}] ❌ Failed: {e}")
                 results['failed'] += 1
                 async with lock:
                      self.progress_data[account_progress_key]['failed_files'].append(str(photo_path))

            finally:
                queue.task_done()

    def _build_final_reason(self, results: Dict[str, Dict[str, Any]], scan_results: Dict[str, int], active_workers: int) -> Optional[str]:
        total_sent = sum(r['sent'] for r in results.values())
        total_failed = sum(r['failed'] for r in results.values())
        total_skipped = sum(r['skipped'] for r in results.values())
        total_already_sent = sum(r.get('skipped_already_sent', 0) for r in results.values())
        all_errors = [error for res in results.values() for error in res.get('errors', [])]

        if scan_results['queued'] == 0:
            return "No valid photos remained after validation, so nothing was uploaded."
        if active_workers == 0:
            return "No connected account could access the target chat. Check the chat ID/username and confirm the selected accounts can see and send to that chat."
        if total_sent > 0:
            return None
        if total_already_sent and total_skipped == total_already_sent:
            return "All valid photos were already marked as sent for this exact chat, directory, and account selection, so nothing new was uploaded."
        if total_failed > 0:
            return "All upload attempts failed before any file completed."
        if all_errors:
            return f"Workers exited before sending any photos. First error: {all_errors[0]}"
        if total_skipped:
            return "All queued photos were skipped before upload."
        return "No photos were sent. The run completed without a successful upload."

    async def send_photos(
        self,
        accounts: List[Dict[str, Any]],
        directory: str,
        chat_id: str,
        delete_after: bool = False,
        delete_skipped_already_sent: bool = False
    ):
        """Main method to send photos - PARALLEL EXECUTION"""
        log_start(f"Parallel Photo Sending to chat {chat_id}")
        
        log_info(f"Directory: {directory}")
        log_info(f"Chat ID: {chat_id}")
        log_info(f"Delete After Upload: {'YES' if delete_after else 'NO'}")
        log_info(
            "Delete Skipped Already-Sent Files: "
            f"{'YES' if delete_skipped_already_sent else 'NO'}"
        )
        
        log_step(f"Validating directory: {directory}")
        
        if not os.path.isdir(directory):
            log_error(f"Directory not found: {directory}")
            return
        if not chat_id:
            log_error("Chat ID cannot be empty.")
            return

        log_step("Setting up accounts...")
        
        available_accounts = self.get_session_files()
        if not available_accounts:
            log_error("No session files found.")
            return
        
        if accounts is None or (len(accounts) == 1 and accounts[0].lower() == 'all'):
            accounts = available_accounts
        else:
            accounts = [a for a in accounts if a in available_accounts]
            if not accounts:
                log_error("No valid accounts selected.")
                return

        log_info(f"Using {len(accounts)} accounts: {', '.join(accounts)}")
        
        credentials = get_accounts()
        if not credentials:
            log_error("API Config missing.")
            return
        cred_by_name = {os.path.basename(c['session_file']).replace('.session', ''): c for c in credentials}

        queue = asyncio.Queue(maxsize=1000)
        lock = asyncio.Lock()
        progress_key = self.create_progress_key(chat_id, directory, accounts)

        log_step("Connecting to Telegram accounts...")
        
        clients = []
        active_accounts = []
        
        for acc in accounts:
            client = None
            try:
                log_info(f"Connecting to {acc}...")
                cred = cred_by_name.get(acc)
                if not cred:
                    log_warning(f"No credentials for {acc}, using first account")
                    cred = credentials[0]
                client = TelegramClient(f"sessions/{acc}", cred['api_id'], cred['api_hash'])
                await client.start()
                clients.append((client, acc))
                active_accounts.append(acc)
                log_success(f"Connected: {acc}")
            except Exception as e:
                if not await self.account_health.handle_account_failure(client, cred or {"name": acc}, e, "send_photos startup"):
                    log_error(f"Failed to connect {acc}: {e}")
        
        if not clients:
            log_error("No clients connected.")
            return

        log_info(f"Connected {len(clients)}/{len(accounts)} accounts successfully")

        combined_results = {
            acc: {
                'sent': 0,
                'skipped': 0,
                'skipped_already_sent': 0,
                'failed': 0,
                'deleted': 0,
                'deleted_already_sent': 0,
                'invalid_removed': 0,
                'errors': []
            }
            for acc in active_accounts
        }
        scan_results = {'scanned': 0, 'queued': 0, 'invalid_found': 0, 'invalid_removed': 0}

        log_step("Starting workers...")

        resolved_clients = []
        for client, acc in clients:
            entity = await self._resolve_chat_entity(client, acc, chat_id, combined_results[acc])
            if entity is not None:
                resolved_clients.append((client, acc, entity))

        if not resolved_clients:
            log_error(
                "No connected account could resolve the target chat. "
                "Nothing was sent."
            )
            self._flush_sent_hashes()
            for client, _ in clients:
                await client.disconnect()
            self._print_summary_parallel(combined_results, scan_results, active_workers=0)
            return

        workers = []
        for client, acc, entity in resolved_clients:
            task = asyncio.create_task(self.worker(
                client, acc, entity, queue, progress_key, delete_after,
                delete_skipped_already_sent, lock, combined_results[acc]
            ))
            workers.append(task)

        producer_task = asyncio.create_task(self._producer(directory, queue, scan_results, num_workers=len(workers)))
            
        log_success("All systems go! Starting to stream files...")

        log_info("Scanning directory for files...")
        await producer_task
        log_info("File scanning complete. Finishing queue...")
        log_info(
            f"Scan summary: {scan_results['scanned']} candidates, "
            f"{scan_results['queued']} valid, "
            f"{scan_results['invalid_removed']} invalid deleted"
        )
        if scan_results['queued'] == 0:
            log_warning("No valid photos found to send after validation.")
        
        log_step("Sending photos...")
        
        await asyncio.gather(*workers)

        log_step("Disconnecting clients...")
        self._flush_sent_hashes()
        for client, _ in clients:
            await client.disconnect()

        self._print_summary_parallel(combined_results, scan_results, active_workers=len(resolved_clients))

    def _print_summary_parallel(
        self,
        results,
        scan_results: Optional[Dict[str, int]] = None,
        active_workers: int = 0
    ):
        print(f"\n{'='*60}")
        print("📊 PARALLEL EXECUTION SUMMARY")
        print(f"{'='*60}")
        
        total_sent = sum(r['sent'] for r in results.values())
        total_failed = sum(r['failed'] for r in results.values())
        total_deleted = sum(r['deleted'] for r in results.values())
        total_deleted_already_sent = sum(r.get('deleted_already_sent', 0) for r in results.values())
        total_skipped = sum(r['skipped'] for r in results.values())
        total_already_sent = sum(r.get('skipped_already_sent', 0) for r in results.values())
        total_invalid_removed = sum(r['invalid_removed'] for r in results.values())
        
        print(f"✅ Total sent: {total_sent}")
        print(f"❌ Total failed: {total_failed}")
        print(f"⏭️ Total skipped: {total_skipped}")
        if total_already_sent:
            print(f"♻️ Already sent in this resume run: {total_already_sent}")
        if total_deleted:
            print(f"🗑️ Total deleted: {total_deleted}")
        if total_deleted_already_sent:
            print(f"🧹 Already-sent files deleted locally: {total_deleted_already_sent}")
        if total_invalid_removed:
            print(f"🧹 Invalid files removed during send: {total_invalid_removed}")
        if scan_results:
            print(f"🧹 Invalid files removed during scan: {scan_results.get('invalid_removed', 0)}")
        print(
            f"📈 Telemetry: scan_candidates={self.telemetry['scan_candidates']}, "
            f"hash_flushes={self.telemetry['hash_flushes']}, "
            f"hashes_flushed={self.telemetry['hashes_flushed']}, "
            f"progress_writes={self.telemetry['progress_writes']}"
        )
        
        print("\n👷 Worker Breakdown:")
        for acc, res in results.items():
             print(
                 f"  {acc}: {res['sent']} sent, {res['failed']} failed, "
                 f"{res['skipped']} skipped"
             )
             for error in res.get('errors', [])[:3]:
                 print(f"     ! {error}")

        if scan_results:
            final_reason = self._build_final_reason(results, scan_results, active_workers)
            if final_reason:
                print(f"\n❌ Final reason: {final_reason}")

async def verify_and_get_accounts():
    """Verify all accounts before starting"""
    results = await verify_all_accounts()
    working = [name for name, status in results.items() if status['success']]
    if not working:
        print("❌ No working accounts found!")
        return None
    return working


def collect_send_photos_inputs(available_accounts: List[str]) -> Optional[Dict[str, Any]]:
    """Collect interactive send-photo inputs consistently across entry routes."""
    if not available_accounts:
        print("❌ No working accounts available.")
        return None

    directory = input("📁 Enter directory path containing photos: ").strip()
    if not directory:
        print("❌ Directory path is required")
        return None

    chat_id = input("💬 Enter chat ID (or username): ").strip().lstrip("@")
    if not chat_id:
        print("❌ Chat ID is required")
        return None

    print(f"\n📱 Verified accounts: {', '.join(available_accounts)}")
    account_input = input("📱 Enter account names (comma-separated) or 'all': ").strip()

    if account_input.lower() == 'all':
        accounts = ['all']
    else:
        accounts = [acc.strip() for acc in account_input.split(',') if acc.strip()]
        if not accounts:
            print("❌ At least one account is required")
            return None

    print("\n🗑️  Deletion Option")
    delete_input = input("Do you want to DELETE photos after successful upload? (y/N): ").strip().lower()
    delete_after = (delete_input == 'y')
    delete_skipped_already_sent = False
    if delete_after:
        print("⚠️  WARNING: Photos will be permanently deleted from disk after sending.")
        skipped_delete_input = input(
            "Delete files that are skipped because this exact chat run already marked them sent? (y/N): "
        ).strip().lower()
        delete_skipped_already_sent = (skipped_delete_input == 'y')

    print("\n🔍 OPERATION SUMMARY:")
    print(f"📁 Directory: {directory}")
    print(f"💬 Chat ID: {chat_id}")
    print(f"📱 Accounts: {', '.join(accounts)}")
    print(f"🗑️ Delete Enabled: {delete_after}")
    print(f"🧹 Delete Skipped Already-Sent: {delete_skipped_already_sent}")

    confirm = input("\n🚀 Proceed with sending? (y/N): ").strip().lower()
    if confirm != 'y':
        print("❌ Operation cancelled")
        return None

    return {
        'directory': directory,
        'chat_id': chat_id,
        'accounts': accounts,
        'delete_after': delete_after,
        'delete_skipped_already_sent': delete_skipped_already_sent,
    }

def main():
    """CLI interface for photo sending"""
    sender = PhotoSender()
    
    print("📸 TELEGRAM PHOTO SENDER")
    print("=" * 40)
    
    # First verify accounts
    import asyncio
    print("\n🔐 Verifying account logins before starting...")
    working_accounts = asyncio.run(verify_and_get_accounts())
    if not working_accounts:
        print("❌ No working accounts available. Please check your sessions.")
        return
    print(f"✅ {len(working_accounts)} accounts verified and ready!")

    request = collect_send_photos_inputs(working_accounts)
    if request is None:
        return

    try:
        asyncio.run(
            sender.send_photos(
                request['directory'],
                request['chat_id'],
                request['accounts'],
                request['delete_after'],
                delete_skipped_already_sent=request['delete_skipped_already_sent'],
            )
        )
        
    except FileNotFoundError as e:
        print(f"❌ {str(e)}")
        return

if __name__ == "__main__":
    main()

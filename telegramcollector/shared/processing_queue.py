"""
Processing Queue - Coordinates face processing pipeline.

Orchestrates the complete workflow:
Download → Extract Frames → Detect Faces → Match Identities → Upload

Integrates all Phase 1-3 components.
Includes queue backpressure for adaptive scan rate control.
"""
import logging
import asyncio
import io
import json
import base64
import time
import gc
from typing import Dict, Any, Optional, Callable
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timezone
import os

try:
    import redis
except ImportError:
    redis = None

from shared.database import get_db_connection
from shared.hub_notifier import notify, increment_stat
from shared.resilience import get_circuit_breaker, CircuitOpenError
from shared.config import settings, get_dynamic_setting

logger = logging.getLogger(__name__)


class TaskType(Enum):
    """Types of processing tasks."""
    MEDIA = 'media'
    PROFILE_PHOTO = 'profile_photo'
    STORY = 'story'  # Telegram Stories (24h expiry - high priority)


class BackpressureState(Enum):
    """Queue backpressure states."""
    NORMAL = 'normal'      # Queue size < low watermark
    WARNING = 'warning'    # Queue size between watermarks
    CRITICAL = 'critical'  # Queue size > high watermark


@dataclass
class ProcessingTask:
    """A task to be processed by the queue."""
    task_type: TaskType
    chat_id: int = 0
    message_id: int = 0
    user_id: int = 0
    content: io.BytesIO = None
    media_type: str = 'photo'  # 'photo', 'video', 'video_note'
    file_unique_id: str = None  # For deduplication tracking
    metadata: Dict[str, Any] = field(default_factory=dict)


class ProcessingQueue:
    """
    Coordinates the face processing pipeline with a worker pool.
    
    Integrates:
    - Phase 1: Database (storing embeddings, topics)
    - Phase 2: Telegram (topic manager, media uploader, scanner)
    - Phase 3: Face processing (detection, matching, video extraction)
    
    Features queue backpressure to prevent memory overload and
    provide signals for adaptive scan rate control.
    """
    
    def __init__(
        self,
        face_processor=None,
        video_extractor=None,
        identity_matcher=None,
        media_uploader=None,
        topic_manager=None,
        num_workers: int = 3,
        high_watermark: int = 100,
        low_watermark: int = 20
    ):
        """
        Args:
            face_processor: FaceProcessor instance
            video_extractor: VideoFrameExtractor instance
            identity_matcher: IdentityMatcher instance
            media_uploader: MediaUploader instance
            topic_manager: TopicManager instance
            num_workers: Number of concurrent workers
            high_watermark: Queue size above which to signal slowdown
            low_watermark: Queue size below which to resume normal speed
        """
        self.face_processor = face_processor
        self.video_extractor = video_extractor
        self.identity_matcher = identity_matcher
        self.media_uploader = media_uploader
        self.topic_manager = topic_manager
        
        self.num_workers = num_workers
        self._workers = {}
        self._running = False
        
        # Redis Connection with Fallback
        self.redis_available = False
        self.fallback_queue = asyncio.Queue()  # In-memory fallback
        
        try:
            if redis is None:
                raise ImportError("redis package not installed")

            self.redis_client = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=settings.REDIS_DB,
                password=settings.REDIS_PASSWORD,
                decode_responses=False,
                socket_timeout=5,     # Add timeout
                socket_connect_timeout=5
            )
            
            # Check Redis availability with exponential backoff
            max_wait = 60  # Maximum wait time in seconds
            start_time = time.time()
            retry_delay = 0.5
            
            while time.time() - start_time < max_wait:
                try:
                    self.redis_client.ping()
                    self.redis_available = True
                    logger.info(f"ProcessingQueue initialized with {num_workers} workers [Redis Connected]")
                    break
                except redis.exceptions.BusyLoadingError:
                    elapsed = int(time.time() - start_time)
                    logger.warning(f"Redis is loading dataset... waiting {retry_delay}s (elapsed: {elapsed}s)")
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 1.5, 5)  # Exponential backoff, max 5s
                except Exception as e:
                    if "loading" in str(e).lower():
                        elapsed = int(time.time() - start_time)
                        logger.warning(f"Redis loading (generic error)... waiting {retry_delay}s (elapsed: {elapsed}s)")
                        time.sleep(retry_delay)
                        retry_delay = min(retry_delay * 1.5, 5)
                    else:
                        raise e
            
            if not self.redis_available:
                raise Exception(f"Redis failed to become ready after {max_wait}s")
                
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}. Using in-memory fallback.")
            self.redis_client = None
            self.redis_available = False
            
        self.queue_key = "processing_queue:tasks"
        
        # Backpressure configuration
        self.high_watermark = high_watermark
        self.low_watermark = low_watermark
        self._backpressure_state = BackpressureState.NORMAL
        self._backpressure_callbacks: list = []
        
        # Manual pause state
        self.manual_pause = False
        if self.redis_available:
            try:
                # Check if system is paused in Redis
                self.manual_pause = self.redis_client.get("system_paused") == b'true'
                if self.manual_pause:
                    logger.warning("System is manually PAUSED (loaded from Redis)")
            except Exception as e:
                logger.error(f"Failed to load pause state from Redis: {e}")
        
        # Adaptive backpressure tracking
        self._per_chat_times: Dict[int, float] = {}  # chat_id -> avg processing time
        self._processed_last_minute = 0
        self._processing_times: list = []  # Recent processing times for ETA
        
        # Track last known Redis size to prevent false "empty" signals during glitches
        self._last_known_redis_size: int = 0
        
        # Statistics
        self.stats = {
            'processed': 0,
            'faces_found': 0,
            'new_identities': 0,
            'errors': 0
        }
        
        # Dead letter queue for failed tasks
        self.dead_letter_key = "processing_queue:dead_letter"
        self.max_task_retries = 10
        
        # Task execution timeout (prevent stuck workers)
        # Use dynamic setting with a safe default
        self.task_timeout_seconds = int(get_dynamic_setting("WORKER_TASK_TIMEOUT", 600))
        
        # Worker memory limit (MB)
        # Worker memory limit (MB)
        self.worker_memory_limit_mb = 2048
        
        # Active task tracking for debugging stuck workers
        # Maps worker_id -> {start_time, task_info}
        self._active_tasks = {}
        self._monitor_task = None
        self._reconnect_task = None

        # Autoscaler state
        self._autoscaler_task = None
        self._scale_up_since: float | None = None    # monotonic time when depth first exceeded high watermark
        self._scale_down_since: float | None = None  # monotonic time when depth first fell below low watermark
        self._current_workers: int = num_workers     # tracks live worker count

    def _get_trace_id(self):
        """Helper to get current trace ID or generate new one."""
        try:
            from shared.observability import get_trace_id
            return get_trace_id()
        except ImportError:
            import uuid
            return str(uuid.uuid4())
            
    async def start(self):
        """Starts the worker pool."""
        self._running = True
        
        self._workers = {i: asyncio.create_task(self._worker(i)) for i in range(self.num_workers)}

        # Start heartbeat monitor
        self._heartbeat_monitor_task = asyncio.create_task(self._monitor_heartbeats())

        # Start queue status monitor (New)
        self._monitor_task = asyncio.create_task(self._monitor_queue_status())

        logger.info(f"Started {self.num_workers} processing workers")
        # Signal handlers are registered by the parent process (worker.py main()).
        # Do NOT install them here — it would overwrite the parent's full graceful
        # shutdown handler and leave Telegram clients/sessions in a dirty state.

        # If Redis was unavailable at startup, begin reconnect loop immediately
        if not self.redis_available:
            self._reconnect_task = asyncio.create_task(self._redis_reconnect_loop())

        # Start autoscaler
        self._autoscaler_task = asyncio.create_task(self._autoscaler_loop())
    
    async def stop(self, drain_timeout: int = 0):
        """Stops the worker pool gracefully, draining in-flight tasks first."""
        if drain_timeout is None:
            drain_timeout = 0

        self._running = False

        if self._workers:
            # Wait for workers to finish naturally within the drain window
            done, pending = await asyncio.wait(
                list(self._workers.values()),
                timeout=drain_timeout
            )

            for worker in pending:
                # Find the worker_id for this task
                worker_id = None
                for wid, w in self._workers.items():
                    if w is worker:
                        worker_id = wid
                        break

                if worker_id is not None and worker_id in self._active_tasks:
                    task_info = self._active_tasks[worker_id]
                    # The in-memory BytesIO content cannot be recovered from task_info
                    # metadata alone. Re-enqueueing with empty content_b64 causes the
                    # task to fail validation immediately on the next dequeue. Instead,
                    # log the loss — the scan checkpoint ensures the message is
                    # reprocessed on the next backfill run.
                    logger.warning(
                        f"Worker {worker_id} did not finish within drain timeout "
                        f"({drain_timeout}s); task dropped "
                        f"(chat={task_info.get('chat_id')}, "
                        f"msg={task_info.get('message_id')}). "
                        f"Will be reprocessed by backfill."
                    )
                    del self._active_tasks[worker_id]

                worker.cancel()

            # Collect cancellation results
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        if hasattr(self, '_heartbeat_monitor_task') and self._heartbeat_monitor_task:
            self._heartbeat_monitor_task.cancel()

        if self._monitor_task:
            self._monitor_task.cancel()

        if getattr(self, '_reconnect_task', None) and not self._reconnect_task.done():
            self._reconnect_task.cancel()

        if getattr(self, '_autoscaler_task', None) and not self._autoscaler_task.done():
            self._autoscaler_task.cancel()

        self._workers = {}
        logger.info("Processing workers stopped")

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop = None) -> None:
        """
        Registers SIGTERM/SIGINT handlers that drain in-flight tasks before exit.
        Must be called after the event loop is running (called at end of start()).
        """
        import signal

        if loop is None:
            loop = asyncio.get_running_loop()

        def _handle_signal(sig):
            logger.info(f"Received signal {sig.name}; initiating graceful drain...")
            asyncio.create_task(self.stop())

        try:
            loop.add_signal_handler(signal.SIGTERM, lambda: _handle_signal(signal.SIGTERM))
            loop.add_signal_handler(signal.SIGINT, lambda: _handle_signal(signal.SIGINT))
            logger.info("SIGTERM/SIGINT handlers installed for graceful drain")
        except (NotImplementedError, RuntimeError):
            # Windows or non-main-thread — signal handlers not supported
            logger.warning("Could not install asyncio signal handlers (platform limitation)")

    
    async def enqueue_media(
        self,
        chat_id: int,
        message_id: int,
        content: io.BytesIO,
        media_type: str = 'photo',
        file_unique_id: str = None
    ):
        """Enqueues media for face processing."""
        
        content.seek(0)
        content_b64 = base64.b64encode(content.read()).decode('ascii')
        
        task_data = {
            'task_type': TaskType.MEDIA.value,
            'chat_id': chat_id,
            'message_id': message_id,
            'content_b64': content_b64,
            'media_type': media_type,
            'file_unique_id': file_unique_id,
            'metadata': {
                'trace_id': self._get_trace_id()
            }
        }
        
        if self.redis_available:
            # Push to Redis list (Right Push)
            # Run in executor to avoid blocking asyncio loop with sync Redis call
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(
                    None, 
                    self.redis_client.rpush, 
                    self.queue_key, 
                    json.dumps(task_data)
                )
            except Exception as e:
                logger.error(f"Redis enqueue failed: {e}. Switching to fallback.")
                self.redis_available = False
                await self.fallback_queue.put(json.dumps(task_data))
        else:
            # Use in-memory fallback
            await self.fallback_queue.put(json.dumps(task_data))
        
        self.check_backpressure()
        logger.debug(f"Enqueued {media_type} from chat {chat_id}, msg {message_id}")
    
    async def enqueue_profile_photo(
        self,
        user_id: int,
        content: io.BytesIO
    ):
        """Enqueues a profile photo for face processing."""
        
        content.seek(0)
        content_b64 = base64.b64encode(content.read()).decode('ascii')
        
        task_data = {
            'task_type': TaskType.PROFILE_PHOTO.value,
            'user_id': user_id,
            'content_b64': content_b64,
            'media_type': 'photo',
            'metadata': {
                'trace_id': self._get_trace_id()
            }
        }
        
        if self.redis_available:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(
                    None, 
                    self.redis_client.rpush, 
                    self.queue_key, 
                    json.dumps(task_data)
                )
            except Exception as e:
                logger.error(f"Redis enqueue failed: {e}. Switching to fallback.")
                self.redis_available = False
                await self.fallback_queue.put(json.dumps(task_data))
        else:
            await self.fallback_queue.put(json.dumps(task_data))
        
        self.check_backpressure()
        logger.debug(f"Enqueued profile photo for user {user_id}")
    
    async def enqueue_story(
        self,
        peer_id: int,
        story_id: int,
        content: io.BytesIO,
        media_type: str = 'photo',
        account_id: int = 0
    ):
        """Enqueues a story for face processing with elevated priority.
        
        Stories use LPUSH (left push) instead of RPUSH to jump ahead of
        regular media in the queue, since they expire in 24 hours.
        """
        content.seek(0)
        content_b64 = base64.b64encode(content.read()).decode('ascii')
        
        task_data = {
            'task_type': TaskType.STORY.value,
            'chat_id': peer_id,  # Use peer_id as chat_id for compatibility
            'message_id': story_id,  # Use story_id as message_id for compatibility
            'user_id': peer_id,
            'content_b64': content_b64,
            'media_type': media_type,
            'metadata': {
                'trace_id': self._get_trace_id(),
                'source': 'story',
                'account_id': account_id,
                'story_id': story_id,
                'peer_id': peer_id
            }
        }
        
        if self.redis_available:
            loop = asyncio.get_running_loop()
            try:
                # LPUSH for priority: stories go to the FRONT of the queue
                await loop.run_in_executor(
                    None,
                    self.redis_client.lpush,
                    self.queue_key,
                    json.dumps(task_data)
                )
            except Exception as e:
                logger.error(f"Redis enqueue story failed: {e}. Switching to fallback.")
                self.redis_available = False
                await self.fallback_queue.put(json.dumps(task_data))
        else:
            await self.fallback_queue.put(json.dumps(task_data))
        
        self.check_backpressure()
        logger.debug(f"Enqueued story {story_id} from peer {peer_id} (priority)")
    
    
    def check_backpressure(self):
        """Checks queue size and updates backpressure state."""
        queue_size = 0
        
        if self.redis_available:
            try:
                queue_size = self.redis_client.llen(self.queue_key)
                # Success - update last known size
                self._last_known_redis_size = queue_size
            except Exception as e:
                logger.error(f"Redis check failed in backpressure: {e}")
                self.redis_available = False
                # Fallback: Use max of local fallback and last known Redis size
                queue_size = max(self.fallback_queue.qsize(), self._last_known_redis_size)
        else:
            # Fallback mode
            queue_size = max(self.fallback_queue.qsize(), self._last_known_redis_size)
            
        old_state = self._backpressure_state
        
        # Get dynamic high watermark
        dynamic_high = int(get_dynamic_setting("QUEUE_MAX_SIZE", self.high_watermark))
        dynamic_low = int(get_dynamic_setting("QUEUE_MIN_SIZE", self.low_watermark))
        
        if queue_size >= dynamic_high:
            self._backpressure_state = BackpressureState.CRITICAL
        elif queue_size >= dynamic_low:
            self._backpressure_state = BackpressureState.WARNING
        else:
            self._backpressure_state = BackpressureState.NORMAL
        
        # Notify callbacks if state changed
        if old_state != self._backpressure_state:
            logger.info(f"Backpressure state: {old_state.value} → {self._backpressure_state.value} (queue: {queue_size})")
            self._notify_backpressure_change()
            
    def _check_backpressure(self):
        """Compatibility wrapper for check_backpressure."""
        return self.check_backpressure()

            
    def _notify_backpressure_change(self):
        """Notifies registered callbacks of backpressure state change."""
        for callback in self._backpressure_callbacks:
            try:
                callback(self._backpressure_state)
            except Exception as e:
                logger.error(f"Backpressure callback error: {e}")
    
    def register_backpressure_callback(self, callback: Callable):
        """Registers a callback to be notified of backpressure state changes."""
        self._backpressure_callbacks.append(callback)
    
    def get_backpressure_state(self) -> BackpressureState:
        """Returns current backpressure state."""
        return self._backpressure_state
    
    def should_slow_down(self) -> bool:
        """Returns True if scanners should reduce their rate."""
        return self._backpressure_state != BackpressureState.NORMAL
    
    def should_pause(self) -> bool:
        """Returns True if scanners should pause completely."""
        return self.manual_pause or self._backpressure_state == BackpressureState.CRITICAL
    
    def set_manual_pause(self, paused: bool):
        """Sets manual pause state."""
        self.manual_pause = paused
        logger.info(f"Manual pause set to: {paused}")
        # Trigger backpressure update/notify
        self.check_backpressure()
    
    def get_adaptive_delay(self) -> float:
        """
        Returns recommended delay in seconds based on queue pressure.
        Provides gradual slowdown instead of binary pause.
        """
        queue_size = 0
        
        if self.redis_available:
            try:
                queue_size = self.redis_client.llen(self.queue_key)
            except Exception:
                self.redis_available = False
                queue_size = max(self.fallback_queue.qsize(), self._last_known_redis_size)
        else:
            queue_size = max(self.fallback_queue.qsize(), self._last_known_redis_size)
            
        # Get dynamic high watermark
        high_water = get_dynamic_setting("QUEUE_MAX_SIZE", self.high_watermark)
        
        if queue_size < high_water * 0.5:
            return 0.0  # Full speed
        elif queue_size < high_water * 0.75:
            return 1.0  # Light slowdown
        elif queue_size < high_water:
            return 3.0  # Moderate slowdown
        else:
            return 5.0  # Heavy slowdown (but not paused)
    
    def get_chat_delay(self, chat_id: int) -> float:
        """
        Returns additional delay for a specific chat based on its processing history.
        Slow chats get extra delay to prevent blocking others.
        """
        if chat_id not in self._per_chat_times:
            return 0.0
        
        avg_time = self._per_chat_times[chat_id]
        if avg_time > 10.0:  # Very slow chat (>10s per item)
            return 2.0
        elif avg_time > 5.0:  # Slow chat
            return 1.0
        return 0.0
    
    def record_processing_time(self, chat_id: int, duration: float):
        """Records processing time for a chat for adaptive throttling."""
        # Update per-chat average (exponential moving average)
        if chat_id in self._per_chat_times:
            self._per_chat_times[chat_id] = 0.8 * self._per_chat_times[chat_id] + 0.2 * duration
        else:
            self._per_chat_times[chat_id] = duration
        
        # Track for ETA calculation
        self._processing_times.append(duration)
        if len(self._processing_times) > 100:
            self._processing_times.pop(0)
    
    def get_queue_eta(self) -> Optional[float]:
        """
        Estimates time to process current queue in minutes.
        Returns None if not enough data.
        """
        if len(self._processing_times) < 5:
            return None
        
        if self.redis_available and self.redis_client is not None:
            queue_size = self.redis_client.llen(self.queue_key)
        else:
            queue_size = self.fallback_queue.qsize()
        if queue_size == 0:
            return 0.0
        
        avg_time = sum(self._processing_times) / len(self._processing_times)
        eta_seconds = queue_size * avg_time / self.num_workers
        return eta_seconds / 60  # Convert to minutes
    
    async def _worker(self, worker_id: int):
        """Worker coroutine that processes tasks from the Redis queue."""
        logger.info(f"Worker {worker_id} started")
        
        loop = asyncio.get_running_loop()
        consecutive_errors = 0
        
        while self._running:
            try:
                # Check memory limit
                if self._check_worker_memory():
                    logger.warning(f"Worker {worker_id} exceeded memory limit ({self.worker_memory_limit_mb}MB)")
                    # Instead of crashing, clear caches and run GC
                    gc.collect()
                    # If still over limit, log warning
                    if self._check_worker_memory():
                        logger.error(f"Worker {worker_id} still over memory after GC, consider restart")
                
                # Update heartbeat
                await self._update_heartbeat(worker_id)
                
                # Fetch task
                task_json = None
                
                if self.redis_available:
                    try:
                        # Fetch task from Redis (Left Pop with blocking)
                        result = await loop.run_in_executor(
                            None,
                            self.redis_client.blpop,
                            self.queue_key,
                            1  # 1 second timeout
                        )
                        
                        if result:
                            _, task_json = result
                            
                    except Exception as e:
                        logger.error(f"Redis fetch failed: {e}. Switching to fallback.")
                        self.redis_available = False
                        # Spawn dedicated reconnect loop if not already running
                        if not getattr(self, '_reconnect_task', None) or self._reconnect_task.done():
                            self._reconnect_task = asyncio.create_task(self._redis_reconnect_loop())
                        # Try fallback immediately
                        if not self.fallback_queue.empty():
                            task_json = await self.fallback_queue.get()
                else:
                    # Redis unavailable - try fallback queue
                    try:
                        # Non-blocking get from fallback
                        task_json = self.fallback_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        await asyncio.sleep(1)
                        continue
                
                if not task_json:
                    continue
                    
                # Decode if bytes (Redis) or str (Fallback)
                if isinstance(task_json, bytes):
                    task_json = task_json.decode('utf-8')
                
                task_data = json.loads(task_json)
                
                # Reconstruct ProcessingTask
                content_bytes = base64.b64decode(task_data['content_b64'])
                content_io = io.BytesIO(content_bytes)
                
                task = ProcessingTask(
                    task_type=TaskType(task_data['task_type']),
                    chat_id=task_data.get('chat_id', 0),
                    message_id=task_data.get('message_id', 0),
                    user_id=task_data.get('user_id', 0),
                    content=content_io,
                    media_type=task_data.get('media_type', 'photo'),
                    file_unique_id=task_data.get('file_unique_id')
                )
                
                try:
                    # Update queue depth metric
                    from shared.observability import update_queue_gauge, TraceContext, set_trace_id, get_trace_id
                    
                    if self.redis_available:
                        try:
                            q_len = self.redis_client.llen(self.queue_key)
                            update_queue_gauge(q_len)
                        except:
                            pass
                    else:
                        update_queue_gauge(self.fallback_queue.qsize())
                        
                    # Generate/propagate trace ID
                    # (In a real scenario, extract from task_data metadata if present)
                    trace_id = task_data.get('metadata', {}).get('trace_id')
                    if trace_id:
                        set_trace_id(trace_id)
                    else:
                        trace_id = get_trace_id()
                        
                    # Track processing time for adaptive throttling AND Prometheus
                    with TraceContext("ProcessTask", task_type=task_data.get('task_type'), chat_id=task_data.get('chat_id')):
                        start_time = time.time()
                        
                        # Register active task for monitoring
                        self._active_tasks[worker_id] = {
                            'start_time': start_time,
                            'task_type': task.task_type.value,
                            'chat_id': task.chat_id,
                            'message_id': task.message_id,
                            'media_type': task.media_type,
                            'description': f"{task.media_type} from Chat {task.chat_id} (Msg {task.message_id})"
                        }
                        
                        try:
                            # Add timeout protection to prevent stuck workers
                            # If timeout occurs, task will be cancelled and cleaned up properly
                            task_future = asyncio.create_task(self._process_task(task, worker_id))
                            await asyncio.wait_for(
                                task_future,
                                timeout=self.task_timeout_seconds
                            )
                        except (asyncio.TimeoutError, TimeoutError):
                            # Cancel the task and wait for cleanup
                            if not task_future.done():
                                task_future.cancel()
                                try:
                                    await task_future  # Wait for cancellation to complete
                                except asyncio.CancelledError:
                                    pass  # Expected
                            logger.error(
                                f"Worker {worker_id} task TIMED OUT after {self.task_timeout_seconds}s "
                                f"(Chat {task.chat_id}, Msg {task.message_id}). Task cancelled."
                            )
                            raise  # Re-raise to be handled by the outer block
                        except asyncio.CancelledError:
                            if not task_future.done():
                                task_future.cancel()
                                try:
                                    await task_future
                                except asyncio.CancelledError:
                                    pass
                            raise
                        finally:
                            # Clear active task
                            if worker_id in self._active_tasks:
                                del self._active_tasks[worker_id]
                        
                        # Record processing duration for per-chat throttling
                        duration = time.time() - start_time
                        self.record_processing_time(task.chat_id, duration)
                    
                    self.stats['processed'] += 1
                except asyncio.CancelledError:
                    break

                except (asyncio.TimeoutError, TimeoutError):
                    logger.error(f"Worker {worker_id} TIMED OUT processing task (Chat {task.chat_id}). This usually indicates a stuck network request.")
                    self.stats['errors'] += 1
                    
                    # RETRY LOGIC
                    current_retries = task_data.get('_retry_count', 0)
                    if current_retries < self.max_task_retries:
                        logger.warning(f"Retrying task (attempt {current_retries + 1}/{self.max_task_retries})...")
                        task_data['_retry_count'] = current_retries + 1
                        
                        # Re-enqueue
                        if self.redis_available:
                            try:
                                await loop.run_in_executor(
                                    None, 
                                    self.redis_client.rpush, 
                                    self.queue_key, 
                                    json.dumps(task_data)
                                )
                            except:
                                await self.fallback_queue.put(json.dumps(task_data))
                        else:
                            await self.fallback_queue.put(json.dumps(task_data))
                    else:
                        logger.error(f"Task failed after {self.max_task_retries} retries. Moving to DLQ.")
                        await self._move_to_dead_letter(task_data, "Task Timed Out repeatedly")
                    continue

                except Exception as e:
                    # Check for FloodWaitError dynamically to avoid import issues
                    if type(e).__name__ == 'FloodWaitError':
                        if e.seconds > 300:
                            logger.warning(f"Long FloodWait ({e.seconds}s) detected. Requeueing task to BACK of queue.")
                            if self.redis_available:
                                try:
                                    await loop.run_in_executor(None, self.redis_client.rpush, self.queue_key, json.dumps(task_data))
                                except:
                                    await self.fallback_queue.put(json.dumps(task_data))
                            else:
                                await self.fallback_queue.put(json.dumps(task_data))
                            continue
                        else:
                            # Short wait, carry on to general error handling (or ignore if handled?)
                            # Actually, uploader raises it, so we must handle it. 
                            # If short (<300), we treat as normal error (retry logic below will handle it)
                            pass

                    # General Error Handling (includes short FloodWait)
                    logger.error(f"Worker {worker_id} error processing task: {e} (Chat {task.chat_id})")
                    self.stats['errors'] += 1
                    
                    # RETRY LOGIC
                    current_retries = task_data.get('_retry_count', 0)
                    if current_retries < self.max_task_retries:
                        logger.warning(f"Retrying task (attempt {current_retries + 1}/{self.max_task_retries})...")
                        task_data['_retry_count'] = current_retries + 1
                        
                        # Re-enqueue
                        if self.redis_available:
                            try:
                                await loop.run_in_executor(
                                    None, 
                                    self.redis_client.rpush, 
                                    self.queue_key, 
                                    json.dumps(task_data)
                                )
                            except:
                                await self.fallback_queue.put(json.dumps(task_data))
                        else:
                            await self.fallback_queue.put(json.dumps(task_data))
                    else:
                        logger.error(f"Task failed after {self.max_task_retries} retries. Moving to DLQ.")
                        await self._move_to_dead_letter(task_data, str(e))

            except Exception as e:
                consecutive_errors += 1
                delay = min(30, 1.5 ** consecutive_errors)  # Exponential backoff up to 30s
                logger.error(f"Worker {worker_id} loop error (consecutive={consecutive_errors}): {e}")
                logger.info(f"Worker {worker_id} sleeping {delay:.1f}s for panic recovery...")
                await asyncio.sleep(delay)
            else:
                # Reset error count on successful loop iteration (if we get here)
                consecutive_errors = 0
        
        logger.info(f"Worker {worker_id} stopped")

    async def _move_to_dead_letter(self, task_data: dict, error_reason: str):
        """
        Moves a failed task to the dead letter queue for later analysis.
        
        Args:
            task_data: Original task data dict
            error_reason: Description of why the task failed
        """
        try:
            # Add failure metadata
            task_data['_failure_reason'] = error_reason
            task_data['_failed_at'] = datetime.now(timezone.utc).isoformat()
            task_data['_retry_count'] = task_data.get('_retry_count', 0) + 1
            
            # Assign a unique task ID for hash-based DLQ storage
            import uuid as _uuid
            if '_task_id' not in task_data:
                task_data['_task_id'] = str(_uuid.uuid4())
            
            # Remove large content to save space (just keep ID/metadata)
            if 'content_b64' in task_data:
                task_data['_had_content'] = True
                del task_data['content_b64']
            
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                self.redis_client.hset,
                self.dead_letter_key,
                task_data['_task_id'],
                json.dumps(task_data)
            )
            
            # Also log to database for dashboard visibility
            from shared.database import log_processing_error
            await log_processing_error(
                error_type='TaskFailed',
                error_message=error_reason,
                error_context={
                    'chat_id': task_data.get('chat_id'),
                    'message_id': task_data.get('message_id'),
                    'task_type': task_data.get('task_type')
                }
            )
            
            logger.warning(f"Task moved to dead letter queue: {error_reason}")
            
            # Notify Hub about task failure (immediate alert)
            await notify('error', f"Task failed: {error_reason[:80]}", priority=2)
            await increment_stat('errors_count', 1)
        except Exception as e:
            logger.error(f"Failed to move task to dead letter queue: {e}")

    async def _update_heartbeat(self, worker_id: int):
        """Updates the heartbeat timestamp for a worker in Redis."""
        # Add retry logic for temporary connection issues
        for attempt in range(3):
            try:
                # If Redis is unavailable, skip heartbeat (don't crash worker)
                if not self.redis_available and not self._try_reconnect_redis():
                    return
                
                timestamp = int(datetime.now(timezone.utc).timestamp())
                key = f"worker_heartbeat:{worker_id}"
                
                # Run in executor to avoid blocking if Redis is slow
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    self.redis_client.setex,
                    key, 
                    300, 
                    timestamp
                )
                return  # Success
            except Exception as e:
                if attempt == 2:  # Log only on final failure
                    logger.warning(f"Failed to update heartbeat for worker {worker_id}: {e}")
                else:
                    await asyncio.sleep(1)  # Brief wait before retry
    
    def _check_worker_memory(self) -> bool:
        """
        Checks if worker process exceeds memory limit.
        
        Returns:
            True if memory exceeds limit, False otherwise
        """
        try:
            import psutil
            process = psutil.Process()
            mem_mb = process.memory_info().rss / 1024 / 1024
            return mem_mb > self.worker_memory_limit_mb
        except ImportError:
            # psutil not available, skip check
            return False
        except Exception as e:
            logger.debug(f"Memory check failed: {e}")
            return False

    async def _monitor_heartbeats(self):
        """Monitors worker heartbeats, cancels stuck workers, and spawns replacements."""
        from services.collector.account_manager import get_bot_client
        from shared.config import settings
        
        while self._running:
            await asyncio.sleep(60)  # Check every minute
            
            try:
                now = int(datetime.now(timezone.utc).timestamp())
                stuck_workers = []
                
                for i in range(self._current_workers):
                    key = f"worker_heartbeat:{i}"
                    last_beat = self.redis_client.get(key)
                    
                    if last_beat:
                        last_beat = int(last_beat)
                        if now - last_beat > 300:  # 5 minutes
                            stuck_workers.append(i)
                
                if stuck_workers:
                    msg = f"⚠️ **Worker Alert**: Workers {stuck_workers} haven't heartbeat in 5 mins!"
                    logger.warning(msg)
                    
                    # Alert via Bot
                    try:
                        bot = await get_bot_client()
                        from shared.config import get_hub_group_id, resolve_hub_group_id
                        hub_id = get_hub_group_id()
                        if hub_id is None:
                            try:
                                hub_id = await resolve_hub_group_id(bot)
                            except Exception:
                                hub_id = settings.HUB_GROUP_ID
                        if hub_id:
                            await bot.send_message(hub_id, msg)
                    except Exception as e:
                        logger.error(f"Failed to send alert: {e}")

                    # Cancel stuck workers and spawn replacements
                    for worker_id in stuck_workers:
                        task = self._workers.get(worker_id)
                        if task is None:
                            continue

                        # Log active task details before clearing
                        active_info = self._active_tasks.get(worker_id)
                        if active_info:
                            duration = now - active_info.get('start_time', now)
                            logger.warning(
                                f"Cancelling stuck worker {worker_id}: "
                                f"task_type={active_info.get('task_type')}, "
                                f"chat_id={active_info.get('chat_id')}, "
                                f"duration={duration:.1f}s"
                            )

                        # Cancel the stuck task
                        task.cancel()
                        try:
                            await asyncio.shield(task)
                        except (asyncio.CancelledError, Exception):
                            pass

                        # Spawn replacement worker
                        self._workers[worker_id] = asyncio.create_task(self._worker(worker_id))
                        logger.info(f"Spawned replacement for stuck worker {worker_id}")

                        # Clear active task entry
                        self._active_tasks.pop(worker_id, None)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat monitor error: {e}")
                await asyncio.sleep(60)

    async def _monitor_queue_status(self):
        """
        Periodically logs detailed queue status and stuck tasks.
        Provides visibility into what is clogging the queue.
        """
        while self._running:
            try:
                # 1. Get Queue Sizes
                main_len = 0
                dlq_len = 0
                if self.redis_available:
                    try:
                        main_len = self.redis_client.llen(self.queue_key)
                        dlq_len = self.redis_client.hlen(self.dead_letter_key)
                    except:
                        pass
                
                # 2. Check Active Tasks (Stuck?)
                now = time.time()
                active_count = len(self._active_tasks)
                stuck_log = []
                
                for worker_id, info in self._active_tasks.items():
                    duration = now - info['start_time']
                    if duration > 60:  # If taking longer than 60s
                        stuck_log.append(f"  ⚠️ Worker {worker_id}: {duration:.1f}s on {info['description']}")
                
                # 3. Calculate Rate (approx)
                # We could track processed_last_minute if we reset it, 
                # but simplest is just to print current backlog
                
                log_msg = [
                    f"📊 Queue Status: {main_len} pending, {dlq_len} failed.",
                    f"   Active Workers: {active_count}/{self.num_workers}"
                ]
                
                if stuck_log:
                    log_msg.append("   ⚠️ Stuck/Long Running Tasks:")
                    log_msg.extend(stuck_log)
                elif active_count > 0:
                    # Log what they are doing briefly
                    tasks = [f"W{wid}:{info['media_type']}" for wid, info in self._active_tasks.items()]
                    log_msg.append(f"   Working on: {', '.join(tasks)}")
                
                logger.info("\n".join(log_msg))
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Queue monitor error: {e}")
            
            await asyncio.sleep(60)  # Log every minute

    async def _redis_reconnect_loop(self):
        """
        Background coroutine: while _running and not redis_available,
        attempt reconnection every REDIS_RECONNECT_INTERVAL seconds.
        On success, drain fallback_queue into Redis (async-safe) and set
        redis_available = True. Exits when redis_available becomes True or
        _running becomes False.
        """
        interval = getattr(settings, 'REDIS_RECONNECT_INTERVAL', 30)
        max_attempts = getattr(settings, 'REDIS_RECONNECT_MAX_ATTEMPTS', 0)
        attempt = 0

        while self._running and not self.redis_available:
            await asyncio.sleep(interval)

            if not self._running:
                break

            attempt += 1
            if max_attempts > 0 and attempt > max_attempts:
                logger.warning(
                    f"Redis reconnect: giving up after {max_attempts} attempts."
                )
                break

            logger.info(f"Redis reconnect attempt {attempt}...")
            try:
                loop = asyncio.get_running_loop()

                # Ensure we have a client to ping
                if self.redis_client is None:
                    if redis is None:
                        logger.warning("Redis reconnect: redis package not available.")
                        continue
                    self.redis_client = redis.Redis(
                        host=settings.REDIS_HOST,
                        port=settings.REDIS_PORT,
                        db=settings.REDIS_DB,
                        password=settings.REDIS_PASSWORD,
                        decode_responses=False,
                        socket_timeout=5,
                        socket_connect_timeout=5,
                    )

                await loop.run_in_executor(None, self.redis_client.ping)

                # Drain fallback queue into Redis (async-safe)
                count = 0
                while not self.fallback_queue.empty():
                    try:
                        task_json = self.fallback_queue.get_nowait()
                        await loop.run_in_executor(
                            None,
                            self.redis_client.rpush,
                            self.queue_key,
                            task_json,
                        )
                        count += 1
                    except asyncio.QueueEmpty:
                        break
                    except Exception as drain_err:
                        logger.error(f"Redis reconnect: drain error: {drain_err}")
                        break

                self.redis_available = True
                logger.info(
                    f"✅ Redis reconnected (attempt {attempt}). "
                    f"Drained {count} fallback item(s)."
                )
                return  # Exit loop — reconnect succeeded

            except Exception as e:
                logger.warning(f"Redis reconnect attempt {attempt} failed: {e}")

    async def _autoscaler_loop(self) -> None:
        """
        Background coroutine: every AUTOSCALER_POLL_INTERVAL seconds, checks
        queue depth and scales workers up/down within [num_workers, MAX_WORKERS].

        Scale-up: depth > high_watermark sustained for >= SCALE_UP_SUSTAINED_SECONDS
        Scale-down: depth < low_watermark sustained for >= SCALE_DOWN_SUSTAINED_SECONDS
        """
        import time as _time

        poll_interval = getattr(settings, 'AUTOSCALER_POLL_INTERVAL', 15)
        max_workers = getattr(settings, 'MAX_WORKERS', 10)
        scale_up_sustained = getattr(settings, 'SCALE_UP_SUSTAINED_SECONDS', 60)
        scale_down_sustained = getattr(settings, 'SCALE_DOWN_SUSTAINED_SECONDS', 120)

        while self._running:
            try:
                await asyncio.sleep(poll_interval)

                if not self._running:
                    break

                depth = self.get_queue_size()
                now = _time.monotonic()

                # --- Scale-up path ---
                if depth > self.high_watermark and self._current_workers < max_workers:
                    if self._scale_up_since is None:
                        self._scale_up_since = now
                    elif (now - self._scale_up_since) >= scale_up_sustained:
                        new_id = self._current_workers
                        self._workers[new_id] = asyncio.create_task(self._worker(new_id))
                        self._current_workers += 1
                        self._scale_up_since = None  # reset after spawning one
                        logger.info(
                            f"Autoscaler: scaled up to {self._current_workers} workers "
                            f"(queue depth={depth}, high_watermark={self.high_watermark})"
                        )
                    # Reset scale-down timer when above high watermark
                    self._scale_down_since = None
                else:
                    self._scale_up_since = None

                # --- Scale-down path ---
                if depth < self.low_watermark and self._current_workers > self.num_workers:
                    if self._scale_down_since is None:
                        self._scale_down_since = now
                    elif (now - self._scale_down_since) >= scale_down_sustained:
                        # Cancel the highest-numbered extra worker
                        extra_id = self._current_workers - 1
                        task = self._workers.pop(extra_id, None)
                        if task and not task.done():
                            task.cancel()
                        self._current_workers -= 1
                        self._scale_down_since = None
                        logger.info(
                            f"Autoscaler: scaled down to {self._current_workers} workers "
                            f"(queue depth={depth}, low_watermark={self.low_watermark})"
                        )
                    # Reset scale-up timer when below low watermark
                    self._scale_up_since = None
                else:
                    self._scale_down_since = None

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Autoscaler loop error: {e}")

    def _try_reconnect_redis(self):
        """Attempts to reconnect to Redis if connection was lost."""
        if self.redis_available:
            return
            
        logger.info("Attempting to reconnect to Redis...")
        try:
            # Recreate client
            if redis is None:
                return

            self.redis_client = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=settings.REDIS_DB,
                password=settings.REDIS_PASSWORD,
                decode_responses=False
            )
            self.redis_client.ping()
            self.redis_available = True
            logger.info("✅ Redis reconnected! Draining fallback queue...")
            
            # Drain fallback queue to Redis (sync — only called from sync contexts)
            count = 0
            while not self.fallback_queue.empty():
                try:
                    task_json = self.fallback_queue.get_nowait()
                    self.redis_client.rpush(self.queue_key, task_json)
                    count += 1
                except asyncio.QueueEmpty:
                    break
                except Exception as e:
                    logger.error(f"Error draining fallback queue: {e}")
                    break
            
            if count > 0:
                logger.info(f"Drained {count} tasks from in-memory fallback to Redis")
                
        except Exception as e:
            logger.warning(f"Redis reconnection failed: {e}")


    async def _process_task(self, task: ProcessingTask, worker_id: int):
        """Processes a single task through the complete pipeline."""
        
        if task.task_type == TaskType.PROFILE_PHOTO:
            await self._process_profile_photo(task)
        elif task.task_type == TaskType.STORY:
            # Stories use the same media pipeline but could have special handling
            logger.debug(f"Processing story from peer {task.chat_id}")
            await self._process_media(task)
        else:
            await self._process_media(task)

    async def _process_media(self, task: ProcessingTask):
        """
        Complete media processing pipeline:
        1. Validate media (check for corruption)
        2. Extract frames (for video) or use image directly
        3. Detect faces in each frame
        4. Match each face to identity
        5. Upload media to matched topics
        """
        logger.debug(f"Processing {task.media_type} from chat {task.chat_id}")
        
        # Step 0: Validate media before processing
        if not self._validate_media(task.content, task.media_type):
            logger.warning(f"⚠️ Skipping invalid/corrupted {task.media_type} from chat {task.chat_id}")
            self.stats['errors'] += 1
            return
        
        # Watchdog logging
        logger.debug(f"Step 1/4: Extracting frames from {task.media_type}...")
        
        # Step 1: Get frames
        if task.media_type in ('video', 'video_note'):
            is_round = task.media_type == 'video_note'
            frames = await self.video_extractor.extract_frames(task.content, is_round)
        else:
            # Single image - wrap in list
            frames = [task.content]
        
        if not frames:
            logger.warning(f"No frames extracted from {task.media_type}")
            return
        
        # Step 2 & 3: Detect faces and match identities
        logger.debug(f"Step 2/4: Detecting faces in {len(frames)} frames...")
        matched_topics = set()
        total_faces_detected = 0
        
        for frame_idx, frame in enumerate(frames):
            faces = await self.face_processor.process_image(frame)
            total_faces_detected += len(faces)
            
            if faces:
                logger.info(f"🔍 Frame {frame_idx}: detected {len(faces)} face(s)")
            
            for face in faces:
                self.stats['faces_found'] += 1
                
                topic_id, is_new = await self.identity_matcher.find_or_create_identity(
                    embedding=face['embedding'],
                    quality_score=face['quality'],
                    source_chat_id=task.chat_id,
                    source_message_id=task.message_id,
                    frame_index=frame_idx
                )
                
                if topic_id:
                    logger.debug(f"  → Face matched to topic {topic_id} (new={is_new}, quality={face['quality']:.2f})")
                    matched_topics.add(topic_id)
                    if is_new:
                        self.stats['new_identities'] += 1
                        # Notify Hub about new identity (high priority)
                        await notify('face', f"🆕 New identity **Person {topic_id}** created!", priority=2)
                else:
                    logger.debug(f"  → Face skipped (quality={face['quality']:.2f}, below threshold?)")
        
        logger.info(f"👥 Total: {total_faces_detected} faces detected → {len(matched_topics)} unique topic(s)")
        # Update stats for batched Hub notification
        if total_faces_detected > 0:
            await increment_stat('faces_detected', total_faces_detected)
        if matched_topics:
            await increment_stat('uploads_completed', len(matched_topics))
        
        # Step 4: Upload media to all matched topics
        if matched_topics and self.media_uploader:
            logger.debug(f"Step 3/4: Uploading to {len(matched_topics)} topic(s)...")
            logger.info(f"📤 Uploading to {len(matched_topics)} topic(s) for msg {task.message_id}")
            task.content.seek(0)
            
            upload_failures = 0
            
            for topic_id in matched_topics:
                result = await self.media_uploader.upload_to_topic(
                    db_topic_id=topic_id,
                    media_buffer=task.content,
                    source_message_id=task.message_id,
                    source_chat_id=task.chat_id,
                    media_type=task.media_type
                )
                if result == 0:
                    logger.warning(f"⚠️ Upload to topic {topic_id} failed!")
                    upload_failures += 1
                elif result == -1:
                    logger.debug(f"⏭️ Media already in topic {topic_id}, skipping.")
                
                task.content.seek(0)  # Reset for next upload

            # CRITICAL: If any upload failed, fail the task so it goes to DLQ/Retry.
            # MediaUploader handles deduplication, so successful uploads won't be duplicated on retry.
            if upload_failures > 0:
                raise Exception(f"Upload failed for {upload_failures}/{len(matched_topics)} topics")
                
        elif not matched_topics:
            logger.debug(f"No faces matched for {task.media_type} msg {task.message_id}")
        
        # Step 5: Mark file as processed for deduplication
        logger.debug("Step 4/4: Marking file as processed...")
        if task.file_unique_id:
            await self._mark_file_processed(
                file_unique_id=task.file_unique_id,
                media_type=task.media_type,
                chat_id=task.chat_id,
                message_id=task.message_id,
                faces_found=len([t for t in matched_topics]),
                topics_matched=list(matched_topics)
            )
        
        logger.info(f"Processed {task.media_type}: {len(frames)} frames, matched to {len(matched_topics)} topics")
    
    async def _process_profile_photo(self, task: ProcessingTask):
        """Processes a user's profile photo."""
        logger.debug(f"Processing profile photo for user {task.user_id}")
        
        faces = await self.face_processor.process_image(task.content)
        
        for face in faces:
            self.stats['faces_found'] += 1
            
            topic_id, is_new = await self.identity_matcher.find_or_create_identity(
                embedding=face['embedding'],
                quality_score=face['quality'],
                source_chat_id=0,  # Profile photos don't have chat context
                source_message_id=task.user_id,  # Use user_id as identifier
                frame_index=0
            )
            
            if is_new:
                self.stats['new_identities'] += 1
                
                # Optionally upload profile photo to the new topic
                if self.media_uploader and topic_id:
                    task.content.seek(0)
                    await self.media_uploader.upload_to_topic(
                        db_topic_id=topic_id,
                        media_buffer=task.content,
                        source_message_id=task.user_id,
                        source_chat_id=0,
                        caption=f"👤 Profile photo (User ID: {task.user_id})"
                    )
        
        logger.debug(f"Profile photo processed: {len(faces)} faces found")
    
    async def _mark_file_processed(
        self,
        file_unique_id: str,
        media_type: str,
        chat_id: int,
        message_id: int,
        faces_found: int,
        topics_matched: list
    ):
        """
        Records that a media file has been processed.
        Used for deduplication of forwarded/duplicate content.
        """
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        INSERT INTO face_recognition.processed_media
                            (file_unique_id, media_type, first_seen_chat_id,
                             first_seen_message_id, faces_found, topics_matched)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (file_unique_id) DO NOTHING
                    """, (
                        file_unique_id,
                        media_type,
                        chat_id,
                        message_id,
                        faces_found,
                        topics_matched if topics_matched else None
                    ))
        except Exception as e:
            logger.warning(f"Failed to mark file as processed: {e}")
    
    def _validate_media(self, content: io.BytesIO, media_type: str) -> bool:
        """
        Validates that media content is not empty or corrupted.
        
        Args:
            content: BytesIO buffer with media data
            media_type: 'photo', 'video', or 'video_note'
            
        Returns:
            True if valid, False if corrupted/empty
        """
        try:
            content.seek(0)
            data = content.read(1024)  # Read first 1KB for header check
            content.seek(0)  # Reset for later use
            
            if not data or len(data) < 10:
                logger.warning(f"Empty or too small {media_type} content ({len(data)} bytes)")
                return False
            
            # Check for common image magic bytes
            if media_type == 'photo':
                # JPEG: FF D8 FF
                # PNG: 89 50 4E 47
                # GIF: 47 49 46 38
                # WebP: 52 49 46 46 ... 57 45 42 50
                jpeg_magic = data[:3] == b'\xff\xd8\xff'
                png_magic = data[:4] == b'\x89PNG'
                gif_magic = data[:4] == b'GIF8'
                webp_magic = data[:4] == b'RIFF' and data[8:12] == b'WEBP'
                
                if not (jpeg_magic or png_magic or gif_magic or webp_magic):
                    logger.warning(f"Invalid image format (magic bytes: {data[:4].hex()})")
                    return False
                    
            elif media_type in ('video', 'video_note'):
                # MP4/MOV: 00 00 00 XX 66 74 79 70 (ftyp at offset 4)
                # WebM/MKV: 1A 45 DF A3
                # AVI: 52 49 46 46 ... 41 56 49
                mp4_ftyp = b'ftyp' in data[:32]
                webm_magic = data[:4] == b'\x1aE\xdf\xa3'
                avi_magic = data[:4] == b'RIFF' and data[8:11] == b'AVI'
                
                if not (mp4_ftyp or webm_magic or avi_magic):
                    logger.warning(f"Invalid video format (magic bytes: {data[:8].hex()})")
                    return False
            
            return True
            
        except Exception as e:
            logger.error(f"Media validation error: {e}")
            return False
    
    def get_stats(self) -> Dict[str, int]:
        """Returns processing statistics."""
        return self.stats.copy()
    
    def get_queue_size(self) -> int:
        """Returns current queue depth."""
        if self.redis_available:
            try:
                return int(self.redis_client.llen(self.queue_key))
            except Exception:
                return self.fallback_queue.qsize()
        return self.fallback_queue.qsize()

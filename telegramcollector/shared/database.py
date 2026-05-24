
import logging
import contextlib
import asyncio
import psycopg
from psycopg_pool import AsyncConnectionPool
from shared.config import settings
from shared.resilience import retry_with_jitter, get_circuit_breaker, CircuitOpenError

logger = logging.getLogger(__name__)

class DatabaseManager:
    """
    Database connection pool manager with health monitoring and circuit breaker.
    """
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DatabaseManager, cls).__new__(cls)
            cls._instance.pool = None
            cls._instance._health_task = None
            cls._instance._circuit_breaker = None
        return cls._instance
    
    async def initialize(self):
        """Initializes the async connection pool if not already initialized."""
        if self.pool is None:
            await self._initialize_pool()
            # Start health monitor
            self._health_task = asyncio.create_task(self._pool_health_monitor())
            # Initialize circuit breaker
            self._circuit_breaker = get_circuit_breaker('database')
            # Validate health before considering circuit closed
            if self._circuit_breaker:
                try:
                    async with self.pool.connection() as conn:
                        await asyncio.wait_for(conn.execute("SELECT 1"), timeout=5.0)
                    # Only reset if DB is actually healthy
                    self._circuit_breaker.reset()
                    logger.info("Database health validated, circuit breaker reset")
                except Exception as e:
                    logger.warning(f"Database not healthy on startup, circuit breaker NOT reset: {e}")
    
    async def _initialize_pool(self):
        """Creates an async connection pool with TCP keepalive."""
        try:
            conn_str = (
                f"host={settings.DB_HOST} "
                f"port={settings.DB_PORT} "
                f"dbname={settings.DB_NAME} "
                f"user={settings.DB_USER} "
                f"password={settings.DB_PASSWORD} "
                # TCP keepalive to prevent Docker NAT from dropping idle connections
                f"keepalives=1 "
                f"keepalives_idle=30 "
                f"keepalives_interval=10 "
                f"keepalives_count=3"
            )
            
            self.pool = AsyncConnectionPool(
                conninfo=conn_str,
                min_size=2,
                max_size=10,
                max_lifetime=600,   # Recycle connections every 10 minutes
                max_idle=300,       # Close idle connections after 5 minutes
                reconnect_timeout=30,
                open=False,
                kwargs={'autocommit': True}
            )
            await self.pool.open()
            logger.info("Async database connection pool initialized (keepalive enabled, max_lifetime=600s)")
        except Exception as e:
            logger.error(f"Failed to initialize database pool: {e}")
            raise
    
    async def _pool_health_monitor(self):
        """
        Periodically monitors pool health and recovers stale connections.
        Runs every 60 seconds to prevent stale connections in Docker networking.
        """
        while True:
            try:
                await asyncio.sleep(60)  # Check every 60 seconds
                
                if self.pool is None:
                    continue
                
                # Check pool statistics
                stats = self.pool.get_stats()
                pool_size = stats.get('pool_size', 0)
                pool_available = stats.get('pool_available', 0)
                requests_waiting = stats.get('requests_waiting', 0)
                
                logger.debug(
                    f"Pool health: size={pool_size}, available={pool_available}, "
                    f"waiting={requests_waiting}"
                )
                
                # If too many waiting requests, try to recover
                if requests_waiting > 5:
                    logger.warning(f"High connection wait queue ({requests_waiting}). Attempting pool recovery...")
                    await self._recover_pool()
                
                # Test a connection
                try:
                    async with self.pool.connection() as conn:
                        await asyncio.wait_for(conn.execute("SELECT 1"), timeout=5.0)
                    logger.debug("Pool health check passed")
                except Exception as e:
                    logger.warning(f"Pool health check failed: {e}. Attempting recovery...")
                    await self._recover_pool()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                # Break loop if event loop is closed to prevent infinite log spam
                if isinstance(e, RuntimeError) and "event loop" in str(e).lower():
                    logger.error("Pool health monitor stopping: event loop error")
                    break
                logger.error(f"Pool health monitor error: {e}")
                # Wait before retrying to verify it's not a tight loop
                try:
                    await asyncio.sleep(5)
                except Exception:
                    break
    
    async def _recover_pool(self):
        """Attempts to recover the connection pool by resizing or recreating."""
        try:
            if self.pool:
                # Try soft recovery first: check() reconnects stale connections
                logger.info("Attempting pool recovery...")
                await self.pool.check()
                logger.info("Pool recovery completed")
                return  # Soft recovery succeeded — pool reference unchanged
        except Exception as e:
            logger.error(f"Pool recovery failed: {e}")
            # Hard recovery: close the broken pool and reinitialise.
            # Use finally to guarantee self.pool = None even if close() raises.
            try:
                if self.pool:
                    await self.pool.close()
            except Exception as close_err:
                logger.error(f"Pool close during recovery failed (ignored): {close_err}")
            finally:
                self.pool = None  # Always clear the reference

            try:
                await self._initialize_pool()
                logger.info("Pool reinitialized after recovery failure")
            except Exception as reinit_err:
                logger.error(f"Pool reinitialization failed: {reinit_err}")

    async def close(self):
        """Closes the connection pool and stops health monitor."""
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
        
        if self.pool:
            try:
                await self.pool.close()
            except asyncio.CancelledError:
                logger.info("Database pool close was cancelled (expected during shutdown)")
            except Exception as e:
                logger.error(f"Error closing DB pool: {e}")
            finally:
                self.pool = None
            logger.info("Database pool closed")

# Global instance
db_manager = DatabaseManager()

@contextlib.asynccontextmanager
async def get_db_connection(max_retries: int = 3, retry_delay: float = 1.0):
    """
    Async context manager for getting a database connection with circuit breaker protection.
    
    Args:
        max_retries: Maximum number of connection attempts
        retry_delay: Base delay between retries (exponential backoff applied)
    
    Usage:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(...)
                result = await cur.fetchone()
    
    Raises:
        CircuitOpenError: If database circuit breaker is open
        psycopg.OperationalError: If connection fails after retries
    """
    if db_manager.pool is None:
        await db_manager.initialize()
    
    # Check circuit breaker
    circuit = db_manager._circuit_breaker
    if circuit:
        from shared.resilience import CircuitState
        cs = circuit.state  # property auto-transitions OPEN→HALF_OPEN after timeout
        if cs == CircuitState.OPEN:
            logger.warning("Database circuit breaker is OPEN - failing fast")
            raise CircuitOpenError("Database circuit breaker is open - too many failures")
        if cs == CircuitState.HALF_OPEN and circuit._stats.state != CircuitState.HALF_OPEN:
            # Commit the timeout-based transition so _on_success() can close the circuit
            circuit._stats.state = CircuitState.HALF_OPEN
            circuit._half_open_calls = 0

    last_error = None
    for attempt in range(max_retries):
        try:
            async with db_manager.pool.connection() as conn:
                # Record success with circuit breaker
                if circuit:
                    await circuit._on_success()
                yield conn
                return
        except psycopg.OperationalError as e:
            last_error = e
            # Only connection failures should affect the circuit breaker
            if circuit:
                await circuit._on_failure()

            if attempt < max_retries - 1:
                delay = retry_delay * (2 ** attempt)
                logger.warning(f"DB connection failed (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {delay}s...")
                await asyncio.sleep(delay)
            else:
                logger.error(f"DB connection failed after {max_retries} attempts: {e}")
                raise
        except Exception as e:
            # Data errors (UniqueViolation, etc.) must NOT trip the circuit breaker —
            # they indicate schema/logic issues, not a lost DB connection.
            logger.error(f"DB error (not retrying): {e}")
            raise
    
    if last_error:
        raise last_error

async def init_db():
    """Initializes the database schema."""
    try:
        with open('init-db.sql', 'r') as f:
            schema = f.read()
    except FileNotFoundError:
        logger.error(
            "CRITICAL: init-db.sql not found. "
            "Ensure the file exists in the working directory. "
            "Current directory: " + __import__('os').getcwd()
        )
        raise
    except Exception as e:
        logger.error(f"Failed to read init-db.sql: {e}")
        raise
    
    async with get_db_connection() as conn:
        try:
            
            # Execute schema
            await conn.execute(schema)
            
            # Run migration for historical profile photos table to ensure it exists
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_profile_photos (
                    user_id BIGINT NOT NULL,
                    photo_id BIGINT NOT NULL,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, photo_id)
                );
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_processed_profile_photos_user ON processed_profile_photos(user_id);")
            await conn.execute("ALTER TABLE collector.scan_checkpoints ADD COLUMN IF NOT EXISTS last_seen_message_id BIGINT DEFAULT 0;")
            await conn.execute("ALTER TABLE collector.scan_checkpoints ADD COLUMN IF NOT EXISTS chat_type VARCHAR(20);")
            await conn.execute("ALTER TABLE public.health_checks ADD COLUMN IF NOT EXISTS clock_drift_sec REAL;")
            await conn.execute("ALTER TABLE face_recognition.processed_media ADD COLUMN IF NOT EXISTS first_seen_chat_id BIGINT;")
            await conn.execute("ALTER TABLE face_recognition.processed_media ADD COLUMN IF NOT EXISTS first_seen_message_id BIGINT;")
            await conn.execute("ALTER TABLE face_recognition.telegram_topics ALTER COLUMN topic_id DROP NOT NULL;")
            await conn.execute("UPDATE face_recognition.telegram_topics SET topic_id = NULL WHERE topic_id = 0;")
            
            logger.info("Database schema initialized.")
        except Exception as e:
            logger.error(f"Database initialization failed: {e}")

async def check_db_health():
    """Checks if database is responsive."""
    try:
        async with get_db_connection() as conn:
            await conn.execute("SELECT 1")
        return True
    except Exception:
        return False

async def log_processing_error(error_type: str, error_message: str, error_context: dict = None):
    """
    Logs a processing error to the database for dashboard display.
    
    Args:
        error_type: Category of error (e.g., 'FaceDetection', 'MediaDownload')
        error_message: Detailed error description
        error_context: Optional dictionary with extra context (chat_id, message_id, etc.)
    """
    try:
        import json
        context_json = json.dumps(error_context) if error_context else None
        
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO collector.processing_errors
                        (error_type, error_message, error_context)
                    VALUES (%s, %s, %s)
                """, (error_type, str(error_message), context_json))
    except Exception as e:
        # Fallback to logger if DB logging fails
        logger.error(f"Failed to log error to DB: {e}")


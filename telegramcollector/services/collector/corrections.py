"""
Correction Handlers - Implements merge, split, and rename operations.

Used by the dashboard to manually correct clustering mistakes.
"""
import logging
from shared.database import get_db_connection

logger = logging.getLogger(__name__)

class CorrectionHandler:
    """
    Handles manual correction operations for identities.
    """
    
    def __init__(self, topic_manager=None):
        self.topic_manager = topic_manager
    
    async def merge_identities(self, source_topic_id: int, target_topic_id: int) -> bool:
        """
        Merges two identities by moving all embeddings from source to target.
        
        Args:
            source_topic_id: The topic to merge FROM (will be deleted)
            target_topic_id: The topic to merge INTO (will remain)
            
        Returns:
            True if successful
        """
        logger.info(f"Merging topic {source_topic_id} into {target_topic_id}")
        
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cursor:
                    # Move all embeddings to target
                    await cursor.execute("""
                        UPDATE face_recognition.face_embeddings
                        SET topic_id = %s
                        WHERE topic_id = %s
                    """, (target_topic_id, source_topic_id))
                    
                    # Move uploaded media references
                    await cursor.execute("""
                        UPDATE face_recognition.uploaded_media
                        SET topic_id = %s
                        WHERE topic_id = %s
                    """, (target_topic_id, source_topic_id))
                    
                    # Delete the source topic record
                    await cursor.execute("""
                        DELETE FROM face_recognition.telegram_topics WHERE id = %s
                    """, (source_topic_id,))
                    
                    # Update target counts (Ported from dashboard.py for consistency)
                    await cursor.execute("""
                        UPDATE face_recognition.telegram_topics SET
                            face_count = (SELECT COUNT(*) FROM face_recognition.face_embeddings WHERE topic_id = %s),
                            message_count = (SELECT COUNT(*) FROM face_recognition.uploaded_media WHERE topic_id = %s),
                            updated_at = NOW()
                        WHERE id = %s
                    """, (target_topic_id, target_topic_id, target_topic_id))

                
            logger.info(f"Merge complete: {source_topic_id} -> {target_topic_id}")
            return True
            
        except Exception as e:
            logger.error(f"Merge failed: {e}")
            return False
    
    async def split_embedding(self, embedding_id: int, new_label: str = "Split Identity") -> int:
        """
        Splits a single embedding into a new identity.
        
        Args:
            embedding_id: The embedding to split out
            new_label: Label for the new identity
            
        Returns:
            The new topic ID, or 0 on failure
        """
        logger.info(f"Splitting embedding {embedding_id} into new identity: {new_label}")
        
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cursor:
                    # Create new topic
                    await cursor.execute("""
                        INSERT INTO face_recognition.telegram_topics (topic_id, label)
                        VALUES (0, %s)
                        RETURNING id
                    """, (new_label,))
                    
                    result = await cursor.fetchone()
                    new_topic_id = result[0]
                    
                    # Move the embedding
                    await cursor.execute("""
                        UPDATE face_embeddings 
                        SET topic_id = %s 
                        WHERE id = %s
                    """, (new_topic_id, embedding_id))
                
            logger.info(f"Split complete. New topic ID: {new_topic_id}")
            return new_topic_id
            
        except Exception as e:
            logger.error(f"Split failed: {e}")
            return 0
    
    async def rename_identity(self, topic_id: int, new_label: str) -> bool:
        """
        Renames an identity.
        
        Args:
            topic_id: The topic to rename
            new_label: The new label
            
        Returns:
            True if successful
        """
        logger.info(f"Renaming topic {topic_id} to: {new_label}")
        
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("""
                        UPDATE face_recognition.telegram_topics
                        SET label = %s
                        WHERE id = %s
                    """, (new_label, topic_id))
            
            # Rename the actual Telegram forum topic if topic_manager is available
            if self.topic_manager:
                await self.topic_manager.rename_topic(topic_id, new_label)
            
            return True
            
        except Exception as e:
            logger.error(f"Rename failed: {e}")
            return False

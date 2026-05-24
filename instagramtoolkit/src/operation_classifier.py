"""
Operation classification system for Instagram operations.

This module defines the data models for classifying operations by their access
requirements and rate limit sensitivity, and provides the OperationClassifier
class for querying operation metadata.
"""

import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class OperationType(Enum):
    """Classification of operations by access requirements."""
    
    PUBLIC = "public"  # Any account can perform
    FOLLOWING_REQUIRED = "following_required"  # Must follow target
    MUTUAL_FOLLOWING = "mutual_following"  # Must be mutually following


@dataclass
class OperationMetadata:
    """Metadata for an Instagram operation.
    
    Attributes:
        name: Operation name (e.g., "download_stories")
        operation_type: Access requirement classification
        rate_limit_weight: Rate limit sensitivity (1-10 scale)
        description: Human-readable description
    """
    
    name: str
    operation_type: OperationType
    rate_limit_weight: int
    description: str
    
    def __post_init__(self):
        """Validate rate_limit_weight is in valid range."""
        if not isinstance(self.rate_limit_weight, int):
            raise ValueError(
                f"rate_limit_weight must be an integer, got {type(self.rate_limit_weight).__name__}"
            )
        
        if not (1 <= self.rate_limit_weight <= 10):
            raise ValueError(
                f"rate_limit_weight must be between 1 and 10, got {self.rate_limit_weight}"
            )
        
        if not isinstance(self.operation_type, OperationType):
            raise ValueError(
                f"operation_type must be an OperationType enum, got {type(self.operation_type).__name__}"
            )


class OperationClassifier:
    """Classifies Instagram operations by their access requirements.

    Loads the operation registry from config.py on initialization and
    validates all entries.  Unknown operations default to PUBLIC for safety.

    Requirements: 2.1, 2.2, 2.3, 2.5, 2.6, 12.1, 12.2, 12.4, 12.5
    """

    def __init__(self):
        """Initialize the classifier and validate the operation registry."""
        from config import OPERATION_REGISTRY  # imported here to avoid circular imports

        self._registry: dict[str, OperationMetadata] = {}
        self._load_and_validate_registry(OPERATION_REGISTRY)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_and_validate_registry(self, raw_registry: dict) -> None:
        """Parse and validate the raw registry dict from config.

        Raises ValueError for any entry that fails validation so that
        misconfiguration is caught at startup (Requirement 12.2).
        """
        valid_type_names = {t.name for t in OperationType}

        for op_name, entry in raw_registry.items():
            # Validate required fields (Requirement 12.5)
            for field in ("operation_type", "rate_limit_weight", "description"):
                if field not in entry:
                    raise ValueError(
                        f"Operation '{op_name}' is missing required field '{field}'"
                    )

            # Validate operation_type string (Requirement 12.4)
            op_type_str = entry["operation_type"]
            if op_type_str not in valid_type_names:
                raise ValueError(
                    f"Operation '{op_name}' has invalid operation_type '{op_type_str}'. "
                    f"Must be one of: {sorted(valid_type_names)}"
                )

            op_type = OperationType[op_type_str]

            # OperationMetadata.__post_init__ validates rate_limit_weight (Requirement 12.3)
            metadata = OperationMetadata(
                name=op_name,
                operation_type=op_type,
                rate_limit_weight=entry["rate_limit_weight"],
                description=entry["description"],
            )

            self._registry[op_name] = metadata

        logger.debug(
            "OperationClassifier loaded %d operations from registry",
            len(self._registry),
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def classify(self, operation_name: str) -> OperationType:
        """Return the OperationType for *operation_name*.

        Returns OperationType.PUBLIC as a safe default for unknown operations
        (Requirement 2.3).  The result is deterministic for the same input.
        """
        metadata = self._registry.get(operation_name)
        if metadata is None:
            logger.debug(
                "classify(): unknown operation '%s', defaulting to PUBLIC",
                operation_name,
            )
            return OperationType.PUBLIC
        return metadata.operation_type

    def requires_following(self, operation_name: str) -> bool:
        """Return True if *operation_name* requires a following relationship."""
        op_type = self.classify(operation_name)
        return op_type in (OperationType.FOLLOWING_REQUIRED, OperationType.MUTUAL_FOLLOWING)

    def is_public_operation(self, operation_name: str) -> bool:
        """Return True if *operation_name* can be performed by any account."""
        return self.classify(operation_name) == OperationType.PUBLIC

    def get_operation_metadata(self, operation_name: str) -> OperationMetadata:
        """Return full metadata for *operation_name*.

        For unknown operations a synthetic PUBLIC metadata entry is returned
        so callers always receive a valid OperationMetadata object.
        """
        metadata = self._registry.get(operation_name)
        if metadata is None:
            logger.debug(
                "get_operation_metadata(): unknown operation '%s', returning synthetic PUBLIC metadata",
                operation_name,
            )
            return OperationMetadata(
                name=operation_name,
                operation_type=OperationType.PUBLIC,
                rate_limit_weight=1,
                description="Unknown operation (defaulting to PUBLIC)",
            )
        return metadata

    def get_all_operations(self) -> dict[str, OperationMetadata]:
        """Return a copy of the full operation registry."""
        return dict(self._registry)



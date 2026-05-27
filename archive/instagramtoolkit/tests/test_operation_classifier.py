"""
Unit tests for OperationClassifier.

Tests classification of all registered operations, unknown operation handling,
operation metadata retrieval, and registry validation on startup.

Requirements: 2.2, 2.3, 2.6, 12.2
"""

import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from src.operation_classifier import (
    OperationClassifier,
    OperationType,
    OperationMetadata,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def classifier():
    """Return a fresh OperationClassifier instance."""
    return OperationClassifier()


# ---------------------------------------------------------------------------
# 1. Classification of all registered operations (Requirement 2.2)
# ---------------------------------------------------------------------------

class TestRegisteredOperationClassification:
    """Test that every operation in the registry is classified correctly."""

    def test_download_profile_pic_is_public(self, classifier):
        """download_profile_pic should be classified as PUBLIC."""
        assert classifier.classify("download_profile_pic") == OperationType.PUBLIC

    def test_get_basic_info_is_public(self, classifier):
        """get_basic_info should be classified as PUBLIC."""
        assert classifier.classify("get_basic_info") == OperationType.PUBLIC

    def test_get_followers_is_public(self, classifier):
        """get_followers should be classified as PUBLIC."""
        assert classifier.classify("get_followers") == OperationType.PUBLIC

    def test_get_following_is_public(self, classifier):
        """get_following should be classified as PUBLIC."""
        assert classifier.classify("get_following") == OperationType.PUBLIC

    def test_download_stories_requires_following(self, classifier):
        """download_stories should be classified as FOLLOWING_REQUIRED."""
        assert classifier.classify("download_stories") == OperationType.FOLLOWING_REQUIRED

    def test_download_highlights_requires_following(self, classifier):
        """download_highlights should be classified as FOLLOWING_REQUIRED."""
        assert classifier.classify("download_highlights") == OperationType.FOLLOWING_REQUIRED

    def test_download_media_requires_following(self, classifier):
        """download_media should be classified as FOLLOWING_REQUIRED."""
        assert classifier.classify("download_media") == OperationType.FOLLOWING_REQUIRED

    def test_all_registered_operations_return_operation_type(self, classifier):
        """Every registered operation must return a valid OperationType."""
        for op_name in classifier.get_all_operations():
            result = classifier.classify(op_name)
            assert isinstance(result, OperationType), (
                f"classify('{op_name}') returned {type(result).__name__}, expected OperationType"
            )

    def test_public_operations_is_public_helper(self, classifier):
        """is_public_operation() returns True for all PUBLIC operations."""
        public_ops = [
            name for name, meta in classifier.get_all_operations().items()
            if meta.operation_type == OperationType.PUBLIC
        ]
        for op in public_ops:
            assert classifier.is_public_operation(op) is True, (
                f"is_public_operation('{op}') should be True"
            )

    def test_following_required_operations_requires_following_helper(self, classifier):
        """requires_following() returns True for FOLLOWING_REQUIRED operations."""
        following_ops = [
            name for name, meta in classifier.get_all_operations().items()
            if meta.operation_type == OperationType.FOLLOWING_REQUIRED
        ]
        for op in following_ops:
            assert classifier.requires_following(op) is True, (
                f"requires_following('{op}') should be True"
            )

    def test_public_operations_do_not_require_following(self, classifier):
        """requires_following() returns False for PUBLIC operations."""
        public_ops = [
            name for name, meta in classifier.get_all_operations().items()
            if meta.operation_type == OperationType.PUBLIC
        ]
        for op in public_ops:
            assert classifier.requires_following(op) is False, (
                f"requires_following('{op}') should be False for PUBLIC operation"
            )


# ---------------------------------------------------------------------------
# 2. Unknown operation handling (Requirement 2.3)
# ---------------------------------------------------------------------------

class TestUnknownOperationHandling:
    """Unknown operations must default to PUBLIC for safety."""

    def test_unknown_operation_classify_returns_public(self, classifier):
        """classify() returns PUBLIC for an unregistered operation name."""
        assert classifier.classify("nonexistent_operation") == OperationType.PUBLIC

    def test_unknown_operation_is_public_operation(self, classifier):
        """is_public_operation() returns True for an unregistered operation."""
        assert classifier.is_public_operation("totally_unknown_op") is True

    def test_unknown_operation_does_not_require_following(self, classifier):
        """requires_following() returns False for an unregistered operation."""
        assert classifier.requires_following("totally_unknown_op") is False

    def test_empty_string_operation_defaults_to_public(self, classifier):
        """An empty string operation name defaults to PUBLIC."""
        assert classifier.classify("") == OperationType.PUBLIC

    def test_numeric_string_operation_defaults_to_public(self, classifier):
        """A numeric string operation name defaults to PUBLIC."""
        assert classifier.classify("12345") == OperationType.PUBLIC

    def test_unknown_operation_metadata_has_public_type(self, classifier):
        """get_operation_metadata() for unknown op returns PUBLIC metadata."""
        meta = classifier.get_operation_metadata("unknown_op_xyz")
        assert meta.operation_type == OperationType.PUBLIC

    def test_unknown_operation_metadata_has_valid_weight(self, classifier):
        """get_operation_metadata() for unknown op returns valid rate_limit_weight."""
        meta = classifier.get_operation_metadata("unknown_op_xyz")
        assert 1 <= meta.rate_limit_weight <= 10

    def test_unknown_operation_metadata_has_description(self, classifier):
        """get_operation_metadata() for unknown op returns a non-empty description."""
        meta = classifier.get_operation_metadata("unknown_op_xyz")
        assert isinstance(meta.description, str) and meta.description

    def test_unknown_operation_metadata_name_matches_input(self, classifier):
        """get_operation_metadata() for unknown op sets name to the input string."""
        op_name = "some_unknown_operation"
        meta = classifier.get_operation_metadata(op_name)
        assert meta.name == op_name


# ---------------------------------------------------------------------------
# 3. Operation metadata retrieval (Requirement 2.6)
# ---------------------------------------------------------------------------

class TestOperationMetadataRetrieval:
    """Test that get_operation_metadata() returns correct, complete metadata."""

    def test_metadata_for_download_stories(self, classifier):
        """download_stories metadata has correct type and weight."""
        meta = classifier.get_operation_metadata("download_stories")
        assert isinstance(meta, OperationMetadata)
        assert meta.name == "download_stories"
        assert meta.operation_type == OperationType.FOLLOWING_REQUIRED
        assert 1 <= meta.rate_limit_weight <= 10
        assert isinstance(meta.description, str) and meta.description

    def test_metadata_for_download_profile_pic(self, classifier):
        """download_profile_pic metadata has correct type and weight."""
        meta = classifier.get_operation_metadata("download_profile_pic")
        assert meta.name == "download_profile_pic"
        assert meta.operation_type == OperationType.PUBLIC
        assert 1 <= meta.rate_limit_weight <= 10

    def test_metadata_for_get_basic_info(self, classifier):
        """get_basic_info metadata has correct type and weight."""
        meta = classifier.get_operation_metadata("get_basic_info")
        assert meta.name == "get_basic_info"
        assert meta.operation_type == OperationType.PUBLIC

    def test_metadata_returns_operation_metadata_instance(self, classifier):
        """get_operation_metadata() always returns an OperationMetadata instance."""
        for op_name in classifier.get_all_operations():
            meta = classifier.get_operation_metadata(op_name)
            assert isinstance(meta, OperationMetadata), (
                f"Expected OperationMetadata for '{op_name}', got {type(meta).__name__}"
            )

    def test_metadata_name_matches_registry_key(self, classifier):
        """metadata.name matches the registry key for all registered operations."""
        for op_name, meta in classifier.get_all_operations().items():
            assert meta.name == op_name, (
                f"Registry key '{op_name}' does not match metadata.name '{meta.name}'"
            )

    def test_metadata_description_is_non_empty_string(self, classifier):
        """All registered operations have a non-empty description."""
        for op_name, meta in classifier.get_all_operations().items():
            assert isinstance(meta.description, str) and meta.description, (
                f"Operation '{op_name}' has empty or missing description"
            )

    def test_get_all_operations_returns_copy(self, classifier):
        """get_all_operations() returns a copy; mutating it doesn't affect the registry."""
        ops1 = classifier.get_all_operations()
        ops1["injected_op"] = None
        ops2 = classifier.get_all_operations()
        assert "injected_op" not in ops2

    def test_metadata_rate_limit_weight_is_integer(self, classifier):
        """rate_limit_weight is an integer for all registered operations."""
        for op_name, meta in classifier.get_all_operations().items():
            assert isinstance(meta.rate_limit_weight, int), (
                f"Operation '{op_name}' rate_limit_weight is not an int"
            )

    def test_metadata_rate_limit_weight_in_valid_range(self, classifier):
        """rate_limit_weight is between 1 and 10 for all registered operations."""
        for op_name, meta in classifier.get_all_operations().items():
            assert 1 <= meta.rate_limit_weight <= 10, (
                f"Operation '{op_name}' rate_limit_weight={meta.rate_limit_weight} out of range"
            )

    def test_high_sensitivity_operations_have_higher_weight(self, classifier):
        """Stories/highlights have higher rate_limit_weight than basic info."""
        stories_weight = classifier.get_operation_metadata("download_stories").rate_limit_weight
        basic_weight = classifier.get_operation_metadata("get_basic_info").rate_limit_weight
        assert stories_weight > basic_weight


# ---------------------------------------------------------------------------
# 4. Registry validation on startup (Requirement 12.2)
# ---------------------------------------------------------------------------

class TestRegistryValidationOnStartup:
    """OperationClassifier validates the registry when initialized."""

    def test_missing_operation_type_field_raises(self, monkeypatch):
        """Registry entry missing 'operation_type' raises ValueError on init."""
        bad_registry = {
            "bad_op": {
                "rate_limit_weight": 5,
                "description": "Missing operation_type",
            }
        }
        monkeypatch.setattr("config.OPERATION_REGISTRY", bad_registry)
        with pytest.raises(ValueError, match="operation_type"):
            OperationClassifier()

    def test_missing_rate_limit_weight_field_raises(self, monkeypatch):
        """Registry entry missing 'rate_limit_weight' raises ValueError on init."""
        bad_registry = {
            "bad_op": {
                "operation_type": "PUBLIC",
                "description": "Missing rate_limit_weight",
            }
        }
        monkeypatch.setattr("config.OPERATION_REGISTRY", bad_registry)
        with pytest.raises(ValueError, match="rate_limit_weight"):
            OperationClassifier()

    def test_missing_description_field_raises(self, monkeypatch):
        """Registry entry missing 'description' raises ValueError on init."""
        bad_registry = {
            "bad_op": {
                "operation_type": "PUBLIC",
                "rate_limit_weight": 3,
            }
        }
        monkeypatch.setattr("config.OPERATION_REGISTRY", bad_registry)
        with pytest.raises(ValueError, match="description"):
            OperationClassifier()

    def test_invalid_operation_type_string_raises(self, monkeypatch):
        """Registry entry with invalid operation_type string raises ValueError on init."""
        bad_registry = {
            "bad_op": {
                "operation_type": "INVALID_TYPE",
                "rate_limit_weight": 5,
                "description": "Bad type",
            }
        }
        monkeypatch.setattr("config.OPERATION_REGISTRY", bad_registry)
        with pytest.raises(ValueError, match="invalid_type|INVALID_TYPE|operation_type"):
            OperationClassifier()

    def test_rate_limit_weight_out_of_range_raises(self, monkeypatch):
        """Registry entry with rate_limit_weight outside 1-10 raises ValueError on init."""
        bad_registry = {
            "bad_op": {
                "operation_type": "PUBLIC",
                "rate_limit_weight": 99,
                "description": "Weight too high",
            }
        }
        monkeypatch.setattr("config.OPERATION_REGISTRY", bad_registry)
        with pytest.raises(ValueError):
            OperationClassifier()

    def test_valid_registry_loads_without_error(self, monkeypatch):
        """A well-formed registry loads without raising any exception."""
        good_registry = {
            "test_op": {
                "operation_type": "PUBLIC",
                "rate_limit_weight": 3,
                "description": "A valid test operation",
            }
        }
        monkeypatch.setattr("config.OPERATION_REGISTRY", good_registry)
        classifier = OperationClassifier()
        assert "test_op" in classifier.get_all_operations()

    def test_empty_registry_loads_without_error(self, monkeypatch):
        """An empty registry loads without raising any exception."""
        monkeypatch.setattr("config.OPERATION_REGISTRY", {})
        classifier = OperationClassifier()
        assert classifier.get_all_operations() == {}

    def test_all_operation_types_accepted_in_registry(self, monkeypatch):
        """All valid OperationType names are accepted in the registry."""
        valid_registry = {
            "public_op": {
                "operation_type": "PUBLIC",
                "rate_limit_weight": 1,
                "description": "Public op",
            },
            "following_op": {
                "operation_type": "FOLLOWING_REQUIRED",
                "rate_limit_weight": 5,
                "description": "Following required op",
            },
            "mutual_op": {
                "operation_type": "MUTUAL_FOLLOWING",
                "rate_limit_weight": 8,
                "description": "Mutual following op",
            },
        }
        monkeypatch.setattr("config.OPERATION_REGISTRY", valid_registry)
        classifier = OperationClassifier()
        assert classifier.classify("public_op") == OperationType.PUBLIC
        assert classifier.classify("following_op") == OperationType.FOLLOWING_REQUIRED
        assert classifier.classify("mutual_op") == OperationType.MUTUAL_FOLLOWING

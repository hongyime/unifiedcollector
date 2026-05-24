"""
Property-based tests for OperationClassifier using Hypothesis.

**Validates: Requirements 2.2, 2.3, 2.4, 12.3, 12.5**
"""

import sys
import os
import string

import pytest
from hypothesis import given, strategies as st, settings, assume

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from src.operation_classifier import OperationClassifier, OperationType, OperationMetadata


# Strategy for generating arbitrary operation name strings
operation_name_strategy = st.text(
    alphabet=string.ascii_letters + string.digits + '_',
    min_size=1,
    max_size=50,
)

# Strategy for generating operation names that are NOT in the registry
def unregistered_operation_strategy(classifier: OperationClassifier):
    known_ops = set(classifier.get_all_operations().keys())
    return operation_name_strategy.filter(lambda name: name not in known_ops)


class TestOperationClassificationDeterminism:
    """Property 13: Operation Classification Determinism - Same operation always returns same type."""

    @given(operation_name_strategy)
    @settings(max_examples=100, deadline=None)
    def test_classification_is_deterministic(self, operation_name: str):
        """
        **Property 13: Operation Classification Determinism**

        For any operation name, classifying it at different times always returns
        the same OperationType value.

        **Validates: Requirements 2.2**
        """
        classifier = OperationClassifier()

        result_first = classifier.classify(operation_name)
        result_second = classifier.classify(operation_name)
        result_third = classifier.classify(operation_name)

        assert result_first == result_second == result_third
        assert isinstance(result_first, OperationType)

    @given(st.lists(operation_name_strategy, min_size=1, max_size=20))
    @settings(max_examples=50, deadline=None)
    def test_classification_consistent_across_multiple_calls(self, operation_names: list):
        """
        **Property 13: Operation Classification Determinism (batch)**

        For any list of operation names, classifying each name twice always
        produces the same result both times.

        **Validates: Requirements 2.2**
        """
        classifier = OperationClassifier()

        first_pass = {name: classifier.classify(name) for name in operation_names}
        second_pass = {name: classifier.classify(name) for name in operation_names}

        assert first_pass == second_pass


class TestOperationClassificationDefaultSafety:
    """Property 14: Operation Classification Default Safety - Unknown operations return PUBLIC."""

    @given(operation_name_strategy)
    @settings(max_examples=100, deadline=None)
    def test_unknown_operation_defaults_to_public(self, operation_name: str):
        """
        **Property 14: Operation Classification Default Safety**

        For any operation name not in the registry, classification returns
        PUBLIC as a safe default.

        **Validates: Requirements 2.3**
        """
        classifier = OperationClassifier()
        known_ops = set(classifier.get_all_operations().keys())

        # Only test names that are NOT in the registry
        assume(operation_name not in known_ops)

        result = classifier.classify(operation_name)

        assert result == OperationType.PUBLIC

    @given(operation_name_strategy)
    @settings(max_examples=100, deadline=None)
    def test_unknown_operation_metadata_defaults_to_public(self, operation_name: str):
        """
        **Property 14: Operation Classification Default Safety (metadata)**

        For any operation name not in the registry, get_operation_metadata()
        returns metadata with operation_type PUBLIC.

        **Validates: Requirements 2.3**
        """
        classifier = OperationClassifier()
        known_ops = set(classifier.get_all_operations().keys())

        assume(operation_name not in known_ops)

        metadata = classifier.get_operation_metadata(operation_name)

        assert isinstance(metadata, OperationMetadata)
        assert metadata.operation_type == OperationType.PUBLIC

    @given(operation_name_strategy)
    @settings(max_examples=100, deadline=None)
    def test_unknown_operation_is_public_helper(self, operation_name: str):
        """
        **Property 14: Operation Classification Default Safety (helper)**

        For any operation name not in the registry, is_public_operation()
        returns True and requires_following() returns False.

        **Validates: Requirements 2.3**
        """
        classifier = OperationClassifier()
        known_ops = set(classifier.get_all_operations().keys())

        assume(operation_name not in known_ops)

        assert classifier.is_public_operation(operation_name) is True
        assert classifier.requires_following(operation_name) is False


class TestRateLimitWeightValidity:
    """Property 15: Rate Limit Weight Validity - All weights between 1-10."""

    def test_all_registered_operations_have_valid_weight(self):
        """
        **Property 15: Rate Limit Weight Validity**

        For any operation in the registry, the rate_limit_weight value is
        between 1 and 10 inclusive.

        **Validates: Requirements 2.4, 12.3**
        """
        classifier = OperationClassifier()
        all_ops = classifier.get_all_operations()

        for op_name, metadata in all_ops.items():
            assert isinstance(metadata.rate_limit_weight, int), (
                f"Operation '{op_name}' rate_limit_weight must be int, "
                f"got {type(metadata.rate_limit_weight).__name__}"
            )
            assert 1 <= metadata.rate_limit_weight <= 10, (
                f"Operation '{op_name}' rate_limit_weight={metadata.rate_limit_weight} "
                f"is outside valid range [1, 10]"
            )

    @given(operation_name_strategy)
    @settings(max_examples=100, deadline=None)
    def test_metadata_weight_always_valid(self, operation_name: str):
        """
        **Property 15: Rate Limit Weight Validity (any name)**

        For any operation name (registered or not), get_operation_metadata()
        always returns a metadata object with a valid rate_limit_weight.

        **Validates: Requirements 2.4, 12.3**
        """
        classifier = OperationClassifier()

        metadata = classifier.get_operation_metadata(operation_name)

        assert isinstance(metadata.rate_limit_weight, int)
        assert 1 <= metadata.rate_limit_weight <= 10

    def test_invalid_weight_raises_on_construction(self):
        """
        **Property 15: Rate Limit Weight Validity (construction guard)**

        Constructing OperationMetadata with an out-of-range rate_limit_weight
        raises a ValueError.

        **Validates: Requirements 12.3**
        """
        with pytest.raises(ValueError):
            OperationMetadata(
                name="test_op",
                operation_type=OperationType.PUBLIC,
                rate_limit_weight=0,
                description="weight too low",
            )

        with pytest.raises(ValueError):
            OperationMetadata(
                name="test_op",
                operation_type=OperationType.PUBLIC,
                rate_limit_weight=11,
                description="weight too high",
            )


class TestOperationRegistryCompleteness:
    """Property 26: Operation Registry Completeness - All operations have required fields."""

    def test_all_operations_have_required_fields(self):
        """
        **Property 26: Operation Registry Completeness**

        For any operation in the registry, the operation metadata includes all
        required fields: operation name, operation type, rate limit weight, and
        description.

        **Validates: Requirements 12.5**
        """
        classifier = OperationClassifier()
        all_ops = classifier.get_all_operations()

        assert len(all_ops) > 0, "Registry must contain at least one operation"

        for op_name, metadata in all_ops.items():
            # name field
            assert hasattr(metadata, 'name'), f"Operation '{op_name}' missing 'name' field"
            assert isinstance(metadata.name, str) and metadata.name, (
                f"Operation '{op_name}' has empty or non-string name"
            )

            # operation_type field
            assert hasattr(metadata, 'operation_type'), (
                f"Operation '{op_name}' missing 'operation_type' field"
            )
            assert isinstance(metadata.operation_type, OperationType), (
                f"Operation '{op_name}' operation_type is not an OperationType enum"
            )

            # rate_limit_weight field
            assert hasattr(metadata, 'rate_limit_weight'), (
                f"Operation '{op_name}' missing 'rate_limit_weight' field"
            )
            assert isinstance(metadata.rate_limit_weight, int), (
                f"Operation '{op_name}' rate_limit_weight is not an int"
            )

            # description field
            assert hasattr(metadata, 'description'), (
                f"Operation '{op_name}' missing 'description' field"
            )
            assert isinstance(metadata.description, str) and metadata.description, (
                f"Operation '{op_name}' has empty or non-string description"
            )

            # registry key matches metadata name
            assert metadata.name == op_name, (
                f"Registry key '{op_name}' does not match metadata.name '{metadata.name}'"
            )

    def test_registry_operation_types_are_valid_enum_values(self):
        """
        **Property 26: Operation Registry Completeness (enum validity)**

        For any operation in the registry, the operation_type is a valid
        OperationType enum member.

        **Validates: Requirements 12.5**
        """
        classifier = OperationClassifier()
        valid_types = set(OperationType)

        for op_name, metadata in classifier.get_all_operations().items():
            assert metadata.operation_type in valid_types, (
                f"Operation '{op_name}' has invalid operation_type: {metadata.operation_type}"
            )

    @given(operation_name_strategy)
    @settings(max_examples=100, deadline=None)
    def test_get_operation_metadata_always_returns_complete_object(self, operation_name: str):
        """
        **Property 26: Operation Registry Completeness (any name)**

        For any operation name (registered or not), get_operation_metadata()
        always returns a fully populated OperationMetadata object with all
        required fields present and valid.

        **Validates: Requirements 12.5**
        """
        classifier = OperationClassifier()

        metadata = classifier.get_operation_metadata(operation_name)

        assert isinstance(metadata, OperationMetadata)
        assert isinstance(metadata.name, str) and metadata.name
        assert isinstance(metadata.operation_type, OperationType)
        assert isinstance(metadata.rate_limit_weight, int)
        assert 1 <= metadata.rate_limit_weight <= 10
        assert isinstance(metadata.description, str) and metadata.description

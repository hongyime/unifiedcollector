# Telegram Toolkit Test Documentation

## Overview

This document describes the test suite for the Telegram Toolkit, including test structure, execution, and coverage goals.

## Table of Contents

- [Test Structure](#test-structure)
- [Running Tests](#running-tests)
- [Test Categories](#test-categories)
- [Test Coverage](#test-coverage)
- [CI/CD Integration](#cicd-integration)
- [Troubleshooting](#troubleshooting)

## Test Structure

```
tests/
├── conftest.py                          # Shared fixtures and configuration
├── test_feature_registry.py             # Feature registry tests
├── test_state_manager.py               # State manager tests
├── test_media_downloader_processor.py   # Media downloader tests
├── test_user_analyzer_processor.py      # User analyzer tests
├── test_e2e_menu_routing.py             # Menu routing tests
├── test_e2e_cli_arguments.py            # CLI argument tests
├── test_csv_export_verification.py      # CSV export tests
├── test_processor_integration.py        # Processor integration tests
└── fixtures/
    ├── mock_accounts.py                 # Mock account data
    ├── mock_telegram_client.py          # Mock Telegram client
    └── test_data/                       # Test data files
```

## Running Tests

### Run All Tests

```bash
pytest
```

### Run Specific Test File

```bash
pytest tests/test_e2e_menu_routing.py
```

### Run Specific Test Class

```bash
pytest tests/test_e2e_menu_routing.py::TestMenuRoutingOption1
```

### Run Specific Test

```bash
pytest tests/test_e2e_menu_routing.py::TestMenuRoutingOption1::test_menu_option_1_routes_to_scan_all_features
```

### Run with Coverage

```bash
pytest --cov=toolkit --cov-report=html
```

### Run Tests with Detailed Output

```bash
pytest -v -s
```

### Run Tests and Stop at First Failure

```bash
pytest -x
```

### Run Tests Matching Pattern

```bash
pytest -k "menu_routing"
```

### Exclude Slow Tests

```bash
pytest -m "not slow"
```

## Test Categories

### 1. Unit Tests

Test individual components in isolation.

**Examples:**
- `test_feature_registry.py` - Processor registration
- `test_state_manager.py` - State manager operations
- `test_media_policy.py` - Media policy logic

**Execution:**
```bash
pytest tests/ -m "not integration and not e2e"
```

### 2. Integration Tests

Test interaction between multiple components.

**Examples:**
- `test_media_downloader_processor.py` - Media downloader integration
- `test_user_analyzer_processor.py` - User analyzer integration
- `test_processor_integration.py` - Processor orchestrator integration

**Execution:**
```bash
pytest tests/ -m integration
```

### 3. End-to-End Tests (E2E)

Test complete workflows from user input to execution.

**Examples:**
- `test_e2e_menu_routing.py` - All menu options (0-16)
- `test_e2e_cli_arguments.py` - All CLI arguments
- `test_csv_export_verification.py` - CSV export verification

**Execution:**
```bash
pytest tests/ -m e2e
```

## Test Coverage

### Coverage Goals

- **Unit Tests**: >80% coverage
- **Integration Tests**: >70% coverage
- **E2E Tests**: >90% routing coverage

### Critical Path Coverage

All menu options and CLI arguments must have tests:

| Category | Items | Tests | Coverage |
|----------|-------|-------|----------|
| Menu Options | 17 | 17 | 100% |
| CLI Arguments | 25+ | 25+ | 100% |
| Processors | 7 | 7 | 100% |
| Managers | 4 | 4 | 100% |
| CSV Export | 2 critical | 2 | 100% |

### Coverage Report

Generate coverage report:

```bash
pytest --cov=toolkit --cov-report=term-missing
```

Generate HTML coverage report:

```bash
pytest --cov=toolkit --cov-report=html
# Open htmlcov/index.html in browser
```

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    
    steps:
    - uses: actions/checkout@v2
    
    - name: Set up Python
      uses: actions/setup-python@v2
      with:
        python-version: '3.9'
    
    - name: Install dependencies
      run: |
        pip install -r requirements.txt
        pip install pytest pytest-asyncio pytest-mock pytest-cov
    
    - name: Run tests
      run: pytest --cov=toolkit --cov-report=xml
    
    - name: Upload coverage
      uses: codecov/codecov-action@v2
```

## Troubleshooting

### Common Issues

#### 1. Async Tests Fail with "asyncio.run() cannot be called from a running event loop"

**Solution:** Ensure pytest-asyncio is installed and configured in `pytest.ini`:

```ini
asyncio_mode = auto
```

#### 2. Tests Fail with ModuleNotFoundError

**Solution:** Add project root to Python path in `conftest.py`:

```python
sys.path.insert(0, str(project_root))
```

#### 3. Tests Fail with State Manager Locking

**Solution:** Use in-memory database for tests and ensure proper cleanup:

```python
def tearDown(self):
    self.state.close()
    shutdown_state_manager()
```

#### 4. Tests Are Slow

**Solution:** Mock external dependencies like Telegram API:

```python
with patch_telegram_client():
    # Test code here
```

### Debugging Failed Tests

#### Run with Detailed Traceback

```bash
pytest --tb=long
```

#### Run withpdb Debugger

```bash
pytest --pdb
```

#### Attach pdb on Failure

```bash
pytest --trace
```

## Test Maintenance

### Adding New Tests

1. Create test file in `tests/` directory
2. Import shared fixtures `from conftest import *`
3. Use appropriate marker (`@pytest.mark.e2e`, `@pytest.mark.integration`)
4. Follow naming convention (`test_<functionality>_<aspect>.py`)
5. Add documentation in docstrings

### Updating Fixtures

When updating fixtures in `conftest.py`:

1. Update fixture implementation
2. Update fixture docstring
3. Run all tests to verify no regressions
4. Update this documentation if fixture behavior changes

## Best Practices

### 1. Use Descriptive Test Names

```python
# Good
def test_menu_option_1_routes_to_scan_all_features(self):
    pass

# Bad
def test_menu_1(self):
    pass
```

### 2. Use Parametrized Tests for Similar Cases

```python
@pytest.mark.parametrize("command,expected_method", [
    ("unified", "scan_all_features"),
    ("1", "scan_all_features"),
])
async def test_cli_unified_commands(self, command, expected_method):
    pass
```

### 3. Mock External Dependencies

```python
@pytest.mark.asyncio
async def test_with_mock_telegram(self):
    with patch_telegram_client():
        # Test code here
        pass
```

### 4. Clean Up Resources

```python
def tearDown(self):
    # Clean up test resources
    self.state.close()
    shutdown_state_manager()
```

## References

- [Pytest Documentation](https://docs.pytest.org/)
- [Pytest Asyncio Plugin](https://pytest-asyncio.readthedocs.io/)
- [Pytest Mock Plugin](https://pytest-mock.readthedocs.io/)
- [Coverage.py](https://coverage.readthedocs.io/)

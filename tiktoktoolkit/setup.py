"""Setup configuration for Unified TikTok Toolkit."""

from setuptools import setup, find_packages
from pathlib import Path

# Read the README file
readme_path = Path(__file__).parent / "README.md"
long_description = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""

# Read requirements
requirements_path = Path(__file__).parent / "requirements.txt"
requirements = []
if requirements_path.exists():
    requirements = requirements_path.read_text(encoding="utf-8").strip().split("\n")
    requirements = [req.strip() for req in requirements if req.strip() and not req.startswith("#")]

setup(
    name="unified-tiktok-toolkit",
    version="0.1.2",
    description="A gallery-dl based toolkit for downloading TikTok videos",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="UTTk Development Team",
    url="https://github.com/example/unified-tiktok-toolkit",
    packages=find_packages(exclude=["tests", "tests.*"]),
    install_requires=requirements,
    python_requires=">=3.8",
    entry_points={
        "console_scripts": [
            "uttk=core.cli:cli",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
    keywords="tiktok download video scraping",
    project_urls={
        "Bug Reports": "https://github.com/example/unified-tiktok-toolkit/issues",
        "Source": "https://github.com/example/unified-tiktok-toolkit",
    },
)

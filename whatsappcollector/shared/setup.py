from setuptools import setup, find_packages

setup(
    name="shared",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "asyncpg>=0.29",
        "pydantic-settings>=2.0",
        "redis[hiredis]>=5.0",
        "prometheus_client>=0.19",
        "structlog>=24.0",
        "aio-pika>=9.0",
    ],
)

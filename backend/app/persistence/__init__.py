"""
Persistence module entry point and repository factory provider.
Selects storage implementation based on application configuration.
"""
from typing import Optional
from app.core.config import get_settings
from app.persistence.interfaces import StorageRepositoryFactory
from app.persistence.file_repo import FileStorageRepositoryFactory


_factory: Optional[StorageRepositoryFactory] = None


def get_storage_factory(force_refresh: bool = False) -> StorageRepositoryFactory:
    """
    Returns the configured StorageRepositoryFactory.
    - Defaults to FileStorageRepositoryFactory when DATABASE_URL is absent or blank.
    - Instantiates PostgresStorageRepositoryFactory when DATABASE_URL is set.
    """
    global _factory
    if _factory is None or force_refresh:
        settings = get_settings(force_reload=force_refresh)
        if settings.is_database_enabled and settings.database_url:
            from app.persistence.postgres import PostgresStorageRepositoryFactory
            _factory = PostgresStorageRepositoryFactory(database_url=settings.database_url)
        else:
            _factory = FileStorageRepositoryFactory(processed_dir=settings.processed_dir)
    return _factory

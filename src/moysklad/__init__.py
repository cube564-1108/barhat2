"""
МойСклад Integration
Интеграция с МойСклад API для проекта "Бархат" (сеть цветочных салонов).
"""

from .client import MoySkladClient, get_client
from .storage import MoySkladStorage, get_storage
from .fetcher import MoySkladFetcher, get_fetcher

__all__ = [
    'MoySkladClient',
    'get_client',
    'MoySkladStorage',
    'get_storage',
    'MoySkladFetcher',
    'get_fetcher',
]

__version__ = '1.0.0'

"""
Модуль кассовых смен БАРХАТ.

Учёт наличных в кассах по точкам продаж с интеграцией RetailCRM.
"""

__version__ = "1.0.0"

# Экспорт основных компонентов
from .server import (
    cashshifts_bp,
    register_cashshifts
)

__all__ = [
    "cashshifts_bp",
    "register_cashshifts",
]

"""LLM abstraction. Imports are lazy so lightweight submodules (jsonutil) stay importable on their own."""
from functools import lru_cache


@lru_cache
def get_llm_router():
    from app.core.config import get_settings
    from app.services.llm.router import LLMRouter

    return LLMRouter(get_settings())

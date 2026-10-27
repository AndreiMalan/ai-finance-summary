"""Embeddings model factory (D-011: no key or vector search disabled -> None -> full-text search)."""
from langchain_core.embeddings import Embeddings

from statbrief.config import Settings


def make_embeddings(settings: Settings) -> Embeddings | None:
    """The configured embeddings model, or None when vector search is off or no API key is set.

    Constructing the client makes no network call; requests happen only when texts are embedded.
    `check_embedding_ctx_length=False` skips LangChain's tiktoken-based token splitting, which would
    download the tiktoken encoding file on a cold start; our inputs are chunks of at most RAG_CHUNK_SIZE
    characters (and short queries), far below the model's 8191-token input limit.
    """
    if not settings.enable_vector_search or settings.openai_api_key is None:
        return None
    if settings.embeddings_provider == "openai":
        from langchain_openai import OpenAIEmbeddings  # deferred: keeps cold starts lean without a key

        return OpenAIEmbeddings(model=settings.embeddings_model, dimensions=settings.embedding_dimensions,
                                api_key=settings.openai_api_key, check_embedding_ctx_length=False)
    raise ValueError(f"unsupported embeddings provider: {settings.embeddings_provider}")

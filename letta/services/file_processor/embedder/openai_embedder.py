import asyncio
from typing import List, Optional, Tuple, cast

from letta.llm_api.llm_client import LLMClient
from letta.llm_api.openai_client import OpenAIClient
from letta.log import get_logger
from letta.otel.tracing import log_event, trace_method
from letta.schemas.embedding_config import EmbeddingConfig
from letta.schemas.enums import ProviderType
from letta.schemas.passage import Passage
from letta.schemas.user import User
from letta.services.file_processor.embedder.base_embedder import BaseEmbedder
from letta.settings import model_settings

logger = get_logger(__name__)


class OpenAIEmbedder(BaseEmbedder):
    """OpenAI-based embedding generation"""

    def __init__(self, embedding_config: Optional[EmbeddingConfig] = None):
        self.default_embedding_config = (
            EmbeddingConfig.default_config(model_name="text-embedding-3-small", provider="openai")
            if model_settings.openai_api_key
            else EmbeddingConfig.default_config(model_name="letta")
        )
        self.embedding_config = embedding_config or self.default_embedding_config
        self.max_concurrent_requests = 20

        # TODO: Unify to global OpenAI client
        self.client: OpenAIClient = cast(
            OpenAIClient,
            LLMClient.create(
                provider_type=ProviderType.openai,
                put_inner_thoughts_first=False,
                actor=None,  # Not necessary
            ),
        )

    @trace_method
    async def _embed_batch(self, batch: List[str], batch_indices: List[int]) -> List[Tuple[int, List[float]]]:
        """Embed a single batch and return embeddings with their original indices"""
        log_event(
            "embedder.batch_started",
            {
                "batch_size": len(batch),
                "model": self.embedding_config.embedding_model,
                "embedding_endpoint_type": self.embedding_config.embedding_endpoint_type,
            },
        )
        embeddings = await self.client.request_embeddings(inputs=batch, embedding_config=self.embedding_config)
        log_event("embedder.batch_completed", {"batch_size": len(batch), "embeddings_generated": len(embeddings)})
        return [(idx, e) for idx, e in zip(batch_indices, embeddings)]

    @trace_method
    async def generate_embedded_passages(self, file_id: str, source_id: str, chunks: List[str], actor: User) -> List[Passage]:
        """Generate embeddings for chunks with batching and concurrent processing"""
        if not chunks:
            return []

        logger.info(f"Generating embeddings for {len(chunks)} chunks using {self.embedding_config.embedding_model}")
        log_event(
            "embedder.generation_started",
            {
                "total_chunks": len(chunks),
                "model": self.embedding_config.embedding_model,
                "embedding_endpoint_type": self.embedding_config.embedding_endpoint_type,
                "batch_size": self.embedding_config.batch_size,
                "file_id": file_id,
                "source_id": source_id,
            },
        )

        # Create batches with their original indices
        batches = []
        batch_indices = []

        for i in range(0, len(chunks), self.embedding_config.batch_size):
            batch = chunks[i : i + self.embedding_config.batch_size]
            indices = list(range(i, min(i + self.embedding_config.batch_size, len(chunks))))
            batches.append(batch)
            batch_indices.append(indices)

        logger.info(f"Processing {len(batches)} batches")
        log_event(
            "embedder.batching_completed",
            {"total_batches": len(batches), "batch_size": self.embedding_config.batch_size, "total_chunks": len(chunks)},
        )

        async def process(batch: List[str], indices: List[int]):
            try:
                return await self._embed_batch(batch, indices)
            except Exception as e:
                logger.error("Failed to embed batch of size %s: %s", len(batch), e)
                log_event("embedder.batch_failed", {"batch_size": len(batch), "error": str(e), "error_type": type(e).__name__})
                raise

        # Execute all batches concurrently with semaphore control
        tasks = [process(batch, indices) for batch, indices in zip(batches, batch_indices)]

        log_event(
            "embedder.concurrent_processing_started",
            {"concurrent_tasks": len(tasks), "max_concurrent_requests": self.max_concurrent_requests},
        )
        results = await asyncio.gather(*tasks)
        log_event("embedder.concurrent_processing_completed", {"batches_processed": len(results)})

        # Flatten results and sort by original index
        indexed_embeddings = []
        for batch_result in results:
            indexed_embeddings.extend(batch_result)

        # Sort by index to maintain original order
        indexed_embeddings.sort(key=lambda x: x[0])

        # Create Passage objects in original order
        passages = []
        for (idx, embedding), text in zip(indexed_embeddings, chunks):
            passage = Passage(
                text=text,
                file_id=file_id,
                source_id=source_id,
                embedding=embedding,
                embedding_config=self.embedding_config,
                organization_id=actor.organization_id,
            )
            passages.append(passage)

        logger.info(f"Successfully generated {len(passages)} embeddings")
        log_event(
            "embedder.generation_completed",
            {"passages_created": len(passages), "total_chunks_processed": len(chunks), "file_id": file_id, "source_id": source_id},
        )
        return passages

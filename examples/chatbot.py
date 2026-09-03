"""The chatbot's configuration, declared and seeded.

A worked example, and a head start on the one migration this store exists for:
`python/chatbot/config.py` currently reads ~20 environment variables through a
`_env` helper, which means a model change is a redeploy and no two replicas can
be shown to agree. The keys below are that file's, one for one, as of the
answer-cache settings it grew most recently.

Run it against a store:

    CONFIGSTORE_ETCD_URL=http://127.0.0.1:2379 uv run python examples/chatbot.py

What it does *not* do is switch the chatbot over. That is a change to the
chatbot, and it belongs in that repo — see the note at the bottom.
"""

from __future__ import annotations

import os

from configstore.admin import ConfigAdmin
from configstore.etcd import Etcd
from configstore.schema import KeySpec, KeyType, Schema

APP = "chatbot"

SCHEMA = Schema.of(
    APP,
    [
        # Which inference server answers, and where. `config.selected_backend`
        # and `selected_base_url`.
        KeySpec(
            "llm.backend",
            KeyType.STRING,
            default="lmstudio",
            description="lmstudio | vmlx | llamacpp",
        ),
        KeySpec("llm.base_url", KeyType.STRING, default="http://localhost:1234/v1"),
        # The three that put the app in server mode when all are named.
        KeySpec("model.chat", KeyType.STRING, required=True),
        KeySpec(
            "model.ocr",
            KeyType.STRING,
            required=True,
            description="must be vision-capable; recorded against each document",
        ),
        KeySpec(
            "model.embed",
            KeyType.STRING,
            required=True,
            description="names the vector collection — changing it builds a new index",
        ),
        # Optional models. Empty means the retriever keeps its RRF ordering and
        # answers go unscored, which is `config.NONE` today.
        KeySpec("model.reranker", KeyType.STRING, default=""),
        KeySpec("model.judge", KeyType.STRING, default=""),
        KeySpec("rerank.backend", KeyType.STRING, default=""),
        KeySpec("rerank.base_url", KeyType.STRING, default=""),
        # Also part of the collection name. See docs/DEPLOY.md.
        KeySpec("chunking.strategy", KeyType.STRING, default="recursive"),
        KeySpec("vector.backend", KeyType.STRING, default="milvus"),
        KeySpec("vector.uri", KeyType.STRING, default="/data/chatbot.db"),
        KeySpec("vector.collection", KeyType.STRING, default="documents"),
        KeySpec("catalog.uri", KeyType.STRING, default="/data/catalog.db"),
        KeySpec("analytics.uri", KeyType.STRING, default="/data/analytics.db"),
        KeySpec("documents.dir", KeyType.STRING, default="/data/documents"),
        # The answer cache. Empty `cache.redis_url` means there is none, which
        # is what `Config.answer_cache_enabled` reads. These two are the best
        # argument in this file for a config store: a similarity threshold is
        # tuned by watching what it does to real misses, and doing that through
        # a redeploy per attempt is why thresholds end up left at their initial
        # guess. Through a watch, a change is live in milliseconds and the
        # revision it landed at says exactly which replicas have it.
        KeySpec(
            "cache.redis_url",
            KeyType.STRING,
            default="",
            description="empty disables the cache entirely",
        ),
        KeySpec(
            "cache.threshold",
            KeyType.FLOAT,
            default=0.92,
            description="minimum cosine similarity to serve a stored answer",
        ),
        KeySpec("cache.ttl_seconds", KeyType.FLOAT, default=3600.0),
        # Bearer tokens for a server started with --api-key. References, not
        # values: etcd stores what it is given in plaintext.
        KeySpec("vmlx.api_key", KeyType.STRING, secret_ref=True),
        KeySpec("llama.api_key", KeyType.STRING, secret_ref=True),
    ],
)

# The LM Studio recommendations from `config.LM_STUDIO_MODELS`, as base-layer
# values rather than schema defaults: these are what an operator chose, and the
# schema's defaults are what the code would do unconfigured. `model.embed` is
# pinned here because every deployment sharing an index must agree on it.
BASE = {
    "model.chat": "qwen/qwen3-vl-8b",
    "model.ocr": "qwen/qwen3-vl-8b",
    "model.embed": "text-embedding-nomic-embed-text-v1.5",
}

# What the Mac mini running vMLX overrides — a different backend, and the
# stronger retriever pair that backend can actually load.
PROD = {
    "llm.backend": "vmlx",
    "cache.redis_url": "redis://redis:6379/0",
    "llm.base_url": "http://host.docker.internal:8000/v1",
    "model.chat": "mlx-community/Qwen3-VL-8B-Instruct-4bit",
    "model.ocr": "mlx-community/Qwen3-VL-8B-Instruct-4bit",
    "model.embed": "mlx-community/embeddinggemma-300m-6bit",
    "model.reranker": "Qwen/Qwen3-Reranker-0.6B",
    "vmlx.api_key": "infisical://apps/chatbot/prod#VMLX_API_KEY",
}


def main() -> None:
    url = os.getenv("CONFIGSTORE_ETCD_URL", "http://127.0.0.1:2379")
    admin = ConfigAdmin(Etcd(url), actor=os.getenv("CONFIGSTORE_ACTOR", "examples"))
    print(f"schema published at revision {admin.publish_schema(SCHEMA)}")
    # One transaction each, so a watching replica never sees a partial seed.
    print(f"base seeded at revision {admin.apply(APP, 'base', BASE)}")
    print(f"prod seeded at revision {admin.apply(APP, 'prod', PROD)}")
    print(f"\n  configstore resolve --app {APP} --env prod")


if __name__ == "__main__":
    main()

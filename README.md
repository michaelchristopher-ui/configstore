# configstore

Configuration storage on etcd: layered resolution, atomic multi-key applies,
watch-fed client caches that survive the store being down, and history that
outlives compaction.

Applications **do not call a config service**. They embed a client that resolves
once at startup and then follows a watch, so reads are dictionary lookups
against an immutable in-memory snapshot — no network in the request path, no
TTL, and a known revision that is identical across every replica. The Streamlit
console and the CLI are write tools; neither is in anybody's read path.

See [ARCHITECTURE.md](ARCHITECTURE.md) for why this is shaped the way it is,
including where Postgres would be the better answer.

## Quickstart

```bash
make sync        # install with dev + ui extras
make etcd-up     # single-node etcd for development
make test        # 62 tests, including the ones that need a live etcd
make ui          # the console on :8502
```

## Declaring an application's keys

The schema is what makes a typo fail on write instead of resolving to a default
for six weeks. Applications declare it in code — defaults belong with the code
that reads them — and publish a copy for the console to build forms from.

```python
from configstore.schema import KeySpec, KeyType, Schema

SCHEMA = Schema.of("chatbot", [
    KeySpec("retrieval.top_k", KeyType.INT, default=8, description="fused hits kept"),
    KeySpec("chat.model", KeyType.STRING, default="qwen/qwen3-vl-8b"),
    KeySpec("judge.enabled", KeyType.BOOL, default=False),
    # etcd stores values in plaintext, so a secret key holds a *reference*.
    KeySpec("vmlx.api_key", KeyType.STRING, secret_ref=True),
])
```

## Reading configuration

```python
from configstore.client import open_config

config = open_config(
    "http://etcd.internal:2379",
    app="chatbot",
    env="prod",
    schema=SCHEMA,
    cache_path="/data/config-cache.json",   # what lets it start when etcd is down
)

config.get("retrieval.top_k")   # in-memory; cannot fail, cannot block
config.revision                 # what this replica is actually running
config.snapshot.stale           # True when serving the cache because etcd was unreachable
```

Pass `on_change=` to react to a change rather than read the new value on the
next request. A callback that raises is logged and cannot kill the watch.

## Writing configuration

Every write states the revision it replaces, so a stale edit is refused rather
than applied:

```python
from configstore.admin import ConfigAdmin, Conflict

admin = ConfigAdmin(etcd, actor="micheal@example.com")   # attribution is required
admin.publish_schema(SCHEMA)

live = admin.current("chatbot", "prod", "retrieval.top_k")
try:
    admin.set("chatbot", "prod", "retrieval.top_k", 12, expect=live.mod_revision)
except Conflict as clash:
    ...  # someone else changed it; clash.current holds what they wrote

# keys that must change together, as one transaction and one watch batch
admin.apply("chatbot", "prod", {"chat.model": "…", "vmlx.api_key": "infisical://…"})
```

## Layers

    schema default   what the application does with no configuration at all
    base             what an operator chose for every environment
    <env>            what this environment overrides

Removing an override is how a value falls back to `base`; the removed value stays
in history. `base` is reserved as a layer name for that reason.

## CLI

```bash
configstore resolve --app chatbot --env prod   # what an app sees, and which layer each value came from
configstore history --app chatbot --layer prod --key retrieval.top_k
configstore rollback --app chatbot --layer prod --key retrieval.top_k --to 3
configstore status                             # revision, dbSize, alarms, defrag hint
```

`CONFIGSTORE_ETCD_URL` and `CONFIGSTORE_ACTOR` set the defaults.

## What this deliberately does not do

Secrets (references only — etcd has no encryption at rest), values over 64 KiB,
cross-app queries, or deletion of history. Authorisation is an environment
variable, which is adequate for one operator behind WARP and nothing more. See
ARCHITECTURE.md.

## Production

`docker-compose.yml` is a **single node, for development only** — that is a file
on one disk, not a durable store. Real deployments run three or five members on
separate hosts via OpenTofu, with `--auto-compaction-retention` set. Both
non-negotiable flags and the reasons are in ARCHITECTURE.md.

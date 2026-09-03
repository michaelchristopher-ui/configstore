# Architecture

Configuration storage on etcd, with a Streamlit console for writes and a client
library that applications embed. The versioning discipline is lifted from
`prompt-registry` — append-only history, atomic promotion, pinned resolution —
but where that project enforces those invariants with partial unique indexes and
transactions it writes itself, this one gets most of them from etcd's own
semantics.

## Why not a backend in front of Postgres

That is the shape most config services take, and it works. What it costs is
specific, and worth naming precisely, because each item is what this design
spends etcd's complexity to buy back.

| | service + Postgres | this |
|---|---|---|
| Where a read goes | over the network, per request or per TTL | a dict lookup in memory |
| Config service down | applications cannot start | applications start on their last snapshot |
| How a change reaches a replica | polled, up to one TTL late | pushed, milliseconds, over a watch |
| "What is prod running right now?" | unanswerable — each replica has its own cache age | a revision number, identical across replicas |
| Two keys that must change together | two statements; a window where half is live | one transaction, one watch batch, no window |
| Lost update from a stale form | possible unless a version column is added *and* every writer uses it | impossible — there is no unconditional write |
| Audit trail | a table someone remembers to insert into | written in the same transaction as the value |

The first two rows are the ones that actually change how a system behaves. A
config service in the read path is a dependency every request inherits; the
usual mitigation, a TTL cache, trades that for not knowing what any replica is
running. A watch-fed local snapshot has neither problem: reads cannot fail
because they touch nothing, and a change is pushed rather than waited for.

**Where Postgres is genuinely better, and this is not close:** anything you need
to *query*. etcd has one access path — scan a prefix — so "every app that
references this Infisical path", "everything changed last Tuesday", or any
report across apps is a full scan in application code. `prompt-registry` asks
those questions of its `vw_*` views trivially. If configuration needs to be
queried rather than read by key, that is a reason to stay on Postgres, and the
key layout below cannot rescue it.

## The layout is the schema

    /cfg/v1/<app>/<layer>/<key>          the live value       (JSON)
    /meta/v1/<app>/schema                declared keys        (JSON)
    /hist/v1/<app>/<layer>/<key>/<seq>   append-only history  (JSON)

`<app>` sits above `<layer>` because the one hot read is "resolve every layer of
one app". A single etcd range is served from one MVCC snapshot, so that read is
atomic by construction — no revision pinning, no torn read across layers, one
round trip. Environment-first would have made it two reads that need a revision
held across them.

Resolution is `default < base < <env>`, a pure function of (raw keys, schema,
env). Purity is load-bearing: the watch loop keeps raw keys current and rebuilds,
so deleting an environment override correctly falls back to the base value
rather than leaving the override behind — the bug every incremental-merge
implementation has.

History lives under `/hist/`, deliberately outside the prefix clients watch, so
audit writes never wake a single application.

## The invariants, and where each one comes from

| Invariant | Enforced by |
|---|---|
| A write cannot silently overwrite a concurrent one | etcd `Txn` comparing `mod_revision`; there is no unconditional `set` |
| A multi-key change is observed atomically | one `Txn`; etcd delivers a transaction as one watch batch |
| A recorded change can never be lost or overwritten | history written in the same `Txn`, which also asserts the slot is empty |
| A typo cannot become configuration | writes validated against the published schema |
| A secret cannot be pasted into the store | `secret_ref` keys accept only `infisical://`-style references |
| An application never reads a half-applied change | snapshots are immutable and swapped by one assignment |

### Why history is not etcd's MVCC

etcd keeps every revision and `range(revision=N)` reads the past directly — for
a while. Compaction destroys that history, and compaction is *mandatory*: skip
it and the backend grows until it trips `--quota-backend-bytes` (2 GiB by
default) and the cluster wedges into a read-only `NOSPACE` alarm. So MVCC
history is a window, not a record. `/hist/` entries are ordinary keys, which
compaction does not touch. `test_history_survives_compaction` is that claim.

### Why history is keyed by a sequence

The first design named each history slot after the revision it *superseded* —
knowable before the commit, unlike the revision a change lands at, and unique
by construction. It has a hole, found by running it: a creation supersedes
nothing, so every creation claims slot 0, and recreating a previously removed
key overwrites the original creation's entry. A per-key sequence has no such
case. It needs no atomic counter: the transaction asserts the slot is empty and
compare-and-swaps the value, so a stale count causes a refused write, never a
lost entry.

## Why this speaks HTTP rather than gRPC

`etcd3`, the obvious client, **cannot be imported** into a virtualenv that pins
a modern protobuf. It ships stubs generated by protoc < 3.19 and raises
`TypeError: Descriptors cannot be created directly` under protobuf 4+/5+ — which
is every environment using pymilvus, as the chatbot does. Verified, not
recalled.

`etcd3gw` avoids that by using the gRPC-gateway's JSON API, but discards the
response header, so there is no way to learn the revision a read happened at —
and without that number there is no consistent snapshot, no watch that resumes
where a read stopped, and no pinned resolution. The whole design rests on it.

So `configstore.etcd` is ~380 lines against the gateway with `requests` as its
only dependency. A client library that cannot be installed beside its consumers
is not a library.

## The watch is the subtle part

Three shapes arrive on a watch stream and conflating any two is a silent
staleness bug:

1. **The creation acknowledgement** carries the *store's* current revision with
   no events. It does **not** mean the watch has delivered up to it — the
   backlog is still coming. Advancing a resume cursor on it skips every change
   in between, permanently if the stream then breaks. Distinguishable only by
   the `created` flag; a progress notification looks otherwise identical. This
   was a live bug in this code, caught by reading a wire dump.
2. **A progress notification** — no events, no `created`, newer revision. It
   legitimately advances the cursor, which is what keeps a reconnect after a
   quiet hour from asking for an hour-old revision that may be compacted.
3. **A cancellation with `compact_revision` set** — the resume point is gone and
   there is no incremental catch-up. The only correct response is to re-read the
   whole prefix. Retrying instead leaves a client frozen at an old revision
   forever, with no error anywhere.

The resume cursor follows the highest `mod_revision` actually applied, not the
batch header, which during catch-up can run ahead of the events in the batch.

## Deliberate non-goals

- **Secrets.** etcd has no encryption at rest; values sit in the boltdb file and
  in every backup of it in plaintext. `secret_ref` keys hold references that an
  application resolves against Infisical at startup.
- **Large values.** Capped at 64 KiB, well under etcd's 1.5 MiB
  `--max-request-bytes`. etcd is a metadata store; a config store approaching
  the quota is storing the wrong thing.
- **Deletion.** `unset` removes a key from a layer — an ordinary edit, since
  that is how a value falls back — and keeps what it removed in history. Nothing
  in this codebase deletes history or compacts `/hist/`.
- **Cross-app queries.** See the Postgres caveat above.
- **Authorisation.** etcd RBAC is per-key-prefix and the console attributes
  writes to an environment variable. That is honest for a single-operator tool
  behind WARP and inadequate the moment two people use it: the console needs the
  same OIDC identity `step ssh login` already issues.

## Operating it

A single node is not a durable store — it is a file on one disk with a daemon in
front. Durability is Raft replication: three members (tolerating one failure) on
separate hosts, provisioned with OpenTofu, not this repo's `docker-compose.yml`,
which is for development. etcd fsyncs every write to its WAL and is far more
sensitive to disk latency than to CPU.

Two flags are not optional: `--auto-compaction-retention` (or the backend grows
until the quota alarm) and a known `--quota-backend-bytes`. In `revision` mode
the retention is a *count of revisions*, despite etcd logging it as a duration
(`=1000` prints as `"1µs"`) — a Duration-typed config field rendered by zap,
not a misparse. Measured on 3.5.17: `=10` compacted to exactly `latest-10`. Compaction frees
pages inside the file but does not return them to the filesystem — `etcdutl
defrag` does, and `configstore status` prints the `dbSize` vs `dbSizeInUse` gap
that says when it is due.

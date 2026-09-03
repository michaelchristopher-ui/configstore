"""The read path: a local snapshot kept current by a watch.

This is the half that makes an etcd-backed store behave differently from a
config service behind HTTP, and the difference is not speed. It is the
dependency direction.

A service that answers `GET /config` puts itself in the caller's critical path.
Every application then either calls it per request — adding a network hop and a
failure mode to every request — or caches with a TTL, which means changes take
up to a TTL to land and nobody can say what any replica is actually running. And
when the config service is down, applications cannot start.

Here, reads are dictionary lookups against an immutable snapshot in memory. The
network is involved exactly twice: once at startup, and then never again except
as a stream that pushes changes in. Specifically:

- **Reads cannot fail and cannot block.** `get` touches no socket and takes no
  lock. There is no TTL, because there is nothing to expire.
- **Changes arrive in milliseconds, not on a TTL.** A watch is a push.
- **An etcd outage is a staleness problem, not an outage.** Startup falls back
  to the last snapshot on disk and sets `stale`. An application that has run
  once can always start again.
- **A multi-key change is observed atomically.** etcd delivers one transaction
  as one watch batch, so a snapshot never shows half of a change — the thing
  N sequential `PUT`s against a database-backed API cannot promise.

The one hazard is the reason most hand-rolled etcd watchers are subtly broken:
if the revision a watcher resumes from has been compacted away, the changes in
between no longer exist, and etcd says so by cancelling the watch with
`compact_revision` set. Ignoring that leaves a client silently frozen at an old
revision forever. `_watch_forever` handles it by re-reading the whole prefix.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .etcd import Etcd, EtcdError, EtcdUnavailable, RangeResult
from .layout import app_prefix, check_app, check_env, parse_config_key
from .schema import Schema
from .snapshot import Snapshot, resolve

log = logging.getLogger(__name__)

# How long to wait before reopening a watch that broke, and the ceiling it backs
# off to. A broken watch is not urgent — the snapshot in hand stays servable —
# so this is deliberately patient rather than tight.
_RETRY_INITIAL = 0.5
_RETRY_MAX = 30.0


class ConfigClient:
    """One application's view of its configuration.

    Built with the schema the application *declares in code*, not the one in the
    store. Defaults are a property of the code that reads them — an application
    upgraded to know about a new key must be able to run before anyone has
    written that key — and it means the client needs nothing from `/meta/` and
    watches exactly one prefix. The copy in the store is for the console's
    benefit and for validating writes; see `admin.ConfigAdmin`.
    """

    def __init__(
        self,
        etcd: Etcd,
        *,
        app: str,
        env: str,
        schema: Schema,
        cache_path: str | os.PathLike[str] | None = None,
        on_change: Callable[[Snapshot], None] | None = None,
        watch: bool = True,
    ) -> None:
        self._etcd = etcd
        self._app = check_app(app)
        self._env = check_env(env)
        self._schema = schema
        self._cache = Path(cache_path) if cache_path else None
        self._on_change = on_change
        self._want_watch = watch

        self._prefix = app_prefix(app)
        self._raw: dict[str, Any] = {}
        self._snapshot: Snapshot | None = None
        self._revision = 0
        self._connected = False
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        # A session of its own, so `stop()` can close the watch's socket without
        # disturbing an ordinary read in flight on the main one.
        self._watch_etcd: Etcd | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> Snapshot:
        """Load configuration and begin following changes.

        Returns the snapshot the application should start on. Raises only if
        there is no configuration to be had at all — etcd unreachable *and* no
        usable cache — because that is the one case where guessing would be
        worse than refusing to start.
        """
        try:
            self._load()
        except EtcdUnavailable as exc:
            if not self._load_cache():
                raise
            log.warning(
                "configstore: etcd unreachable (%s); starting on the cached "
                "snapshot at revision %s",
                exc,
                self._revision,
            )
        if self._want_watch:
            self._thread = threading.Thread(
                target=self._watch_forever,
                name=f"configstore-watch-{self._app}",
                daemon=True,
            )
            self._thread.start()
        assert self._snapshot is not None
        return self._snapshot

    def stop(self, timeout: float = 2.0) -> None:
        self._stopping.set()
        if self._watch_etcd is not None:
            self._watch_etcd.close()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def __enter__(self) -> ConfigClient:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- reads -------------------------------------------------------------

    @property
    def snapshot(self) -> Snapshot:
        """The current snapshot.

        A plain attribute read of an immutable object. The watch thread swaps
        this reference in one assignment, so a reader gets the old snapshot or
        the new one and never a half-applied change — no lock on the read path,
        and none needed.
        """
        if self._snapshot is None:
            raise RuntimeError("ConfigClient.start() has not been called")
        return self._snapshot

    def get(self, key: str, fallback: Any = None) -> Any:
        return self.snapshot.get(key, fallback)

    def require(self, key: str) -> Any:
        return self.snapshot.require(key)

    @property
    def revision(self) -> int:
        return self.snapshot.revision

    @property
    def connected(self) -> bool:
        """Whether the watch is currently following changes.

        False means the snapshot is still valid as of its revision but is no
        longer being updated — worth a metric, not worth failing a request.
        """
        return self._connected

    def refresh(self) -> Snapshot:
        """Re-read everything now, ignoring the watch. For the console and tests."""
        self._load()
        return self.snapshot

    # -- internals ---------------------------------------------------------

    def _raw_from(self, result: RangeResult) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        for kv in result.kvs:
            try:
                parse_config_key(kv.key)
                raw[kv.key] = kv.json()
            except (ValueError, json.JSONDecodeError):
                # A key under this prefix that this layout did not write, or a
                # value that is not JSON. Skipped rather than fatal, and kept in
                # the store: something put it there and deleting it is not this
                # code's call.
                log.warning("configstore: ignoring unreadable key %s", kv.key)
        return raw

    def _load(self) -> None:
        result = self._etcd.range(self._prefix, prefix=True)
        self._raw = self._raw_from(result)
        self._revision = result.revision
        self._connected = True
        self._rebuild(stale=False)
        self._save_cache()

    def _rebuild(self, *, stale: bool) -> None:
        snapshot = resolve(
            app=self._app,
            env=self._env,
            revision=self._revision,
            raw=self._raw,
            schema=self._schema,
            stale=stale,
        )
        previous = self._snapshot
        self._snapshot = snapshot
        changed = previous is not None and previous.values != snapshot.values
        if self._on_change is not None and changed:
            try:
                self._on_change(snapshot)
            except Exception:
                # An application's reaction to a config change must not be able
                # to kill the thread that delivers the next one.
                log.exception("configstore: on_change callback raised")

    def _watch_forever(self) -> None:
        backoff = _RETRY_INITIAL
        self._watch_etcd = self._etcd.clone()
        while not self._stopping.is_set():
            try:
                for batch in self._watch_etcd.watch(
                    self._prefix, prefix=True, start_revision=self._revision + 1
                ):
                    if self._stopping.is_set():
                        return
                    self._connected = True
                    backoff = _RETRY_INITIAL

                    if batch.created:
                        # The stream is open. Its header names the store's
                        # current revision, *not* how far this watch has
                        # delivered, so the cursor must not move: the changes
                        # since `start_revision` are still on their way, and
                        # advancing here would skip them if the connection
                        # broke before they landed.
                        continue

                    if batch.lost_history:
                        # The resume point is gone: the only way back to correct
                        # is to re-read the world. Never treat this as transient.
                        log.warning(
                            "configstore: revision %s was compacted (server is at "
                            "%s); re-reading %s",
                            self._revision + 1,
                            batch.compact_revision,
                            self._prefix,
                        )
                        self._load()
                        break
                    if batch.canceled:
                        log.warning(
                            "configstore: watch cancelled: %s", batch.cancel_reason
                        )
                        break

                    if batch.events:
                        for event in batch.events:
                            if event.type == "DELETE":
                                self._raw.pop(event.kv.key, None)
                            else:
                                try:
                                    self._raw[event.kv.key] = event.kv.json()
                                except json.JSONDecodeError:
                                    log.warning(
                                        "configstore: ignoring non-JSON write to %s",
                                        event.kv.key,
                                    )
                        # The cursor follows the highest revision actually
                        # applied, not the batch header. During catch-up the
                        # header carries the store's current revision, which can
                        # run ahead of the events in the batch; trusting it would
                        # skip the remainder if the stream broke mid-catch-up.
                        # etcd never splits one transaction across batches, so
                        # the maximum `mod_revision` here is a safe resume point.
                        self._revision = max(
                            self._revision,
                            max(event.kv.mod_revision for event in batch.events),
                        )
                        # Rebuilt from the raw keys rather than patched, so a
                        # deleted environment override correctly falls back to
                        # the base layer instead of lingering.
                        self._rebuild(stale=False)
                        self._save_cache()
                    else:
                        # A progress notification: no change, newer revision.
                        # Moving the cursor means a reconnect after a quiet
                        # stretch resumes from now rather than from an old
                        # revision that may since have been compacted.
                        self._revision = max(self._revision, batch.revision)
            except (EtcdUnavailable, EtcdError) as exc:
                self._connected = False
                if self._stopping.is_set():
                    return
                log.warning(
                    "configstore: watch on %s dropped (%s); retrying in %.1fs",
                    self._prefix,
                    exc,
                    backoff,
                )
                self._stopping.wait(backoff)
                backoff = min(backoff * 2, _RETRY_MAX)

    # -- the disk cache ----------------------------------------------------

    def _save_cache(self) -> None:
        if self._cache is None or self._snapshot is None:
            return
        try:
            self._cache.parent.mkdir(parents=True, exist_ok=True)
            # Written to a sibling and renamed: a crash mid-write must not be
            # able to leave a truncated cache, because the cache is exactly what
            # gets read when things are already going badly.
            fd, tmp = tempfile.mkstemp(dir=str(self._cache.parent), prefix=".cfg-")
            with os.fdopen(fd, "wb") as handle:
                handle.write(self._snapshot.to_cache(self._raw))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._cache)
        except OSError as exc:
            log.warning("configstore: could not write cache %s: %s", self._cache, exc)

    def _load_cache(self) -> bool:
        if self._cache is None or not self._cache.exists():
            return False
        try:
            parsed = json.loads(self._cache.read_bytes().decode("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("configstore: cache %s is unusable: %s", self._cache, exc)
            return False
        if parsed.get("app") != self._app or parsed.get("env") != self._env:
            # A cache written by a different app or environment. Refused rather
            # than adapted: starting prod on staging's configuration is a worse
            # outcome than refusing to start.
            log.warning("configstore: cache %s belongs to another app/env", self._cache)
            return False
        self._raw = dict(parsed.get("raw", {}))
        self._revision = int(parsed.get("revision", 0))
        self._connected = False
        self._rebuild(stale=True)
        return True


def open_config(
    url: str,
    *,
    app: str,
    env: str,
    schema: Schema,
    cache_path: str | os.PathLike[str] | None = None,
    timeout: float = 5.0,
) -> ConfigClient:
    """Build and start a client in one call — what an application's wiring wants."""
    client = ConfigClient(
        Etcd(url, timeout=timeout),
        app=app,
        env=env,
        schema=schema,
        cache_path=cache_path,
    )
    client.start()
    return client

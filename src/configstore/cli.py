"""Command line access, for the things a console should not be the only way to do.

Deliberately small. `resolve` is the one that earns its place: it prints exactly
what an application would see, with the layer each value came from, so "why is
prod running that" is answerable without attaching a debugger to prod.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .admin import ConfigAdmin, Conflict, compact_to_latest
from .client import ConfigClient
from .etcd import Etcd, EtcdError
from .schema import Schema

DEFAULT_URL = os.getenv("CONFIGSTORE_ETCD_URL", "http://127.0.0.1:2379")
DEFAULT_ACTOR = os.getenv("CONFIGSTORE_ACTOR") or os.getenv("USER") or "cli"


def _value(raw: str) -> Any:
    """Parse a command-line value as JSON, falling back to a bare string.

    So `--value 12` is an integer and `--value qwen/qwen3-vl-8b` is a string,
    without making the caller quote every model id. The schema decides whether
    the result is acceptable, so a mis-parse is refused rather than stored.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _print_snapshot(client: ConfigClient) -> None:
    snapshot = client.snapshot
    print(f"{snapshot.app}/{snapshot.env} at revision {snapshot.revision}", end="")
    print(" (STALE — served from cache)" if snapshot.stale else "")
    width = max((len(k) for k in snapshot.values), default=0)
    for key in sorted(snapshot.values):
        print(
            f"  {key:<{width}}  {snapshot.values[key]!r:<24} [{snapshot.sources[key]}]"
        )
    for key in snapshot.unknown:
        print(f"  {key:<{width}}  (in the store, not declared by the schema)")
    for problem in snapshot.invalid:
        print(f"  ! {problem}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="configstore", description=__doc__)
    parser.add_argument("--etcd", default=DEFAULT_URL, help=f"default {DEFAULT_URL}")
    parser.add_argument(
        "--actor", default=DEFAULT_ACTOR, help="recorded on every write"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    resolve = sub.add_parser("resolve", help="print what an app would see")
    resolve.add_argument("--app", required=True)
    resolve.add_argument("--env", required=True)

    get = sub.add_parser("get", help="read one key from one layer")
    for target in (get,):
        target.add_argument("--app", required=True)
        target.add_argument("--layer", required=True)
        target.add_argument("--key", required=True)

    setter = sub.add_parser("set", help="write one key")
    setter.add_argument("--app", required=True)
    setter.add_argument("--layer", required=True)
    setter.add_argument("--key", required=True)
    setter.add_argument("--value", required=True)
    setter.add_argument(
        "--expect", type=int, default=None, help="mod_revision to require"
    )

    unset = sub.add_parser("unset", help="remove one key from one layer")
    unset.add_argument("--app", required=True)
    unset.add_argument("--layer", required=True)
    unset.add_argument("--key", required=True)

    listing = sub.add_parser("list", help="every key in one layer")
    listing.add_argument("--app", required=True)
    listing.add_argument("--layer", required=True)

    history = sub.add_parser("history", help="every recorded change to one key")
    history.add_argument("--app", required=True)
    history.add_argument("--layer", required=True)
    history.add_argument("--key", required=True)

    rollback = sub.add_parser("rollback", help="restore a value from history")
    rollback.add_argument("--app", required=True)
    rollback.add_argument("--layer", required=True)
    rollback.add_argument("--key", required=True)
    rollback.add_argument(
        "--to", type=int, required=True, help="the `replaces` revision"
    )

    push = sub.add_parser("push-schema", help="publish an app's declared keys")
    push.add_argument(
        "--file", required=True, help="JSON, as `Schema.to_bytes` writes it"
    )

    show_schema = sub.add_parser("schema", help="print an app's published schema")
    show_schema.add_argument("--app", required=True)

    sub.add_parser("status", help="cluster health, size and revision")

    compact = sub.add_parser("compact", help="discard MVCC history")
    compact.add_argument("--keep", type=int, default=1000)

    args = parser.parse_args(argv)
    etcd = Etcd(args.etcd)

    try:
        if args.command == "resolve":
            schema = ConfigAdmin(etcd, actor=args.actor, validate=False).read_schema(
                args.app
            )
            client = ConfigClient(
                etcd, app=args.app, env=args.env, schema=schema, watch=False
            )
            client.start()
            _print_snapshot(client)
            return 0

        admin = ConfigAdmin(etcd, actor=args.actor)

        if args.command == "get":
            live = admin.current(args.app, args.layer, args.key)
            if live is None:
                print("(absent)")
                return 1
            print(json.dumps(live.json()), f"# mod_revision {live.mod_revision}")
        elif args.command == "set":
            revision = admin.set(
                args.app, args.layer, args.key, _value(args.value), expect=args.expect
            )
            print(f"written at revision {revision}")
        elif args.command == "unset":
            print(f"removed at revision {admin.unset(args.app, args.layer, args.key)}")
        elif args.command == "list":
            layer = admin.read_layer(args.app, args.layer)
            width = max((len(k) for k in layer), default=0)
            for key in sorted(layer):
                print(
                    f"  {key:<{width}}  {json.dumps(layer[key].json()):<24} "
                    f"# mod_revision {layer[key].mod_revision}"
                )
        elif args.command == "history":
            for entry in admin.history(args.app, args.layer, args.key):
                print(
                    f"  replaces={entry.replaces:<8} {entry.op:<6} "
                    f"{json.dumps(entry.previous)} -> {json.dumps(entry.value)}"
                    f"  by {entry.actor} at {entry.at}"
                )
        elif args.command == "rollback":
            revision = admin.rollback(args.app, args.layer, args.key, to=args.to)
            print(f"restored at revision {revision}")
        elif args.command == "push-schema":
            with open(args.file, "rb") as handle:
                schema = Schema.from_bytes(handle.read())
            print(
                f"published schema for {schema.app} "
                f"at revision {admin.publish_schema(schema)}"
            )
        elif args.command == "schema":
            print(admin.read_schema(args.app).to_bytes().decode())
        elif args.command == "status":
            status = etcd.status()
            revision = status.get("header", {}).get("revision")
            size = int(status.get("dbSize", 0))
            in_use = int(status.get("dbSizeInUse", 0))
            print(f"version   {status.get('version')}")
            print(f"revision  {revision}")
            print(
                f"dbSize    {size / 1_048_576:.1f} MiB "
                f"(in use {in_use / 1_048_576:.1f} MiB)"
            )
            if size and in_use / size < 0.5:
                print(
                    "  note: over half the file is free space — "
                    "`etcdutl defrag` returns it"
                )
            for alarm in status.get("errors", ()):
                print(f"  ALARM: {alarm}", file=sys.stderr)
        elif args.command == "compact":
            print(f"compacted to revision {compact_to_latest(etcd, keep=args.keep)}")
    except Conflict as conflict:
        print(f"refused: {conflict}", file=sys.stderr)
        return 2
    except (EtcdError, ValueError, KeyError) as problem:
        print(f"error: {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

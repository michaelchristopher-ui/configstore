"""Declared keys, their types, and the rules a write has to satisfy.

etcd stores opaque bytes and validates nothing, which is the gap that turns a
key/value store into a config store people trust. Everything a Postgres-backed
service would get from column types and constraints has to live here instead,
and it buys three things worth having:

- **A typo cannot become configuration.** A write to a key no schema declares is
  refused. `retreival.top_k` fails at the UI instead of resolving to a default
  for the six weeks before anyone notices.
- **A type error surfaces on write.** `top_k = "eight"` is rejected by whoever
  typed it, not by an application that has already started.
- **Secrets cannot be pasted into the store.** etcd has no encryption at rest —
  values sit in the boltdb file and in every snapshot of it in plaintext. A key
  marked `secret_ref` accepts only a *reference* to a secret manager, so the
  worst case of a leaked etcd backup is knowing which Infisical path an app
  reads, rather than holding its API keys.

The schema is itself a config value: it lives at `/meta/v1/<app>/schema`,
versions with the same MVCC machinery as everything else, and is read once per
resolution rather than compiled in — which is what lets the console offer a form
of the right shape without shipping application code.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .layout import check_app, check_key


class SchemaError(ValueError):
    """A schema that does not describe anything writable."""


class UnknownKey(KeyError):
    """A key no schema declares — refused on write, reported on read."""


class ValidationError(ValueError):
    """A value that does not satisfy its declared type or its reference rule."""


class KeyType(StrEnum):
    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    # An object or array, stored as-is. The escape hatch, and deliberately the
    # least useful one: a `json` key cannot be validated beyond "parses", so
    # anything that can be expressed as a scalar should be.
    JSON = "json"


# What a `secret_ref` value has to look like. A scheme rather than a free string
# so the resolver can dispatch on it, and so a literal API key — which has no
# scheme — is rejected by pattern rather than by hoping nobody pastes one.
_REFERENCE = re.compile(r"^(infisical|vault|awssm|env)://[^\s]+$")


@dataclass(frozen=True, slots=True)
class KeySpec:
    """One declared key.

    `default` is what resolution uses when no layer holds the key, and it is
    part of the *schema* rather than a base-layer value on purpose: a default
    is what the application would do with no configuration at all, so it
    belongs with the declaration and not in a layer someone can clear. The base
    layer is for values an operator chose that happen to apply everywhere.
    """

    name: str
    type: KeyType
    default: Any = None
    required: bool = False
    description: str = ""
    secret_ref: bool = False

    def __post_init__(self) -> None:
        check_key(self.name)
        if self.required and self.default is not None:
            raise SchemaError(
                f"{self.name}: a key with a default is never missing, so "
                "`required` would be unreachable — drop one of the two"
            )
        if self.secret_ref and self.type is not KeyType.STRING:
            raise SchemaError(
                f"{self.name}: a secret reference is a string; {self.type.value} "
                "cannot hold one"
            )
        if self.default is not None:
            # Validated at construction so a bad default fails when the schema
            # is written rather than when a key happens to be absent.
            self.coerce(self.default)

    def coerce(self, value: Any) -> Any:
        """The value as its declared type, or `ValidationError`.

        Strict about `bool`, which is the one Python type that would otherwise
        slip through every other check: `True` is an `int`, so an unguarded
        `int` key would silently accept it and an unguarded `bool` key would
        silently accept `1`. Configuration read by a feature flag is exactly
        where that must not happen.
        """
        if self.type is KeyType.BOOL:
            if not isinstance(value, bool):
                raise ValidationError(f"{self.name}: expected a boolean, got {value!r}")
            return value
        if self.type is KeyType.INT:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValidationError(
                    f"{self.name}: expected an integer, got {value!r}"
                )
            return value
        if self.type is KeyType.FLOAT:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(f"{self.name}: expected a number, got {value!r}")
            return float(value)
        if self.type is KeyType.STRING:
            if not isinstance(value, str):
                raise ValidationError(f"{self.name}: expected a string, got {value!r}")
            if self.secret_ref and not _REFERENCE.match(value):
                raise ValidationError(
                    f"{self.name} is declared `secret_ref`, so it takes a reference "
                    "like infisical://apps/chatbot/prod#VMLX_API_KEY, not a literal "
                    "value — etcd holds this in plaintext"
                )
            return value
        # JSON: anything that survived the round trip through the store.
        return value

    def as_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {"type": self.type.value}
        if self.default is not None:
            body["default"] = self.default
        if self.required:
            body["required"] = True
        if self.description:
            body["description"] = self.description
        if self.secret_ref:
            body["secret_ref"] = True
        return body


@dataclass(frozen=True, slots=True)
class Schema:
    """Every key one app declares."""

    app: str
    keys: Mapping[str, KeySpec] = field(default_factory=dict)

    def __post_init__(self) -> None:
        check_app(self.app)
        for name, spec in self.keys.items():
            if name != spec.name:
                raise SchemaError(
                    f"schema key {name!r} disagrees with spec {spec.name!r}"
                )

    @classmethod
    def of(cls, app: str, specs: Iterable[KeySpec]) -> Schema:
        return cls(app=app, keys={spec.name: spec for spec in specs})

    def spec(self, key: str) -> KeySpec:
        try:
            return self.keys[key]
        except KeyError:
            raise UnknownKey(
                f"{key!r} is not declared by the schema for {self.app!r}; "
                f"declared keys are: {', '.join(sorted(self.keys)) or '(none)'}"
            ) from None

    def coerce(self, key: str, value: Any) -> Any:
        return self.spec(key).coerce(value)

    def defaults(self) -> dict[str, Any]:
        return {
            name: spec.default
            for name, spec in self.keys.items()
            if spec.default is not None
        }

    def required(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, spec in self.keys.items() if spec.required))

    def secret_refs(self) -> tuple[str, ...]:
        return tuple(
            sorted(name for name, spec in self.keys.items() if spec.secret_ref)
        )

    # -- storage ----------------------------------------------------------

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "app": self.app,
                "keys": {n: s.as_json() for n, s in sorted(self.keys.items())},
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> Schema:
        parsed = json.loads(raw.decode("utf-8"))
        keys = {}
        for name, body in parsed.get("keys", {}).items():
            keys[name] = KeySpec(
                name=name,
                type=KeyType(body["type"]),
                default=body.get("default"),
                required=bool(body.get("required", False)),
                description=str(body.get("description", "")),
                secret_ref=bool(body.get("secret_ref", False)),
            )
        return cls(app=parsed["app"], keys=keys)


# The schema an app gets when none has been written yet. Permissive on purpose:
# an app that has not declared anything should still be able to read what is in
# the store, so adopting this store does not have to start with a schema.
# `ConfigAdmin` refuses *writes* against an empty schema instead, which is the
# half where a typo is created rather than observed.
def empty(app: str) -> Schema:
    return Schema(app=app, keys={})

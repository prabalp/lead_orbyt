"""Operator CLI for managing leadorbyt tenants and API keys.

Deliberately kept separate from the MCP tool surface in server.py -- a
client authenticated as one tenant must never be able to mint credentials
for another tenant through the same channel it uses the product.

An API key is a plain Bearer token for `{LEADORBYT_PUBLIC_URL}/mcp`, so any
MCP client or gateway (Claude Desktop, orbytlink, ...) can use one. Give each
client its own labelled key (`create-key`) so it can be revoked on its own.
"""

import argparse
import sys
from datetime import datetime, timezone

from . import store


def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def _print_new_key(raw_key: str) -> None:
    print(f"api_key: {raw_key}")
    print("(this key is shown once and cannot be retrieved again)")


def create_user(label: str, key_label: str | None) -> None:
    user_id = store.create_user(label)
    raw_key = store.create_api_key(user_id, label=key_label)
    print(f"user_id: {user_id}")
    _print_new_key(raw_key)


def create_key(user_id: str, label: str | None) -> None:
    if not store.user_exists(user_id):
        _fail(f"no such user: {user_id}")
    _print_new_key(store.create_api_key(user_id, label=label))


def _fmt_time(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts else "-"


def list_keys(user_id: str) -> None:
    if not store.user_exists(user_id):
        _fail(f"no such user: {user_id}")
    keys = store.list_api_keys(user_id)
    if not keys:
        print("(no keys)")
    for key in keys:
        status = f"revoked {_fmt_time(key['revoked_at'])}" if key["revoked_at"] else "active"
        label = key["label"] or "-"
        print(f"{key['id']}  {label:<20}  created {_fmt_time(key['created_at'])}  {status}")


def revoke_key(raw_key: str | None, key_id: str | None) -> None:
    if key_id:
        ok = store.revoke_api_key_by_id(key_id)
    else:
        from . import auth

        ok = store.revoke_api_key(auth.hash_key(raw_key))
    if not ok:
        _fail("no matching active key (for --id, use the id from list-keys)")
    print("revoked.")


def list_users() -> None:
    for user in store.list_users():
        status = "disabled" if user["disabled_at"] else "active"
        print(f"{user['id']}  {user['label']!r}  {status}")


def set_disabled(user_id: str, disabled: bool) -> None:
    if not store.set_user_disabled(user_id, disabled):
        _fail(f"no such user: {user_id}")
    print("disabled." if disabled else "enabled.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="leadorbyt-admin")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_user_parser = subparsers.add_parser("create-user", help="Create a tenant and its first API key")
    create_user_parser.add_argument("label", help="Human-readable name/email for this tenant")
    create_user_parser.add_argument("--key-label", help="Note for the first key, e.g. 'claude-desktop'")

    create_key_parser = subparsers.add_parser(
        "create-key", help="Mint another API key for an existing tenant (e.g. one per MCP client)"
    )
    create_key_parser.add_argument("user_id")
    create_key_parser.add_argument("--label", help="Where this key is used, e.g. 'orbytlink'")

    list_keys_parser = subparsers.add_parser("list-keys", help="List a tenant's API keys (ids, labels, status)")
    list_keys_parser.add_argument("user_id")

    revoke_key_parser = subparsers.add_parser("revoke-key", help="Revoke an API key by raw value or --id")
    revoke_key_parser.add_argument("raw_key", nargs="?", help="The raw API key to revoke")
    revoke_key_parser.add_argument("--id", dest="key_id", help="The key id shown by list-keys")

    subparsers.add_parser("list-users", help="List all tenants")

    disable_parser = subparsers.add_parser("disable-user", help="Block a tenant: all of its API keys stop working")
    disable_parser.add_argument("user_id")

    enable_parser = subparsers.add_parser("enable-user", help="Re-enable a disabled tenant")
    enable_parser.add_argument("user_id")

    args = parser.parse_args()
    if args.command == "revoke-key" and bool(args.raw_key) == bool(args.key_id):
        parser.error("revoke-key: give exactly one of RAW_KEY or --id")

    store.connect()

    if args.command == "create-user":
        create_user(args.label, args.key_label)
    elif args.command == "create-key":
        create_key(args.user_id, args.label)
    elif args.command == "list-keys":
        list_keys(args.user_id)
    elif args.command == "revoke-key":
        revoke_key(args.raw_key, args.key_id)
    elif args.command == "list-users":
        list_users()
    elif args.command == "disable-user":
        set_disabled(args.user_id, True)
    elif args.command == "enable-user":
        set_disabled(args.user_id, False)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

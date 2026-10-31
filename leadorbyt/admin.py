"""Operator CLI for managing leadorbyt tenants and API keys.

Deliberately kept separate from the MCP tool surface in server.py -- a
client authenticated as one tenant must never be able to mint credentials
for another tenant through the same channel it uses the product.
"""

import argparse
import sys

from . import store


def create_user(label: str) -> None:
    user_id = store.create_user(label)
    raw_key = store.create_api_key(user_id)
    print(f"user_id: {user_id}")
    print(f"api_key: {raw_key}")
    print("(this key is shown once and cannot be retrieved again)")


def revoke_key(raw_key: str) -> None:
    from . import auth

    store.revoke_api_key(auth.hash_key(raw_key))
    print("revoked.")


def list_users() -> None:
    for user in store.list_users():
        status = "disabled" if user["disabled_at"] else "active"
        print(f"{user['id']}  {user['label']!r}  {status}")


def set_disabled(user_id: str, disabled: bool) -> None:
    if not store.set_user_disabled(user_id, disabled):
        print(f"no such user: {user_id}", file=sys.stderr)
        sys.exit(1)
    print("disabled." if disabled else "enabled.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="leadorbyt-admin")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_user_parser = subparsers.add_parser("create-user", help="Create a tenant and its first API key")
    create_user_parser.add_argument("label", help="Human-readable name/email for this tenant")

    revoke_key_parser = subparsers.add_parser("revoke-key", help="Revoke an API key")
    revoke_key_parser.add_argument("raw_key", help="The raw API key to revoke")

    subparsers.add_parser("list-users", help="List all tenants")

    disable_parser = subparsers.add_parser(
        "disable-user", help="Block a tenant: all of its API keys stop working"
    )
    disable_parser.add_argument("user_id")

    enable_parser = subparsers.add_parser("enable-user", help="Re-enable a disabled tenant")
    enable_parser.add_argument("user_id")

    args = parser.parse_args()
    store.connect()

    if args.command == "create-user":
        create_user(args.label)
    elif args.command == "revoke-key":
        revoke_key(args.raw_key)
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

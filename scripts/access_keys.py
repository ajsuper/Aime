#!/usr/bin/env python3
"""Aime access-control admin tool.

Manages who may send messages through the paid model backend, via the
`api_access` flag and single-use invite keys. This is the command-line admin
interface; it is a thin wrapper over `aime.auth.LocalAuthBackend`, which is the
single source of truth. A future admin web dashboard will wrap the same
backend methods — keep all logic there, not here.

See docs/access-control.md for the deployment model (AIME_ACCESS_MODE etc.).

Examples:
    # Mint 3 invite keys, one labelled
    ./scripts/access_keys.py gen 3
    ./scripts/access_keys.py gen --note "Alice"

    # See all keys and all users with their access flag
    ./scripts/access_keys.py list

    # Directly grant / revoke a user (admin override; over-limit kill switch)
    ./scripts/access_keys.py grant alice
    ./scripts/access_keys.py revoke alice

    # Kill an unredeemed key; zero everyone for a billing cutover
    ./scripts/access_keys.py revoke-key <key>
    ./scripts/access_keys.py revoke-all
"""

import os
import sys
import argparse

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# This script's dependencies (argon2-cffi, cryptography — pulled in by
# aime.auth) live only in the uv-managed virtualenv that install.sh builds;
# the host Python itself has nothing installed. Re-exec under that venv so the
# admin can run `./scripts/access_keys.py ...` directly, the same way the .sh
# service scripts invoke .venv/bin/python. Skipped when the venv is absent
# (e.g. inside the Docker image, where the deps are installed system-wide).
_VENV_PYTHON = os.path.join(_REPO_ROOT, ".venv", "bin", "python")
if os.environ.get("_AIME_REEXEC") != "1" and os.path.exists(_VENV_PYTHON):
    os.environ["_AIME_REEXEC"] = "1"
    os.execv(_VENV_PYTHON, [_VENV_PYTHON, os.path.abspath(__file__), *sys.argv[1:]])

# Allow running directly from a checkout: make the `aime` package importable.
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

try:
    from aime import config as aime_config  # noqa: E402
    from aime import auth as _auth  # noqa: E402
except ModuleNotFoundError as exc:  # noqa: E402
    sys.stderr.write(
        f"Error: missing dependency ({exc.name}). "
        "Run ./scripts/install.sh to build the virtualenv first.\n"
    )
    raise SystemExit(1)


def _backend() -> _auth.LocalAuthBackend:
    return _auth.LocalAuthBackend(os.path.join(aime_config.DATABASE_DIR, "auth.sql"))


def cmd_gen(args: argparse.Namespace) -> int:
    backend = _backend()
    count = max(1, args.count)
    print(f"Generated {count} single-use invite key(s) — copy them now, "
          f"they are not recoverable:\n")
    for _ in range(count):
        print(f"  {backend.generate_access_key(args.note)}")
    if args.note:
        print(f"\nNote on each: {args.note!r}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    backend = _backend()

    users = backend.list_users()
    print(f"Users ({len(users)}):")
    for u in users:
        flag = "send" if u.api_access else "no-send"
        print(f"  #{u.id:<4} {u.username:<24} [{flag}]")

    keys = backend.list_access_keys()
    print(f"\nInvite keys ({len(keys)}):")
    if not keys:
        print("  (none — mint some with 'gen')")
    for k in keys:
        if k.redeemed:
            state = f"redeemed by {k.redeemed_by_username} at {k.redeemed_at}"
        else:
            state = "unredeemed"
        note = f"  note={k.note!r}" if k.note else ""
        print(f"  {k.key_hash[:12]}…  created {k.created_at}  {state}{note}")
    return 0


def cmd_grant(args: argparse.Namespace) -> int:
    if _backend().set_api_access_by_username(args.username, True):
        print(f"Granted send access to {args.username!r}.")
        return 0
    print(f"No such user: {args.username!r}", file=sys.stderr)
    return 1


def cmd_revoke(args: argparse.Namespace) -> int:
    if _backend().set_api_access_by_username(args.username, False):
        print(f"Revoked send access from {args.username!r}.")
        return 0
    print(f"No such user: {args.username!r}", file=sys.stderr)
    return 1


def cmd_revoke_key(args: argparse.Namespace) -> int:
    if _backend().revoke_access_key(args.key):
        print("Key revoked; it can no longer be redeemed.")
        return 0
    print("Key not found or already redeemed (revoke the user instead).",
          file=sys.stderr)
    return 1


def cmd_revoke_all(args: argparse.Namespace) -> int:
    if not args.yes:
        reply = input("Revoke send access for ALL users? This is the billing "
                       "cutover action. Type 'yes' to confirm: ")
        if reply.strip().lower() != "yes":
            print("Aborted.")
            return 1
    n = _backend().revoke_all_access()
    print(f"Revoked send access for {n} user(s).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="access_keys.py",
        description="Manage Aime send-access and invite keys.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("gen", help="mint single-use invite keys")
    p_gen.add_argument("count", nargs="?", type=int, default=1,
                       help="how many keys to generate (default 1)")
    p_gen.add_argument("--note", default="",
                       help="optional label stored with each key")
    p_gen.set_defaults(func=cmd_gen)

    sub.add_parser("list", help="list users and invite keys").set_defaults(
        func=cmd_list)

    p_grant = sub.add_parser("grant", help="grant a user send access")
    p_grant.add_argument("username")
    p_grant.set_defaults(func=cmd_grant)

    p_revoke = sub.add_parser("revoke", help="revoke a user's send access")
    p_revoke.add_argument("username")
    p_revoke.set_defaults(func=cmd_revoke)

    p_rk = sub.add_parser("revoke-key", help="kill an unredeemed invite key")
    p_rk.add_argument("key")
    p_rk.set_defaults(func=cmd_revoke_key)

    p_ra = sub.add_parser("revoke-all",
                          help="revoke send access for every user")
    p_ra.add_argument("--yes", action="store_true",
                      help="skip the confirmation prompt")
    p_ra.set_defaults(func=cmd_revoke_all)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

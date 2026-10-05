"""Back-office administration from the terminal.

    uv run just create-owner --email owner@example.test --name "Dueña"     # asks twice
    printf '%s\\n' "$PASSWORD" | uv run just create-owner ... --password-stdin

The password is never printed.
"""

import argparse
import asyncio
import getpass
import sys

from fragancia_api.config import Settings
from fragancia_api.container import build_container
from fragancia_api.modules.identity.application.commands.create_user import CreateUser
from fragancia_api.modules.identity.domain.user import Role
from fragancia_api.shared.kernel import Err, Ok


def _read_password(from_stdin: bool) -> str | None:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    password = getpass.getpass("Password: ")
    if getpass.getpass("Repeat the password: ") != password:
        return None
    return password


async def _create_owner(email: str, name: str, password: str) -> int:
    container = build_container(Settings())  # values come from the environment
    try:
        result = await container.services.get(CreateUser).execute(email, name, password, Role.OWNER)
    finally:
        await container.close()
    match result:
        case Ok(_):
            print(f"✔ owner {email} created")
            return 0
        case Err(error):
            print(f"✘ {error.code}: {error.message}", file=sys.stderr)
            return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back-office administration")
    commands = parser.add_subparsers(dest="command", required=True)
    create_owner = commands.add_parser("create-owner", help="create a back-office owner account")
    create_owner.add_argument("--email", required=True)
    create_owner.add_argument("--name", required=True)
    create_owner.add_argument(
        "--password-stdin", action="store_true", help="read the password from standard input"
    )
    args = parser.parse_args(argv)

    password = _read_password(args.password_stdin)
    if password is None:
        print("✘ The passwords do not match", file=sys.stderr)
        return 1
    return asyncio.run(_create_owner(args.email, args.name, password))


if __name__ == "__main__":
    sys.exit(main())

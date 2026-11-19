"""Manage device accounts from the command line.

    python -m app.admin add quin-home --name Home --owner Quin   # prints token + password
    python -m app.admin list
    python -m app.admin passwd quin                # new password
    python -m app.admin new-token quin             # new token (reflash the ESP32)
    python -m app.admin remove quin

In Docker:  docker compose exec train-status python -m app.admin list
"""
import argparse
import sys

from . import accounts, storage


def fail(message):
    print("Error:", message, file=sys.stderr)
    sys.exit(1)


def main(argv=None):
    print(f"Accounts file: {storage.DATA_DIR / accounts.ACCOUNTS_FILE}", file=sys.stderr)
    parser = argparse.ArgumentParser(prog="python -m app.admin")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="create a device account")
    add.add_argument("device_id", help="short id: lowercase letters, digits, - or _")
    add.add_argument("--name", help="display name (default: the id)")
    add.add_argument("--owner", default="", help="who it belongs to (just a label)")
    add.add_argument("--password", help="default: a random one is generated")

    sub.add_parser("list", help="show all accounts")

    passwd = sub.add_parser("passwd", help="set a new password (logs that person out)")
    passwd.add_argument("device_id")
    passwd.add_argument("--password")

    token = sub.add_parser("new-token", help="replace the device token")
    token.add_argument("device_id")

    remove = sub.add_parser("remove", help="delete an account and its settings")
    remove.add_argument("device_id")

    args = parser.parse_args(argv)
    devices = accounts.load()

    if args.command == "list":
        if not devices:
            print("No accounts yet. Create one with: python -m app.admin add <id>")
        for device_id, acc in sorted(devices.items()):
            print(f"{device_id:20} {acc.get('name', ''):16} {acc.get('owner', ''):12} "
                  f"token={acc['token']}")
        return

    device_id = args.device_id
    if args.command == "add":
        if not accounts.ID_PATTERN.fullmatch(device_id):
            fail("id must be 1-32 chars: lowercase letters, digits, - or _")
        if device_id in devices:
            fail(f"'{device_id}' already exists")
        password = args.password or accounts.new_password()
        devices[device_id] = {
            "name": args.name or device_id,
            "owner": args.owner,
            "token": accounts.new_token(),
            "password_hash": accounts.hash_password(password),
        }
        accounts.save(devices)
        print(f"Created '{device_id}'")
        print(f"  web login:    {device_id} / {password}   (they can change the password)")
        print(f"  ESP32 token:  {devices[device_id]['token']}")
        return

    if device_id not in devices:
        fail(f"no account '{device_id}'")

    if args.command == "passwd":
        password = args.password or accounts.new_password()
        devices[device_id]["password_hash"] = accounts.hash_password(password)
        accounts.save(devices)
        print(f"New password for '{device_id}': {password}")
    elif args.command == "new-token":
        devices[device_id]["token"] = accounts.new_token()
        accounts.save(devices)
        print(f"New device token for '{device_id}': {devices[device_id]['token']}")
    elif args.command == "remove":
        del devices[device_id]
        accounts.save(devices)
        try:
            (storage.DATA_DIR / "devices" / f"{device_id}.json").unlink()
        except OSError:
            pass
        print(f"Removed '{device_id}'")


if __name__ == "__main__":
    main()

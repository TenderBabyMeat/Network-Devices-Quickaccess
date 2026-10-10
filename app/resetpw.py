"""Reset the login password from the command line:
    docker compose exec netdevices python -m app.resetpw
"""
import getpass
import sys

from . import auth, db


def main():
    db.init()
    pw = getpass.getpass("New password: ")
    if (m := auth.check_password_rules(pw)):
        sys.exit(m)
    if getpass.getpass("Repeat: ") != pw:
        sys.exit("Passwords don't match.")
    auth.set_password(pw)
    print("Password changed. All existing logins were signed out.")


if __name__ == "__main__":
    main()

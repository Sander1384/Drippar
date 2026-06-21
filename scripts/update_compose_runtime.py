import argparse
import os
import re
import tempfile
from pathlib import Path


def update_compose(path, image, session_secret):
    path = Path(path)
    if len(session_secret) < 32:
        raise RuntimeError("DRIPARR_NEW_SESSION_SECRET must contain at least 32 characters.")
    original = path.read_text(encoding="utf-8")
    updated, image_count = re.subn(
        r"(?m)^([ \t]*image:[ \t]*)ghcr\.io/sander1384/driparr:[^\s]+[ \t]*$",
        lambda match: f"{match.group(1)}{image}",
        original,
    )
    updated, secret_count = re.subn(
        r"(?m)^([ \t]*DRIPARR_SESSION_SECRET:[ \t]*).*$",
        lambda match: f'{match.group(1).rstrip()} "{session_secret}"',
        updated,
    )
    if image_count != 1 or secret_count != 1:
        raise RuntimeError(f"Expected one image and one session-secret entry; found {image_count} and {secret_count}.")

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(updated)
        temporary = Path(handle.name)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description="Pin the Driparr image and rotate its Compose session secret.")
    parser.add_argument("compose_file")
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    session_secret = os.environ.get("DRIPARR_NEW_SESSION_SECRET", "")
    update_compose(args.compose_file, args.image, session_secret)
    print("Compose runtime settings updated.")


if __name__ == "__main__":
    main()

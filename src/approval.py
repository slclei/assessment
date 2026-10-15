import os
import sys
import time
from typing import Optional


def read_line_with_deadline(prompt: str, timeout: float) -> Optional[str]:
    if timeout <= 0:
        return None
    if os.name == "nt":
        return _read_windows_console_line(prompt, timeout)
    return _read_selectable_stdin_line(prompt, timeout)


def _read_selectable_stdin_line(prompt: str, timeout: float) -> Optional[str]:
    import select

    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            sys.stdout.write("\nApproval timed out.\n")
            sys.stdout.flush()
            return None
        response = sys.stdin.readline()
    except (OSError, ValueError):
        return None
    return response.rstrip("\r\n") if response else None


def _read_windows_console_line(prompt: str, timeout: float) -> Optional[str]:
    import msvcrt

    deadline = time.monotonic() + timeout
    characters = []
    sys.stdout.write(prompt)
    sys.stdout.flush()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            sys.stdout.write("\nApproval timed out.\n")
            sys.stdout.flush()
            return None
        if not msvcrt.kbhit():
            time.sleep(min(remaining, 0.02))
            continue

        character = msvcrt.getwch()
        if character in ("\r", "\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()
            return "".join(characters)
        if character == "\003":
            raise KeyboardInterrupt
        if character == "\b":
            if characters:
                characters.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            continue
        if character in ("\x00", "\xe0"):
            msvcrt.getwch()
            continue
        characters.append(character)
        sys.stdout.write(character)
        sys.stdout.flush()
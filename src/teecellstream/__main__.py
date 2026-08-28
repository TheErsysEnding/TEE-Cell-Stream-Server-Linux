"""Entry point: `python3 -m teecellstream [--minimized] [--headless]`."""

import sys
import time


def main(argv: list[str]) -> int:
    if "--headless" in argv:
        # no window at all - for the integration test and for running under a plain terminal
        from . import log
        from .server import Server
        server = Server()
        if not server.start():
            print("Eine andere Kopie des Servers läuft bereits.", file=sys.stderr)
            return 1
        server.install_exit_hooks()
        print("TEE Cell Stream Server läuft (headless). Log: " + log.LOG_PATH, file=sys.stderr)
        try:
            while True:
                time.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            pass
        server.shutdown()
        return 0

    from .app import main as app_main
    return app_main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

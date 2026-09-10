"""TEE Cell Stream Server Linux - streams the desktop to a PS3 running the cell-stream homebrew app.

Linux port of cell-stream-server (ps3-dev, Apache-2.0, release 174-a5dd795).
"""

__version__ = "1.0.0"
# The DEBIAN version carries an epoch. Development ran to 1.38.0 before the first public release was
# cut at 1.0.0, and dpkg compares versions strictly: without the epoch apt sees 1.0.0 as older than
# what is installed and refuses to "upgrade" to it. "1:" is exactly the mechanism Debian provides for
# a version number that has been restarted, and it stays on every version from here on.
DEB_EPOCH = "1"
UPSTREAM_VERSION = "174-a5dd795"

APP_ID = "de.tee.CellStreamServer"
APP_NAME = "TEE Cell Stream Server"
APP_EXEC = "tee-cell-stream-server"

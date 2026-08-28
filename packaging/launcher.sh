#!/bin/sh
# TEE Cell Stream Server - launcher. Runs the packaged Python module with the system interpreter.
export PYTHONPATH="/usr/lib/tee-cell-stream-server${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m teecellstream "$@"

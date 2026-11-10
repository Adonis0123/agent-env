#!/bin/sh
# exec prefix of the demo remote-exec environment: reads an sh script on stdin and runs it on the server.
# Set VPS_HELPER_HOST to an ssh destination (a Host entry of ~/.ssh/config) before `doctor --env vps-helper`.
exec ssh "${VPS_HELPER_HOST:?set VPS_HELPER_HOST to the ssh destination of the server}" sh -s

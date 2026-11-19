#!/bin/sh
# Manage device accounts inside the running container.
#
#   ./admin.sh list
#   ./admin.sh add quin-home --name Home --owner Quin
#   ./admin.sh passwd quin-home
#   ./admin.sh new-token quin-home
#   ./admin.sh remove quin-home
#
# Works from any directory; uses the container's own data folder.
set -e
cd "$(dirname "$0")"

if [ $# -eq 0 ]; then
    set -- --help
fi

# -T only when not in a terminal (e.g. piped), so prompts still work normally.
if [ -t 0 ]; then
    exec docker compose exec train-status python -m app.admin "$@"
else
    exec docker compose exec -T train-status python -m app.admin "$@"
fi

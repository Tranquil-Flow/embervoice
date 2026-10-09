#!/bin/zsh
# Double-click to start Embervoice. Close this window to stop it.
set -eu
cd "${0:A:h}"
unset PYTHONPATH PYTHONHOME || true
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

if [[ ! -x .venv/bin/audiobook-studio ]]; then
  print 'Embervoice is not installed in this folder yet.'
  print 'Double-click "Install.command" first, then try again.'
  print ''
  read '?Press Return to close this window. '
  exit 1
fi

# The launcher owns the port, the browser and shutdown, so nothing else here
# may probe or hold a port.
exec .venv/bin/audiobook-studio "$@"
#!/bin/bash
# Double-click this file to install Embervoice.
# Finder gives a bare PATH, so the usual tool directories are added by hand.
set -uo pipefail
unset PYTHONPATH PYTHONHOME || true

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

SELF="$0"
case "$SELF" in
  */*) HERE="${SELF%/*}" ;;
  *) HERE="." ;;
esac
HERE="$(cd "$HERE" && pwd)" || exit 1

cd "$HERE" || {
  printf 'Could not enter the Embervoice folder.\n'
  printf "Press Return to close this window."; read -r _ || true
  exit 1
}

clear
printf 'Embervoice installer\n\n'

./install.sh
status=$?

if [ "$status" -ne 0 ]; then
  printf '\nThe installer stopped (exit %s). Partial app files or cached downloads may remain.\n' "$status"
  printf 'Fix the problem printed above, then double-click this file again.\n'
  printf 'If you need help, keep this window and copy the text above.\n'
else
  printf '\nInstall finished.\n'
fi

printf '\nPress Return to close this window. '
read -r _ || true
exit "$status"
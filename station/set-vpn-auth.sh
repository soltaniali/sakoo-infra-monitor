#!/bin/sh
# Writes secrets/cluster-auth.txt (VPN username + password) without the password appearing on screen or in history.
cd "$(dirname "$0")"
printf "VPN username [se-user15]: "; read -r u; u=${u:-se-user15}
stty -echo; printf "VPN password: "; read -r p; stty echo; echo
umask 077; printf '%s\n%s\n' "$u" "$p" > secrets/cluster-auth.txt && echo "written: secrets/cluster-auth.txt"

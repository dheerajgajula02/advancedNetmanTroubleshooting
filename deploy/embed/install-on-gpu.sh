#!/bin/sh
# Run this yourself, from boulder. It asks for indiaserver's password once.
#
# On indiaserver (dheeraj@10.9.8.190) it:
#   1. creates ~/ts-embed with the forwarder and its key
#   2. pulls the embedding model nomic-embed-text (about 270 MB)
#   3. adds one crontab line so the forwarder starts again after a reboot
#   4. starts the forwarder on 10.9.8.190:8791
# To undo it: crontab -e and delete the ts-embed line, pkill -f embed_proxy.py, rm -r ~/ts-embed
set -e
cd "$(dirname "$0")/../.."
KEY=$(grep '^EMBED_KEY=' secrets/embed.env | cut -d= -f2-)
PROXY=$(base64 -w0 deploy/embed/embed_proxy.py)
ssh dheeraj@10.9.8.190 sh -s <<REMOTE
set -e
umask 077
mkdir -p ~/ts-embed
echo $PROXY | base64 -d > ~/ts-embed/embed_proxy.py
printf '%s\n' '$KEY' > ~/ts-embed/key
ollama pull nomic-embed-text 2>&1 | tail -1
(crontab -l 2>/dev/null | grep -v ts-embed; echo '@reboot cd \$HOME/ts-embed && /usr/bin/python3 embed_proxy.py >> proxy.log 2>&1') | crontab -
pkill -f embed_proxy.py || true
cd ~/ts-embed
setsid nohup /usr/bin/python3 embed_proxy.py >> proxy.log 2>&1 < /dev/null &
sleep 2
ss -ltn | grep 8791 && echo "The embedding forwarder is running."
REMOTE

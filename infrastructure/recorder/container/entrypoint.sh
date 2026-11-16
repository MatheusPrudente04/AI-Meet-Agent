#!/usr/bin/env bash
# Sobe a tela virtual, o som virtual e o noVNC; depois roda o comando (worker do bot ou bot_setup).
set -euo pipefail

Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &
until xdpyinfo -display :99 >/dev/null 2>&1; do sleep 0.2; done

# Saída de som nula: sem ela a aba não tem onde tocar e a captura pode vir muda (ADR-016).
pulseaudio --start --exit-idle-time=-1 --log-target=stderr
pactl load-module module-null-sink sink_name=bot_sink >/dev/null
pactl set-default-sink bot_sink

# Tela do container pelo navegador do host, só para o login (porta publicada só em 127.0.0.1).
x11vnc -display :99 -forever -shared -nopw -localhost -quiet -bg -o /tmp/x11vnc.log
websockify --web /usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 &

exec "$@"

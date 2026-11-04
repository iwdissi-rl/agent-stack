#!/usr/bin/env bash
# Hang detector (la-watchdog.timer, every minute). systemd's Restart=always covers crashes; this
# covers processes that are alive but stop answering. A unit is restarted after 3 consecutive
# failed probes, never within its first 10 minutes (Flash-Next can take minutes to load cold).
STATE=/mnt/data/local-agent/logs/watchdog.state
touch "$STATE"
probe() {  # unit url
  local unit=$1 url=$2 since age fails
  systemctl --user is-active --quiet "$unit" || return 0   # stopped on purpose / restarting: not ours to judge
  since=$(systemctl --user show "$unit" -p ActiveEnterTimestampMonotonic --value)
  age=$(( ($(awk '{print int($1*1000000)}' /proc/uptime) - since) / 1000000 ))
  if curl -sf --noproxy '*' -m 10 "$url" >/dev/null; then
    sed -i "/^$unit /d" "$STATE"; return 0
  fi
  [ "$age" -lt 600 ] && return 0
  fails=$(( $(grep -oP "^$unit \K\d+" "$STATE" || echo 0) + 1 ))
  sed -i "/^$unit /d" "$STATE"; echo "$unit $fails" >> "$STATE"
  if [ "$fails" -ge 3 ]; then
    echo "$(date -Is) watchdog: $unit unresponsive ($url) for 3 checks, restarting" | tee -a /mnt/data/local-agent/logs/watchdog.log
    systemctl --user restart "$unit"; sed -i "/^$unit /d" "$STATE"
  fi
}
probe la-hud.service   http://127.0.0.1:8190/healthz
# ./agent.sh stop sets this flag: the owner stopped the stack on purpose, so only guard the HUD
[ -f /mnt/data/local-agent/logs/STOPPED ] && exit 0
probe la-model.service http://127.0.0.1:8181/health
probe la-embed.service http://127.0.0.1:8182/health
probe la-relay.service http://127.0.0.1:8190/healthz/relay
# containers have restart: unless-stopped; bring the stack back if someone stopped it or dockerd restarted
for svc in searxng crawl4ai qdrant; do
  if [ -z "$(cd /mnt/data/local-agent/services && docker compose ps -q --status running "$svc" 2>/dev/null)" ]; then
    echo "$(date -Is) watchdog: container $svc not running, starting" >> /mnt/data/local-agent/logs/watchdog.log
    (cd /mnt/data/local-agent/services && docker compose up -d "$svc" >/dev/null 2>&1)
  fi
done

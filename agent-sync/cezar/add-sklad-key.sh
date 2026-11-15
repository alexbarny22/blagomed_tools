#!/usr/bin/env bash
# Привязка складского ключа SKLAD к пользователю cezar на VPS.
#
# Правила:
#   * меняется ТОЛЬКО <home cezar>/.ssh/authorized_keys;
#   * добавляется РОВНО ОДНА строка — публичный ключ склада;
#   * существующие ключи не читаются в открытом виде, не изменяются и не удаляются;
#   * повторный запуск ничего не дублирует (идемпотентно);
#   * /root/.ssh, sudoers, sshd_config, firewall не затрагиваются.
#
# Использование:
#   sudo bash add-sklad-key.sh            # применить
#   bash add-sklad-key.sh --dry-run       # только показать, что будет сделано (без sudo)
set -euo pipefail

KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAD2oggZMlWXeNJTSUv8u0VaYWBZR0XivlShwXFKUrwW SSSR@Windows-ai-agent-vps-2026'
FPR='SHA256:WZh8c6cHWN4VGu5TthgbnjwEvxKidW7cw7U7nRlKnWA'
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

id cezar >/dev/null 2>&1 || { echo "ОШИБКА: пользователь cezar не найден" >&2; exit 1; }
home=$(getent passwd cezar | cut -d: -f6)
[ -n "$home" ] && [ -d "$home" ] || { echo "ОШИБКА: нет домашнего каталога cezar" >&2; exit 1; }
group=$(id -gn cezar)
ak="$home/.ssh/authorized_keys"

body=$(printf '%s' "$KEY" | awk '{print $2}')

if [ -f "$ak" ]; then
  before=$(grep -cve '^[[:space:]]*$' "$ak" || true)
else
  before=0
fi

if [ -f "$ak" ] && awk -v b="$body" '{ for (i = 1; i <= NF; i++) if ($i == b) found = 1 } END { exit(found ? 0 : 1) }' "$ak"; then
  echo "ключ SKLAD уже присутствует — изменений не требуется (ключей: $before)"
  added=0
else
  if [ "$DRY" -eq 1 ]; then
    echo "DRY-RUN: будет добавлена 1 строка в $ak:"
    echo "  $KEY"
    echo "DRY-RUN: ключей сейчас: $before, станет: $((before + 1))"
    exit 0
  fi
  [ "$(id -u)" -eq 0 ] || { echo "ОШИБКА: нужен root/sudo (или запустите с --dry-run)" >&2; exit 1; }
  install -d -m 700 -o cezar -g "$group" "$home/.ssh"
  [ -f "$ak" ] || : > "$ak"
  printf '%s\n' "$KEY" >> "$ak"
  chown cezar:"$group" "$ak"
  chmod 600 "$ak"
  added=1
  echo "ключ SKLAD добавлен"
fi

# Контроль: ровно одна строка добавлена, остальные ключи на месте.
after=$(grep -cve '^[[:space:]]*$' "$ak" || true)
delta=$((after - before))
if [ "$added" -eq 1 ] && [ "$delta" -ne 1 ]; then
  echo "ОШИБКА: ожидалось +1 ключ, получено $delta" >&2
  exit 2
fi
if [ "$added" -eq 0 ] && [ "$delta" -ne 0 ]; then
  echo "ОШИБКА: файл изменился неожидаемо ($delta)" >&2
  exit 2
fi

echo "--- контроль ---"
echo "ключей было: $before, стало: $after, добавлено: $delta"
echo "всего ключей у cezar: $after (остальные не тронуты)"
ssh-keygen -lf "$ak" | awk -v f="$FPR" '{ print "  " $2 " " ($2 == f ? "<- SKLAD" : "") }'
grep -qF "$FPR" <(ssh-keygen -lf "$ak") && echo "OK: складской ключ на месте ($FPR)"

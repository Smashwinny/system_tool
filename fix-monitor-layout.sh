#!/usr/bin/env bash
set -u

export DISPLAY="${DISPLAY:-:1}"
export XAUTHORITY="${XAUTHORITY:-/run/user/$(id -u)/gdm/Xauthority}"

screen_lock_state() {
  local output session locked found_unlocked=0

  if command -v gdbus >/dev/null 2>&1; then
    if output="$(gdbus call --session \
        --dest org.gnome.ScreenSaver \
        --object-path /org/gnome/ScreenSaver \
        --method org.gnome.ScreenSaver.GetActive 2>/dev/null)"; then
      case "$output" in
        *true*) printf '%s\n' locked; return 0 ;;
        *false*) printf '%s\n' unlocked; return 0 ;;
      esac
    fi
  fi

  if command -v loginctl >/dev/null 2>&1; then
    while read -r session _; do
      [ -n "$session" ] || continue
      locked="$(loginctl show-session "$session" --property=LockedHint --value 2>/dev/null)" || continue
      case "$locked" in
        yes) printf '%s\n' locked; return 0 ;;
        no) found_unlocked=1 ;;
      esac
    done < <(loginctl list-sessions --no-legend 2>/dev/null | awk -v uid="$(id -u)" '$2 == uid {print $1}')
  fi

  if [ "$found_unlocked" -eq 1 ]; then
    printf '%s\n' unlocked
  else
    printf '%s\n' unknown
  fi
}

lock_state="$(screen_lock_state)"
if [ "$lock_state" != unlocked ]; then
  command -v logger >/dev/null 2>&1 && \
    logger --tag system-tool-display "skip xrandr: screen lock state is $lock_state"
  exit 0
fi

layout="$(xrandr --query 2>/dev/null)" || exit 0

if printf '%s\n' "$layout" | grep -q '^HDMI-1-0 connected 2560x1440+0+160'; then
  xrandr \
    --output HDMI-1-0 --mode 2560x1440 --rate 59.95 --pos 0x0 \
    --output eDP-1 --mode 1920x1200 --rate 60 --pos 2560x0 --primary
fi

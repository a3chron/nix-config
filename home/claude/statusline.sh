#!/usr/bin/env bash
# Claude Code status line — styled after the stellar theme a3chron/ctp-blue@1.0
# (Catppuccin Mocha palette, ~/.config/starship.toml)
#
# ╌╌──╌ dir ╌──╌ branch ✚ ╌──╌ model ╌╌ effort ╌──╌ ctx% ╌╌ 5h% ╌╌ 7d% ↻3d ╌───…───╌ title ╌──╌ version ╌
#
# Every value is bracketed by ╌ , the rule between them is ─ , related values abut with ╌╌ .
# Too narrow? Low-priority segments drop out rather than wrapping.

input=$(cat)

# Columns the TUI keeps for itself; raise if the right edge is still clipped.
reserve=4

# ${#str} must count characters, not bytes, for the right-alignment to land
case "${LC_ALL:-$LANG}" in *UTF-8* | *utf8*) ;; *) export LC_ALL=C.UTF-8 ;; esac

# --- Catppuccin Mocha ----------------------------------------------------
r=$'\033[0m'; b=$'\033[1m'; d=$'\033[2m'
fg() { printf '\033[38;2;%d;%d;%dm' "$1" "$2" "$3"; }
mauve=$(fg 203 166 247)
pink=$(fg 245 194 231)
teal=$(fg 148 226 213)
sapphire=$(fg 116 199 236)
lavender=$(fg 180 190 254)
blue=$(fg 137 180 250)
peach=$(fg 250 179 135)
red=$(fg 243 139 168)
overlay0=$(fg 108 112 134)

j() { printf '%s' "$input" | jq -r "$1 // empty" 2>/dev/null; }
# Percentages arrive as raw floats (7.000000000000001), which both render ugly
# and break the [ x -ge y ] threshold tests below — those need integers, and a
# float makes them silently false, so the peach/red warnings never fire.
jn() { printf '%s' "$input" | jq -r "$1 // empty | round" 2>/dev/null; }
rep() { local n=$1 s=''; while [ "$n" -gt 0 ]; do s+=$2; n=$((n - 1)); done; printf '%s' "$s"; }

# Left-hand segments; dropped by priority (low first) when space runs out.
# tight=1 joins straight onto the previous value (╌╌) instead of a ── run.
sc=() sp=() spri=() stight=()
push() { sc+=("$1"); sp+=("$2"); spri+=("$3"); stight+=("${4:-0}"); }

# --- directory (starship truncation_length = 5) --------------------------
cwd=$(j '.workspace.current_dir // .cwd'); [ -n "$cwd" ] || cwd=$PWD
dir=${cwd/#$HOME/\~}
IFS='/' read -ra parts <<< "$dir"
if [ ${#parts[@]} -gt 5 ]; then
  dir="…/$(printf '%s/' "${parts[@]: -5}")"; dir=${dir%/}
fi
push "${mauve}${b}${dir}${r}" "$dir" 9

# --- git branch + dirty state --------------------------------------------
if branch=$(git --no-optional-locks -C "$cwd" symbolic-ref --quiet --short HEAD 2>/dev/null) \
   || branch=$(git --no-optional-locks -C "$cwd" describe --tags --exact-match HEAD 2>/dev/null) \
   || branch=$(git --no-optional-locks -C "$cwd" rev-parse --short HEAD 2>/dev/null); then
  # ◆ filled = dirty tree, ◇ hollow = clean. Geometric rather than dingbats so
  # they sit with the box-drawing rule, and the fill carries the state on its
  # own — colour alone would be lost to a colourblind reader or a plain log.
  if [ -n "$(git --no-optional-locks -C "$cwd" status --porcelain 2>/dev/null | head -1)" ]; then
    mark='◆'
  else
    mark='◇'
  fi

  # Commits on HEAD that aren't on its upstream yet. A clean tree says nothing
  # about whether the work has left the machine, which is the more useful thing
  # to know. Stays silent when there's no upstream at all (fresh branch,
  # detached HEAD) rather than implying a synced zero.
  ahead=$(git --no-optional-locks -C "$cwd" rev-list --count '@{upstream}..HEAD' 2>/dev/null)
  if [ -n "$ahead" ] && [ "$ahead" -gt 0 ] 2>/dev/null; then
    mark+=" ↑${ahead}"
  fi

  # Same shape as the model segment: one hue for the whole element, bold for the
  # value, dim for the trailing detail — no reset in between, so the marks read
  # as part of the branch rather than as their own segment.
  push "${pink}${b}${branch} ${d}${mark}${r}" "$branch $mark" 8
fi

# --- model ---------------------------------------------------------------
model=$(j '.model.display_name') modelp=$(j '.model.display_name')
model=${model/ (1M context)/ ${d}1M}      # "Opus 5 (1M context)" -> "Opus 5 1M"
modelp=${modelp/ (1M context)/ 1M}
if [ "$(j '.fast_mode')" = 'true' ]; then
  push "${lavender}${b}${model}${r} ${peach}${b}⚡${r}" "$modelp ⚡" 7
else
  push "${lavender}${b}${model}${r}" "$modelp" 7
fi

# --- effort level (dimmed, tucked against the model) ---------------------
effort=$(j '.effort.level')
[ -n "$effort" ] && push "${overlay0}${d}${effort}${r}" "$effort" 2 1

# --- context window left -------------------------------------------------
ctx=$(jn '.context_window.remaining_percentage')
if [ -n "$ctx" ]; then
  ctxcol=$blue
  [ "$ctx" -le 25 ] 2>/dev/null && ctxcol=$peach
  [ "$ctx" -le 10 ] 2>/dev/null && ctxcol=$red
  push "${overlay0}ctx${r} ${ctxcol}${b}${ctx}%${r}" "ctx ${ctx}%" 4
fi

# --- rate limits: 5h block, 7d window + reset countdown ------------------
usage_col() {
  if   [ "$1" -ge 85 ] 2>/dev/null; then printf '%s' "$red"
  elif [ "$1" -ge 60 ] 2>/dev/null; then printf '%s' "$peach"
  else printf '%s' "$teal"; fi
}
h5=$(jn '.rate_limits.five_hour.used_percentage')
[ -n "$h5" ] && push "${overlay0}5h${r} $(usage_col "$h5")${b}${h5}%${r}" "5h ${h5}%" 5 1

d7=$(jn '.rate_limits.seven_day.used_percentage')
if [ -n "$d7" ]; then
  resets=$(j '.rate_limits.seven_day.resets_at') left=''
  if [ -n "$resets" ]; then
    secs=$((resets - $(date +%s)))
    if   [ $secs -le 0 ];     then left=''
    elif [ $secs -lt 86400 ]; then left="↻$(((secs + 3599) / 3600))h"
    else                           left="↻$(((secs + 86399) / 86400))d"
    fi
  fi
  if [ -n "$left" ]; then
    push "${overlay0}7d${r} $(usage_col "$d7")${b}${d7}%${r} ${overlay0}${d}${left}${r}" "7d ${d7}% $left" 6 1
  else
    push "${overlay0}7d${r} $(usage_col "$d7")${b}${d7}%${r}" "7d ${d7}%" 6 1
  fi
fi

# --- right-hand tail: session title, then version ------------------------
title=$(j '.session_name')
[ ${#title} -gt 32 ] && title="${title:0:31}…"
ver=$(j '.version')

cols=${COLUMNS:-$(tput cols 2>/dev/null)}; cols=${cols:-100}
budget=$((cols - reserve))

# --- fit: drop title, then lowest-priority segments, then version --------
alive=(); for i in "${!sp[@]}"; do alive[i]=1; done
width() {
  local w=4 i first=1                       # 4 = the "╌╌──" head
  for i in "${!sp[@]}"; do
    [ "${alive[i]}" = 1 ] || continue
    w=$((w + ${#sp[i]} + 4))
    [ $first -eq 1 ] && first=0 || [ "${stight[i]}" = 1 ] || w=$((w + 2))
  done
  [ -n "$title" ] && w=$((w + ${#title} + 6))
  [ -n "$ver" ]   && w=$((w + ${#ver} + 6))
  printf '%s' "$w"
}
while [ "$(width)" -gt $budget ]; do
  if [ -n "$title" ]; then title=''; continue; fi
  victim=-1 lowest=99
  for i in "${!sp[@]}"; do
    [ "${alive[i]}" = 1 ] || continue
    [ "${spri[i]}" -lt $lowest ] && { lowest=${spri[i]}; victim=$i; }
  done
  if [ $victim -ge 0 ]; then alive[victim]=0
  elif [ -n "$ver" ]; then ver=''
  else break; fi
done

# --- assemble ------------------------------------------------------------
out="${overlay0}╌╌──${r}" len=4 first=1
for i in "${!sp[@]}"; do
  [ "${alive[i]}" = 1 ] || continue
  if [ $first -eq 1 ]; then first=0
  elif [ "${stight[i]}" != 1 ]; then out+="${overlay0}──${r}"; len=$((len + 2))
  fi
  out+="${overlay0}╌${r} ${sc[i]} ${overlay0}╌${r}"
  len=$((len + ${#sp[i]} + 4))
done

tail_len=0
[ -n "$title" ] && tail_len=$((tail_len + ${#title} + 6))
[ -n "$ver" ]   && tail_len=$((tail_len + ${#ver} + 6))
gap=$((budget - len - tail_len))
[ $gap -gt 0 ] && out+="${overlay0}$(rep $gap '─')${r}"

[ -n "$title" ] && out+="${overlay0}──╌${r} ${sapphire}${d}${title}${r} ${overlay0}╌${r}"
[ -n "$ver" ]   && out+="${overlay0}──╌${r} ${overlay0}${d}${ver}${r} ${overlay0}╌${r}"

printf '%s\n' "$out"

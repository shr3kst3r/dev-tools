#!/usr/bin/env bash
#
# statusline.sh - Advanced Claude Code status line
#
# Displays: directory, git info, model, tokens, cost, usage limits
# Receives JSON on stdin from Claude Code.
#
# Install: `spg install` links this file to ~/.claude/statusline.sh; point
#          ~/.claude/settings.json's statusLine.command at that path (README).
# Test:    echo '{}' | ./statusline/statusline.sh
#          just test tests/test_statusline.py
#
# Requires jq. Renders on every turn, so it must stay fast and must never fail
# hard: every lookup falls back to a default rather than exiting non-zero.
#

set -euo pipefail

# -- Colors (ANSI) ----------------------------------------------------------
readonly C_RESET='\033[0m'
readonly C_BOLD='\033[1m'
readonly C_DIM='\033[2m'
readonly C_DIR='\033[38;5;75m'        # light blue
readonly C_BRANCH='\033[38;5;114m'    # green
readonly C_DIRTY='\033[38;5;214m'     # orange
readonly C_CLEAN='\033[38;5;114m'     # green
readonly C_AHEAD='\033[38;5;81m'      # cyan
readonly C_BEHIND='\033[38;5;203m'    # red
readonly C_MODEL='\033[38;5;183m'     # lavender
readonly C_TOKENS='\033[38;5;223m'    # warm yellow
readonly C_COST='\033[38;5;157m'      # light green
readonly C_COST_MED='\033[38;5;214m'  # orange
readonly C_COST_HIGH='\033[38;5;203m' # red
readonly C_CTX='\033[38;5;117m'       # sky blue
readonly C_CTX_WARN='\033[38;5;214m'  # orange
readonly C_CTX_CRIT='\033[38;5;203m'  # red
readonly C_SEP='\033[38;5;240m'       # dark gray

# -- Read JSON from stdin ----------------------------------------------------
INPUT=$(cat)

# A payload jq can't parse must not take the status bar down with it: under
# `set -euo pipefail` a failing `jq` inside a command substitution aborts the
# whole script. Fall back to an empty object so every lookup below returns its
# documented default instead. (Empty stdin lands here too, harmlessly.)
if ! printf '%s' "$INPUT" | jq -e . >/dev/null 2>&1; then
    INPUT='{}'
fi

# Helper: extract a value from the JSON input
jval() {
    echo "$INPUT" | jq -r "$1 // empty" 2>/dev/null
}

jval_num() {
    echo "$INPUT" | jq -r "$1 // 0" 2>/dev/null
}

# -- Separator ---------------------------------------------------------------
SEP="${C_SEP}│${C_RESET}"

# -- Directory ---------------------------------------------------------------
raw_dir=$(jval '.cwd')
if [[ -z "$raw_dir" ]]; then
    raw_dir=$(jval '.workspace.current_dir')
fi
if [[ -n "$raw_dir" ]]; then
    display_dir="${raw_dir/#$HOME/~}"
else
    display_dir="~"
fi

# -- Git info ----------------------------------------------------------------
git_info=""
if [[ -n "$raw_dir" ]] && command -v git &>/dev/null; then
    if git -C "$raw_dir" rev-parse --is-inside-work-tree &>/dev/null 2>&1; then
        branch=$(git -C "$raw_dir" symbolic-ref --short HEAD 2>/dev/null || git -C "$raw_dir" rev-parse --short HEAD 2>/dev/null || echo "")

        if [[ -n "$branch" ]]; then
            # Working directory status flags
            flags=""
            if [[ -n $(git -C "$raw_dir" diff --name-only 2>/dev/null) ]]; then
                flags+="${C_DIRTY}!${C_RESET}"
            fi
            if [[ -n $(git -C "$raw_dir" ls-files --others --exclude-standard 2>/dev/null | head -1) ]]; then
                flags+="${C_DIRTY}?${C_RESET}"
            fi
            if [[ -n $(git -C "$raw_dir" diff --cached --name-only 2>/dev/null) ]]; then
                flags+="${C_CLEAN}+${C_RESET}"
            fi

            # Ahead/behind upstream
            upstream=""
            if git -C "$raw_dir" rev-parse --abbrev-ref '@{upstream}' &>/dev/null 2>&1; then
                ahead=$(git -C "$raw_dir" rev-list --count '@{upstream}..HEAD' 2>/dev/null || echo 0)
                behind=$(git -C "$raw_dir" rev-list --count 'HEAD..@{upstream}' 2>/dev/null || echo 0)
                if [[ "$ahead" -gt 0 ]]; then
                    upstream+="${C_AHEAD}↑${ahead}${C_RESET}"
                fi
                if [[ "$behind" -gt 0 ]]; then
                    upstream+="${C_BEHIND}↓${behind}${C_RESET}"
                fi
            fi

            git_info="${C_BRANCH}${branch}${C_RESET}"
            [[ -n "$flags" ]] && git_info+=" ${flags}"
            [[ -n "$upstream" ]] && git_info+=" ${upstream}"
        fi
    fi
fi

# -- Model -------------------------------------------------------------------
model_name=$(jval '.model.display_name')
if [[ -z "$model_name" ]]; then
    model_id=$(jval '.model.id')
    case "$model_id" in
        *opus*)   model_name="Opus" ;;
        *sonnet*) model_name="Sonnet" ;;
        *haiku*)  model_name="Haiku" ;;
        *)        model_name="${model_id:-unknown}" ;;
    esac
fi

# -- Tokens ------------------------------------------------------------------
format_num() {
    printf "%'d" "$1" 2>/dev/null || echo "$1"
}

# Format token counts with K/M suffixes
format_tokens_short() {
    local n=$1
    if [[ "$n" -ge 1000000 ]]; then
        echo "$n" | awk '{printf "%.1fM", $1/1000000}'
    elif [[ "$n" -ge 1000 ]]; then
        echo "$n" | awk '{printf "%.1fK", $1/1000}'
    else
        echo "$n"
    fi
}

input_tokens=$(jval_num '.context_window.total_input_tokens')
output_tokens=$(jval_num '.context_window.total_output_tokens')
total_tokens=$(( input_tokens + output_tokens ))
tokens_display=$(format_num "$total_tokens")

# -- Context usage -----------------------------------------------------------
ctx_pct=$(jval_num '.context_window.used_percentage' | cut -d. -f1)
if [[ "$ctx_pct" -ge 80 ]]; then
    ctx_color="$C_CTX_CRIT"
elif [[ "$ctx_pct" -ge 60 ]]; then
    ctx_color="$C_CTX_WARN"
else
    ctx_color="$C_CTX"
fi

# -- Context window details --------------------------------------------------
ctx_window_size=$(jval_num '.context_window.context_window_size')
if [[ "$ctx_window_size" -ge 1000000 ]]; then
    ctx_size_display="1M"
elif [[ "$ctx_window_size" -ge 1000 ]]; then
    ctx_size_display="$(( ctx_window_size / 1000 ))K"
else
    ctx_size_display="$ctx_window_size"
fi

cur_input=$(jval_num '.context_window.current_usage.input_tokens')
cur_output=$(jval_num '.context_window.current_usage.output_tokens')
cache_create=$(jval_num '.context_window.current_usage.cache_creation_input_tokens')
cache_read=$(jval_num '.context_window.current_usage.cache_read_input_tokens')

cur_input_display=$(format_tokens_short "$cur_input")
cur_output_display=$(format_tokens_short "$cur_output")
cache_create_display=$(format_tokens_short "$cache_create")
cache_read_display=$(format_tokens_short "$cache_read")

ctx_used_tokens=$(( cur_input + cache_create + cache_read ))
ctx_remaining=$(( ctx_window_size - ctx_used_tokens ))
if [[ "$ctx_remaining" -lt 0 ]]; then ctx_remaining=0; fi
ctx_remaining_display=$(format_tokens_short "$ctx_remaining")
ctx_used_display=$(format_tokens_short "$ctx_used_tokens")
ctx_total_display=$(format_tokens_short "$ctx_window_size")

# -- Context grid (2x10 = 20 cells, each = 5%) ------------------------------
total_cells=20

# Split used cells between input and output proportionally
input_with_cache=$(( cur_input + cache_create + cache_read ))
total_in_ctx=$(( input_with_cache + cur_output ))

if [[ "$total_in_ctx" -gt 0 && "$ctx_window_size" -gt 0 ]]; then
    input_cells=$(( input_with_cache * total_cells / ctx_window_size ))
    output_cells=$(( cur_output * total_cells / ctx_window_size ))
    # Ensure at least 1 cell if tokens > 0
    if [[ "$input_with_cache" -gt 0 && "$input_cells" -eq 0 ]]; then input_cells=1; fi
    if [[ "$cur_output" -gt 0 && "$output_cells" -eq 0 ]]; then output_cells=1; fi
    used_cells=$(( input_cells + output_cells ))
    if [[ "$used_cells" -gt "$total_cells" ]]; then
        output_cells=$(( total_cells - input_cells ))
        if [[ "$output_cells" -lt 0 ]]; then output_cells=0; input_cells=$total_cells; fi
    fi
else
    input_cells=0
    output_cells=0
fi
free_cells=$(( total_cells - input_cells - output_cells ))

# Build colored grid cells
readonly C_OUT='\033[38;5;183m'      # lavender for output
ctx_grid=()
for ((i=0; i<input_cells; i++)); do ctx_grid+=("${ctx_color}⛁${C_RESET}"); done
for ((i=0; i<output_cells; i++)); do ctx_grid+=("${C_OUT}⛀${C_RESET}"); done
for ((i=0; i<free_cells; i++)); do ctx_grid+=("${C_DIM}⛶${C_RESET}"); done

# Build 2 rows of 10
ctx_row1=""
for ((i=0; i<10; i++)); do
    [[ $i -gt 0 ]] && ctx_row1+=" "
    ctx_row1+="${ctx_grid[$i]:-}"
done
ctx_row2=""
for ((i=10; i<20; i++)); do
    [[ $i -gt 10 ]] && ctx_row2+=" "
    ctx_row2+="${ctx_grid[$i]:-}"
done

# -- Plugins & MCPs ---------------------------------------------------------
readonly CLAUDE_SETTINGS="${HOME}/.claude/settings.json"
readonly CLAUDE_CONFIG="${HOME}/.claude.json"
readonly PLUGIN_CACHE="${HOME}/.claude/plugins/cache/claude-plugins-official"

# Enabled plugins
plugins=()
if [[ -f "$CLAUDE_SETTINGS" ]]; then
    while IFS= read -r p; do
        plugins+=("$p")
    done < <(jq -r '.enabledPlugins // {} | keys[] | sub("@.*$"; "")' "$CLAUDE_SETTINGS" 2>/dev/null)
fi
plugin_count=${#plugins[@]}

# MCP servers from plugin cache
mcp_names=()
if [[ -d "$PLUGIN_CACHE" ]]; then
    for pdir in "$PLUGIN_CACHE"/*/; do
        pname=$(basename "$pdir")
        if ls "$pdir"*/.mcp.json &>/dev/null 2>&1; then
            mcp_names+=("$pname")
        fi
    done
fi

# Check for managed MCPs (like Notion) from settings
if [[ -f "$CLAUDE_SETTINGS" ]]; then
    while IFS= read -r m; do
        mcp_names+=("$m")
    done < <(jq -r '.mcpServers // {} | keys[]' "$CLAUDE_SETTINGS" 2>/dev/null)
fi
mcp_count=${#mcp_names[@]}

# Custom skills (project + global commands)
skill_count=0
if [[ -n "$raw_dir" && -d "${raw_dir}/.claude/commands" ]]; then
    skill_count=$(find "${raw_dir}/.claude/commands" -name '*.md' 2>/dev/null | wc -l | tr -d ' ')
fi
global_skills=0
if [[ -d "${HOME}/.claude/commands" ]]; then
    global_skills=$(find "${HOME}/.claude/commands" -name '*.md' 2>/dev/null | wc -l | tr -d ' ')
fi
skill_count=$(( skill_count + global_skills ))

# -- Cost --------------------------------------------------------------------
cost_usd=$(jval_num '.cost.total_cost_usd')
# Format cost to 4 decimal places
cost_display=$(printf '$%.4f' "$cost_usd" 2>/dev/null || echo '$0.0000')

# Color-code by cost threshold
cost_cents=$(echo "$cost_usd" | awk '{printf "%d", $1 * 100}')
if [[ "$cost_cents" -ge 100 ]]; then
    cost_color="$C_COST_HIGH"
elif [[ "$cost_cents" -ge 25 ]]; then
    cost_color="$C_COST_MED"
else
    cost_color="$C_COST"
fi

# -- Duration ----------------------------------------------------------------
duration_ms=$(jval_num '.cost.total_duration_ms')
duration_sec=$(( duration_ms / 1000 ))
if [[ "$duration_sec" -ge 3600 ]]; then
    dur_h=$(( duration_sec / 3600 ))
    dur_m=$(( (duration_sec % 3600) / 60 ))
    duration_display="${dur_h}h${dur_m}m"
elif [[ "$duration_sec" -ge 60 ]]; then
    dur_m=$(( duration_sec / 60 ))
    dur_s=$(( duration_sec % 60 ))
    duration_display="${dur_m}m${dur_s}s"
else
    duration_display="${duration_sec}s"
fi

# -- Lines changed -----------------------------------------------------------
lines_added=$(jval_num '.cost.total_lines_added')
lines_removed=$(jval_num '.cost.total_lines_removed')
lines_info=""
if [[ "$lines_added" -gt 0 || "$lines_removed" -gt 0 ]]; then
    lines_info="${C_CLEAN}+${lines_added}${C_RESET} ${C_BEHIND}-${lines_removed}${C_RESET}"
fi

# -- Assemble line 1: directory + git ----------------------------------------
line1="${C_DIR}${C_BOLD}${display_dir}${C_RESET}"
if [[ -n "$git_info" ]]; then
    line1+=" ${SEP} ${git_info}"
fi

# -- Assemble line 2: model + tokens + cost + duration -----------------------
line2="${C_MODEL}${model_name}${C_RESET}"
line2+=" ${SEP} ${C_TOKENS}${tokens_display} tok${C_RESET}"
line2+=" ${SEP} ${cost_color}${cost_display}${C_RESET}"
line2+=" ${SEP} ${C_DIM}${duration_display}${C_RESET}"
if [[ -n "$lines_info" ]]; then
    line2+=" ${SEP} ${lines_info}"
fi

# -- Assemble context lines (grid + info) ------------------------------------
model_id=$(jval '.model.id')
ctx_line1="${ctx_row1}   ${C_BOLD}${model_id:-unknown}${C_RESET} ${C_DIM}·${C_RESET} ${ctx_color}${ctx_used_display}/${ctx_total_display}${C_RESET} tokens (${ctx_color}${ctx_pct}%${C_RESET})"
ctx_line2="${ctx_row2}   ${ctx_color}⛁${C_RESET} in: ${cur_input_display}"
if [[ "$cache_read" -gt 0 || "$cache_create" -gt 0 ]]; then
    ctx_line2+=" (cache ↩${cache_read_display} ↗${cache_create_display})"
fi
ctx_line2+="  ${C_OUT}⛀${C_RESET} out: ${cur_output_display}"
ctx_line2+="  ${C_DIM}⛶${C_RESET} free: ${ctx_remaining_display}"

# -- Assemble plugins/MCPs line ---------------------------------------------
info_line=""
if [[ "$plugin_count" -gt 0 ]]; then
    plugin_list=$(IFS=,; echo "${plugins[*]}" | sed 's/,/, /g')
    info_line+="${C_CLEAN}plugins(${plugin_count}):${C_RESET} ${C_DIM}${plugin_list}${C_RESET}"
fi
if [[ "$mcp_count" -gt 0 ]]; then
    mcp_list=$(IFS=,; echo "${mcp_names[*]}" | sed 's/,/, /g')
    [[ -n "$info_line" ]] && info_line+=" ${SEP} "
    info_line+="${C_AHEAD}mcps(${mcp_count}):${C_RESET} ${C_DIM}${mcp_list}${C_RESET}"
fi
if [[ "$skill_count" -gt 0 ]]; then
    [[ -n "$info_line" ]] && info_line+=" ${SEP} "
    info_line+="${C_TOKENS}skills: ${skill_count}${C_RESET}"
fi

# -- Usage limits (5h + weekly) ----------------------------------------------
readonly USAGE_CACHE="${HOME}/.claude/.statusline-usage-cache.json"
readonly USAGE_CACHE_TTL=300  # 5 minutes

# Color for usage percentage
usage_color() {
    local pct=$1
    if [[ "$pct" -ge 80 ]]; then
        echo "$C_CTX_CRIT"
    elif [[ "$pct" -ge 50 ]]; then
        echo "$C_CTX_WARN"
    else
        echo "$C_CLEAN"
    fi
}

# Build a compact usage bar: [██████░░░░] 60%
usage_bar() {
    local pct=$1
    local width=10
    local filled=$(( pct * width / 100 ))
    if [[ "$pct" -gt 0 && "$filled" -eq 0 ]]; then filled=1; fi
    if [[ "$filled" -gt "$width" ]]; then filled=$width; fi
    local empty=$(( width - filled ))
    local color
    color=$(usage_color "$pct")
    local bar="${color}"
    for ((i=0; i<filled; i++)); do bar+="█"; done
    bar+="${C_DIM}"
    for ((i=0; i<empty; i++)); do bar+="░"; done
    bar+="${C_RESET}"
    echo "${bar}"
}

# Format a reset timestamp to a countdown string.
# Accepts Unix epoch seconds (what Claude Code's statusline JSON provides) or
# an ISO 8601 string (what the OAuth usage endpoint provides).
format_countdown() {
    local resets_at=$1
    local now_epoch reset_epoch diff_sec

    # Already epoch seconds? Use directly — no date(1) parsing needed.
    if [[ "$resets_at" =~ ^[0-9]+$ ]]; then
        reset_epoch=$resets_at
        now_epoch=$(date +%s)
    # Parse the reset timestamp to epoch seconds
    elif command -v gdate &>/dev/null; then
        reset_epoch=$(gdate -d "$resets_at" +%s 2>/dev/null || echo 0)
        now_epoch=$(gdate +%s)
    elif date -d "" &>/dev/null 2>&1; then
        reset_epoch=$(date -d "$resets_at" +%s 2>/dev/null || echo 0)
        now_epoch=$(date +%s)
    else
        # macOS date: try converting ISO 8601
        # Strip timezone offset for macOS date compatibility
        local clean_ts
        clean_ts=$(echo "$resets_at" | sed -E 's/\.[0-9]+//; s/([+-][0-9]{2}):([0-9]{2})$/\1\2/; s/Z$/+0000/')
        reset_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%S%z" "$clean_ts" +%s 2>/dev/null || echo 0)
        now_epoch=$(date +%s)
    fi

    if [[ "$reset_epoch" -eq 0 ]]; then
        echo "??:??"
        return
    fi

    diff_sec=$(( reset_epoch - now_epoch ))
    if [[ "$diff_sec" -le 0 ]]; then
        echo "now"
        return
    fi

    local days=$(( diff_sec / 86400 ))
    local hours=$(( (diff_sec % 86400) / 3600 ))
    local mins=$(( (diff_sec % 3600) / 60 ))

    if [[ "$days" -gt 0 ]]; then
        echo "${days}d${hours}h"
    elif [[ "$hours" -gt 0 ]]; then
        echo "${hours}h${mins}m"
    else
        echo "${mins}m"
    fi
}

# Try native rate_limits from the statusline JSON first.
# Documented shape: .rate_limits.{five_hour,seven_day}.{used_percentage,resets_at}
# used_percentage is a float (e.g. 23.5) -> truncate, since [[ -gt ]] is integer-only.
# resets_at is Unix epoch seconds, NOT ISO 8601.
five_hour_pct=$(jval '.rate_limits.five_hour.used_percentage' | cut -d. -f1)
five_hour_reset=$(jval '.rate_limits.five_hour.resets_at')
weekly_pct=$(jval '.rate_limits.seven_day.used_percentage' | cut -d. -f1)
weekly_reset=$(jval '.rate_limits.seven_day.resets_at')

# If the percentages aren't in the JSON, try cached API data.
# Gate on the percentage only: resets_at can be present while used_percentage
# is absent, and keying off the timestamp would skip this fallback entirely.
if [[ -z "$five_hour_pct" ]]; then
    cache_fresh=false

    if [[ -f "$USAGE_CACHE" ]]; then
        cache_age=0
        if [[ "$(uname)" == "Darwin" ]]; then
            cache_mtime=$(stat -f %m "$USAGE_CACHE" 2>/dev/null || echo 0)
        else
            cache_mtime=$(stat -c %Y "$USAGE_CACHE" 2>/dev/null || echo 0)
        fi
        now_epoch=$(date +%s)
        cache_age=$(( now_epoch - cache_mtime ))
        if [[ "$cache_age" -lt "$USAGE_CACHE_TTL" ]]; then
            cache_fresh=true
        fi
    fi

    # Refresh cache if stale
    if [[ "$cache_fresh" == false ]]; then
        # Get OAuth token from macOS Keychain
        creds_json=$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null || echo "")
        if [[ -n "$creds_json" ]]; then
            access_token=$(echo "$creds_json" | jq -r '.claudeAiOauth.accessToken // empty' 2>/dev/null)
            # Skip the request if the stored token is already expired. This script
            # cannot refresh it, so calling anyway just burns --max-time on every
            # render for a guaranteed 401.
            token_expired=$(echo "$creds_json" | jq -r \
                '.claudeAiOauth.expiresAt as $e | if $e and (($e/1000) < now) then "yes" else "no" end' 2>/dev/null)
            if [[ "$token_expired" == "yes" ]]; then
                access_token=""
            fi
            if [[ -n "$access_token" ]]; then
                curl -sf --max-time 3 \
                    -H "Authorization: Bearer ${access_token}" \
                    -H "Accept: application/json" \
                    -H "Content-Type: application/json" \
                    -H "anthropic-beta: oauth-2025-04-20" \
                    "https://api.anthropic.com/api/oauth/usage" \
                    -o "$USAGE_CACHE" 2>/dev/null || true
            fi
        fi
    fi

    # Read from cache
    if [[ -f "$USAGE_CACHE" ]]; then
        five_hour_pct=$(jq -r '.five_hour.utilization // 0' "$USAGE_CACHE" 2>/dev/null | cut -d. -f1)
        five_hour_reset=$(jq -r '.five_hour.resets_at // empty' "$USAGE_CACHE" 2>/dev/null)
        weekly_pct=$(jq -r '.seven_day.utilization // 0' "$USAGE_CACHE" 2>/dev/null | cut -d. -f1)
        weekly_reset=$(jq -r '.seven_day.resets_at // empty' "$USAGE_CACHE" 2>/dev/null)
    fi
fi

# Build usage line
usage_line=""
five_hour_pct=${five_hour_pct:-0}
weekly_pct=${weekly_pct:-0}

# Render only when we have a real percentage. Rendering off a bare resets_at
# would print a hardcoded-looking "0%" that never moves.
if [[ "$five_hour_pct" -gt 0 ]]; then
    five_bar=$(usage_bar "$five_hour_pct")
    five_color=$(usage_color "$five_hour_pct")
    five_countdown=""
    if [[ -n "${five_hour_reset:-}" ]]; then
        five_countdown=" ↻$(format_countdown "$five_hour_reset")"
    fi
    usage_line+="${C_BOLD}5h:${C_RESET} ${five_bar} ${five_color}${five_hour_pct}%${C_RESET}${C_DIM}${five_countdown}${C_RESET}"

    weekly_bar=$(usage_bar "$weekly_pct")
    weekly_color=$(usage_color "$weekly_pct")
    weekly_countdown=""
    if [[ -n "${weekly_reset:-}" ]]; then
        weekly_countdown=" ↻$(format_countdown "$weekly_reset")"
    fi
    usage_line+=" ${SEP} ${C_BOLD}7d:${C_RESET} ${weekly_bar} ${weekly_color}${weekly_pct}%${C_RESET}${C_DIM}${weekly_countdown}${C_RESET}"
fi

# -- Output ------------------------------------------------------------------
printf '%b\n' "$line1"
printf '%b\n' "$line2"
printf '%b\n' "$ctx_line1"
printf '%b\n' "$ctx_line2"
if [[ -n "$info_line" ]]; then
    printf '%b\n' "$info_line"
fi
if [[ -n "$usage_line" ]]; then
    printf '%b\n' "$usage_line"
fi

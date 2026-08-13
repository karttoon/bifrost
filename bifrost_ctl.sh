#!/bin/bash
set +H
# bifrost_ctl.sh - Manage the Bifrost bridge
# Usage: ./bifrost_ctl.sh {start|stop|restart|status|log|errors}

BIFROST_DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="${BIFROST_DIR}/bifrost.pid"
LOGFILE="${BIFROST_DIR}/bifrost_stdout.log"
APPLOG="${BIFROST_DIR}/bifrost.log"
SCRIPT="${BIFROST_DIR}/bifrost.py"
CONFIG="${BIFROST_DIR}/bifrost_config.json"

RED="\033[0;31m"
GRN="\033[0;32m"
YEL="\033[0;33m"
RST="\033[0m"

get_pid() {
    # First try PID file
    if [ -f "$PIDFILE" ]; then
        local pid=$(cat "$PIDFILE")
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return
        fi
    fi
    # Fallback: find by process name
    local pid=$(pgrep -f "python3 $SCRIPT" 2>/dev/null | head -1)
    if [ -n "$pid" ]; then
        echo "$pid" > "$PIDFILE"
        echo "$pid"
    fi
}

is_running() {
    local pid=$(get_pid)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

cmd_start() {
    if is_running; then
        echo -e "${YEL}Bifrost already running (PID $(get_pid))${RST}"
        return 0
    fi
    echo "Starting Bifrost..."
    rm -f "$PIDFILE"
    export PYTHONIOENCODING=utf-8
    nohup python3 "$SCRIPT" "$CONFIG" >> "$LOGFILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PIDFILE"
    sleep 3
    if is_running; then
        echo -e "${GRN}Bifrost started (PID $(get_pid))${RST}"
    else
        echo -e "${RED}Bifrost failed to start. Check logs:${RST}"
        tail -10 "$LOGFILE"
        rm -f "$PIDFILE"
        return 1
    fi
}

cmd_stop() {
    if ! is_running; then
        echo -e "${YEL}Bifrost is not running${RST}"
        rm -f "$PIDFILE"
        return 0
    fi
    local pid=$(get_pid)
    echo "Stopping Bifrost (PID $pid)..."
    kill "$pid" 2>/dev/null
    for i in 1 2 3 4 5; do
        if \! kill -0 "$pid" 2>/dev/null; then
            break
        fi
        sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "Sending SIGKILL..."
        kill -9 "$pid" 2>/dev/null
    fi
    rm -f "$PIDFILE"
    echo -e "${GRN}Bifrost stopped${RST}"
}

cmd_restart() {
    cmd_stop
    sleep 1
    cmd_start
}

cmd_status() {
    if is_running; then
        local pid=$(get_pid)
        echo -e "${GRN}Bifrost is running (PID $pid)${RST}"
        echo ""
        # Uptime
        local elapsed=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d " ")
        if [ -n "$elapsed" ]; then
            local days=$((elapsed / 86400))
            local hours=$(( (elapsed % 86400) / 3600 ))
            local mins=$(( (elapsed % 3600) / 60 ))
            echo "  Uptime: ${days}d ${hours}h ${mins}m"
        fi
        # Memory
        local mem=$(ps -o rss= -p "$pid" 2>/dev/null | tr -d " ")
        if [ -n "$mem" ]; then
            echo "  Memory: $((mem / 1024)) MB"
        fi
        # Last log lines
        echo ""
        echo "  Last 5 log entries:"
        tail -5 "$APPLOG" 2>/dev/null | sed "s/^/    /"
    else
        echo -e "${RED}Bifrost is not running${RST}"
        if [ -f "$PIDFILE" ]; then
            echo "  (stale PID file found, cleaning up)"
            rm -f "$PIDFILE"
        fi
    fi
}

cmd_log() {
    local lines=${1:-30}
    echo "=== Bifrost log (last $lines lines) ==="
    tail -n "$lines" "$APPLOG" 2>/dev/null || echo "(no log file)"
}

cmd_follow() {
    echo "=== Following Bifrost log (Ctrl+C to stop) ==="
    tail -f "$APPLOG" 2>/dev/null || echo "(no log file)"
}

cmd_errors() {
    echo "=== Bifrost errors (last 30) ==="
    grep -i "error\|warning\|exception\|traceback" "$APPLOG" 2>/dev/null | tail -30 || echo "(no errors found)"
}

cmd_help() {
    echo "Usage: $0 {command}"
    echo ""
    echo "Commands:"
    echo "  start      Start the Bifrost bridge"
    echo "  stop       Stop the Bifrost bridge"
    echo "  restart    Stop then start"
    echo "  status     Show running state, uptime, memory, recent logs"
    echo "  log [N]    Show last N log lines (default 30)"
    echo "  follow     Tail the log in real-time"
    echo "  errors     Show recent errors and warnings"
    echo "  help       Show this message"
}

case "${1}" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_restart ;;
    status)  cmd_status ;;
    log)     cmd_log "$2" ;;
    follow)  cmd_follow ;;
    errors)  cmd_errors ;;
    help)    cmd_help ;;
    *)       cmd_help ;;
esac

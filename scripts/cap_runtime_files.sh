#!/bin/sh
set -eu

runtime_dir="${CERBO_BALANCER_RUNTIME_DIR:-/data/home/nodered}"

compact_jsonl() {
    file="$1"
    maximum_bytes="$2"
    retained_bytes="$3"

    [ -f "$file" ] || return 0
    before="$(wc -c < "$file")"
    [ "$before" -le "$maximum_bytes" ] && return 0

    directory="${file%/*}"
    basename="${file##*/}"
    temporary="$directory/.${basename}.cap.$$"
    trap 'rm -f "$temporary"' EXIT HUP INT TERM

    # tail -c can begin in the middle of a JSON record. Drop that first
    # fragment so every retained line remains independently parseable.
    tail -c "$retained_bytes" "$file" | sed '1d' > "$temporary"
    after="$(wc -c < "$file")"
    if [ "$before" -ne "$after" ]; then
        rm -f "$temporary"
        trap - EXIT HUP INT TERM
        return 0
    fi

    owner="$(stat -c '%u:%g' "$file" 2>/dev/null || true)"
    [ -n "$owner" ] && chown "$owner" "$temporary"
    chmod 0644 "$temporary"
    mv "$temporary" "$file"
    trap - EXIT HUP INT TERM
}

compact_jsonl "$runtime_dir/cerbo-balancer-state.json" 524288 393216
compact_jsonl "$runtime_dir/cerbo-balancer-config.json" 131072 98304

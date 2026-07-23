#!/usr/bin/env bash
# time_slice.sh - identify which time portion a slice number belongs to,
# and print the slice's start/end times.
#
# A "slice number" is: epoch_seconds / portion_size_seconds
# e.g. current 2-week slice:  $(date +%s) / (3600*24*14)
#
# The portion is guessed by picking the portion whose *current* slice
# number is closest (relatively) to the given number.
#
# usage: time_slice.sh [-a] SLICE_NUMBER
#   -a   also print the interval the number would map to in every portion

set -euo pipefail

usage() { echo "usage: $(basename "$0") [-a] SLICE_NUMBER" >&2; exit 1; }

show_all=0
if [[ "${1:-}" == "-a" ]]; then show_all=1; shift; fi
[[ $# -eq 1 && "$1" =~ ^[0-9]+$ ]] || usage
slice=$1

names=("20 minutes" "hour" "day" "week" "14 days" "30 days" "180 days")
secs=(1200 3600 86400 604800 1209600 2592000 15552000)

now=$(date +%s)

fmt() { date -u -d "@$1" +"%-d/%-m/%Y %H:%M"; }

# pick the portion whose current slice number is closest to the input
# (relative distance, scaled to integer math)
best=-1
best_score=0
for i in "${!secs[@]}"; do
    cur=$(( now / secs[i] ))
    diff=$(( slice > cur ? slice - cur : cur - slice ))
    score=$(( diff * 1000000 / cur ))
    if (( best < 0 || score < best_score )); then
        best=$i
        best_score=$score
    fi
done

start=$(( slice * secs[best] ))
end=$(( (slice + 1) * secs[best] ))
cur=$(( now / secs[best] ))

echo "$slice: ${names[best]}, between $(fmt "$start") > $(fmt "$end") (UTC)"
echo "current ${names[best]} slice is $cur"

if (( show_all )); then
    echo
    printf '%-12s %-12s %-22s %-22s\n' "portion" "current" "start" "end"
    for i in "${!secs[@]}"; do
        cur=$(( now / secs[i] ))
        start=$(( slice * secs[i] ))
        end=$(( (slice + 1) * secs[i] ))
        printf '%-12s %-12s %-22s %-22s\n' \
            "${names[i]}" "$cur" "$(fmt "$start")" "$(fmt "$end")"
    done
fi

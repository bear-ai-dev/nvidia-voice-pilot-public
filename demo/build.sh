#!/bin/bash
# Build the demo's data: one trace per runnable conversation (env/trace.py) and
# a compact MP3 of each call's audio. The traces are committed; the audio is
# derived from conversations/<id>/audio/full.wav and is not.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

python3 env/trace.py --out demo/traces

mkdir -p demo/audio
for trace in demo/traces/*.json; do
    id=$(basename "$trace" .json)
    [ "$id" = index ] && continue
    out="demo/audio/$id.mp3"
    [ -s "$out" ] && continue
    ffmpeg -loglevel error -y -i "conversations/$id/audio/full.wav" -ac 1 -ar 24000 -b:a 48k "$out"
    echo "encoded $out"
done

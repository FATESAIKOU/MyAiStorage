#!/usr/bin/env bash
# spike/scripts/run_bench_2_6_pipeline.sh
# 依 PM 指示依序執行 Config A (200 push) 與 Config B (200 push)
set -euo pipefail

export RCLONE_CONFIG=/tmp/rclone.conf
cp /secrets/rclone-committer-test.conf "${RCLONE_CONFIG}"
mkdir -p /work/.config/rclone
cp /secrets/rclone-committer-test.conf /work/.config/rclone/rclone.conf

echo "============================================================"
echo "Phase 1: Config A (Git blob, max_bundles=10, 200 rounds)"
echo "Prefix: agora-2.6git"
echo "Start time: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "============================================================"

python3 /work/bench_2_6.py \
  --config A \
  --workdir /work/repo-a \
  --prefix agora-2.6git \
  --max-bundles 10 \
  --rounds 200 \
  --pre-seed-syncs 150 \
  --output-tsv /work/bench-a-200.tsv \
  --output-json /work/bench-a-200.json

echo "============================================================"
echo "Phase 2: Config B (Annex object, max_bundles=10, 200 rounds)"
echo "Prefix: agora-2.6annex"
echo "Start time: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "============================================================"

python3 /work/bench_2_6.py \
  --config B \
  --workdir /work/repo-b \
  --prefix agora-2.6annex \
  --max-bundles 10 \
  --rounds 200 \
  --pre-seed-syncs 150 \
  --output-tsv /work/bench-b-200.tsv \
  --output-json /work/bench-b-200.json

echo "============================================================"
echo "ALL BENCHMARKS COMPLETED!"
echo "End time: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "============================================================"

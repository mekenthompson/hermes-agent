#!/usr/bin/env bash
# Generate the image SBOM and run the critical vulnerability scan against one
# local image concurrently. Fail-closed: every background exit status is
# checked, both logs are printed, the image ID must be the same before and
# after, and both outputs must exist. Called by action.yml; tested by
# tests/ci/test_fork_image_workflow.py with stub tools.
set -euo pipefail

: "${IMAGE:?IMAGE is required}"
: "${SBOM_OUTPUT:?SBOM_OUTPUT is required}"
: "${SCAN_OUTPUT:?SCAN_OUTPUT is required}"
: "${SYFT_CMD:?SYFT_CMD is required}"
: "${TRIVY_CACHE_DIR:?TRIVY_CACHE_DIR is required}"
TRIVY_CMD="${TRIVY_CMD:-trivy}"

work=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/image-evidence.XXXXXX")
mkdir -p "$work/syft-tmp" "$work/trivy-tmp"
sbom_log="$work/sbom.log"
scan_log="$work/scan.log"
sbom_partial="$work/sbom.partial.json"

# Pin the exact image both tools must read. Nothing in this job retags it
# while they run; the check after they finish proves it.
image_id=$(docker image inspect --format '{{.Id}}' "$IMAGE")
image_size=$(docker image inspect --format '{{.Size}}' "$IMAGE")
echo "Image $IMAGE is $image_id ($image_size bytes)"

# Each tool exports the image from the daemon into its own temp dir, so
# running both at once needs both copies on disk together. Reserve three image
# sizes (Syft archive + unpacked layers, Trivy archive) plus 2 GiB; with less
# free space run them one after the other instead. Both still gate either way.
avail_kib=$(df -Pk "$work" | awk 'NR==2 {print $4}')
avail=$((avail_kib * 1024))
need=$((image_size * 3 + 2 * 1024 * 1024 * 1024))
if [ "${IMAGE_EVIDENCE_MODE:-auto}" = "sequential" ] || [ "$avail" -lt "$need" ]; then
  mode=sequential
  echo "::notice::Running SBOM and scan sequentially: ${avail} bytes free, ${need} needed to overlap them."
else
  mode=concurrent
  echo "Running SBOM and scan concurrently: ${avail} bytes free, ${need} needed."
fi

# Matches anchore/sbom-action: `syft scan <image> -o spdx-json`, stdout is the
# SBOM, written to the output file only when Syft succeeds.
run_sbom() {
  SYFT_CHECK_FOR_APP_UPDATE=false TMPDIR="$work/syft-tmp" \
    "$SYFT_CMD" scan "$IMAGE" -o spdx-json > "$sbom_partial"
}

# Matches aquasecurity/trivy-action with format json, scanners vuln, severity
# CRITICAL, ignore-unfixed and exit-code 1. Trivy writes the report itself,
# including when it finds a vulnerability and exits 1.
run_scan() {
  TMPDIR="$work/trivy-tmp" "$TRIVY_CMD" image \
    --format json \
    --output "$SCAN_OUTPUT" \
    --scanners vuln \
    --severity CRITICAL \
    --ignore-unfixed \
    --exit-code 1 \
    --cache-dir "$TRIVY_CACHE_DIR" \
    "$IMAGE"
}

pids=()
cleanup() {
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  rm -rf "$work"
}
trap cleanup EXIT

sbom_status=0
scan_status=0
if [ "$mode" = concurrent ]; then
  run_sbom > "$sbom_log" 2>&1 &
  sbom_pid=$!
  pids+=("$sbom_pid")
  run_scan > "$scan_log" 2>&1 &
  scan_pid=$!
  pids+=("$scan_pid")
  wait "$sbom_pid" || sbom_status=$?
  wait "$scan_pid" || scan_status=$?
  pids=()
else
  run_sbom > "$sbom_log" 2>&1 || sbom_status=$?
  run_scan > "$scan_log" 2>&1 || scan_status=$?
fi

echo "::group::Syft SBOM log (exit $sbom_status)"
cat "$sbom_log"
echo "::endgroup::"
echo "::group::Trivy scan log (exit $scan_status)"
cat "$scan_log"
echo "::endgroup::"

failed=0
if [ "$sbom_status" -ne 0 ]; then
  echo "::error::SBOM generation failed with exit $sbom_status"
  tail -n 50 "$sbom_log" >&2
  failed=1
else
  mv "$sbom_partial" "$SBOM_OUTPUT"
fi
if [ "$scan_status" -ne 0 ]; then
  echo "::error::Critical vulnerability scan failed with exit $scan_status"
  tail -n 50 "$scan_log" >&2
  failed=1
fi

after_id=$(docker image inspect --format '{{.Id}}' "$IMAGE")
if [ "$after_id" != "$image_id" ]; then
  echo "::error::$IMAGE changed from $image_id to $after_id while it was scanned"
  failed=1
fi

if [ "$failed" -eq 0 ]; then
  for output in "$SBOM_OUTPUT" "$SCAN_OUTPUT"; do
    if [ ! -s "$output" ]; then
      echo "::error::Expected output $output is missing or empty"
      failed=1
    fi
  done
fi

if [ "$failed" -ne 0 ]; then
  exit 1
fi
echo "SBOM ($SBOM_OUTPUT) and scan ($SCAN_OUTPUT) passed for $image_id"

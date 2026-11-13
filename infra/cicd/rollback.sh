#!/usr/bin/env bash
# shellcheck disable=SC2154
# Sourced by deploy.sh. Uses its K command array, DEPLOYMENTS, before_* snapshots,
# APPLIED/DONE flags and log/cleanup functions. Kept separate to exercise failures
# with fake kubectl responses without contacting a cluster.
# The before_* arrays are assigned by the caller, not by this sourced helper.

template_of() {
  "${K[@]}" get deployment "$1" -o jsonpath='{.spec.template.spec.containers[*].image}{" | "}{.spec.template.spec.containers[*].envFrom[*].configMapRef.name}'
}

rollback() {
  log "rolling back Deployments changed by this run to their pre-run revision"
  local d now restored failed=0
  for d in "${DEPLOYMENTS[@]}"; do
    if ! now="$("${K[@]}" get deployment "$d" -o jsonpath='{.metadata.generation}')" || [[ -z "$now" ]]; then
      log "ERROR: rollback of $d cannot be verified: deployment generation unavailable"
      failed=1
      continue
    fi
    [[ "$now" != "${before_generation[$d]}" ]] || continue
    # A run can create several revisions; restore the exact pre-run revision.
    if ! "${K[@]}" rollout undo "deployment/$d" --to-revision="${before_revision[$d]}"; then
      log "ERROR: rollback command failed for $d"
      failed=1
      continue
    fi
    if ! "${K[@]}" rollout status "deployment/$d" --timeout=300s; then
      log "ERROR: rollback of $d did not become ready"
      failed=1
      continue
    fi
    if ! restored="$(template_of "$d")"; then
      log "ERROR: rollback of $d cannot be verified: template unavailable"
      failed=1
      continue
    fi
    if [[ "$restored" == "${before_template[$d]}" ]]; then
      log "rollback verified: $d = ${restored}"
    else
      log "ERROR: rollback of $d incomplete: now '${restored}', expected '${before_template[$d]}'"
      failed=1
    fi
  done
  # Continue restoring the other Deployment even when one fails, but never turn
  # a failed restoration into a success just because the desired template matches.
  return "$failed"
}

on_exit() {
  local rc=$?
  trap - EXIT
  if (( ${MAINTENANCE_STARTED:-0} == 1 && DONE == 0 )); then
    # The V2 schema/payload may already exist. Never resurrect a V1 writer.
    "${K[@]}" patch deployment seoul-now-api --type=merge -p '{"spec":{"replicas":0}}' \
      || log "ERROR: could not stop API after failed cutover; inspect Pods immediately"
    log "ERROR: V2 cutover incomplete; API kept stopped. Repair forward or restore a pre-cutover DB snapshot with a separately reviewed V1 configuration."
    rc=1
  elif (( APPLIED == 1 && DONE == 0 )); then
    if ! rollback; then
      log "ERROR: automatic rollback incomplete; manual action required"
    fi
    rc=1
  fi
  # A rollback failure must not short-circuit Job/temporary ConfigMap cleanup.
  cleanup
  exit "$rc"
}

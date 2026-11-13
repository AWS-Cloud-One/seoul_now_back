#!/usr/bin/env bash
# Backend deploy, run by the VPC CodeBuild project (infra/cicd/deploy.buildspec.yml).
#
# Inputs (environment):
#   IMAGE_DIGEST   sha256:<64 hex> of an image in the backend ECR repository
#   GIT_SHA        commit whose manifests are in the bundle (labels and logs only)
#   DEPLOY_ACTION  release (default) | rollback | cutover (one-time V2 maintenance)
#   DEPLOY_MODE    deploy (default) | verify
#   CLUSTER_NAME, KUBE_NAMESPACE, AWS_REGION   set by the CodeBuild project
#
# Bundle layout: infra/cicd, infra/scripts/w4_database.py and migrations/versions
# from the newest pipeline commit; infra/kubernetes from the commit being deployed
# (for a rollback: the commit of the older image, so its config comes back too).
#
# All actions first run a read-only Job with the target image that reports the
# database revision and the image's migrations; db_compat.py then decides:
#   release   image at/ahead of the DB, compatible pending migrations only
#             -> migrate + grants -> API permission probe
#   rollback  DB is never changed; allowed only if every newer DB revision is
#             backward_compatible with the image (no migrate, grants or probe)
#   cutover   explicit 0016 -> 0023 transition (or 0023 repair): check references,
#             stop API, migrate + grants + probe; failure leaves API stopped
# Then: apply image + versioned (hashed) ConfigMaps -> rollouts -> public health.
# Compatible release/rollback failures after apply restore each Deployment to its
# exact pre-run revision (image AND config). Cutover never restores a V1 image.
#
# verify changes no workload: server-side dry runs, the read-only revision Job
# (deleted afterwards), the compatibility decision and a diff.
set -Eeuo pipefail

REGISTRY_REPO="963701985499.dkr.ecr.ap-northeast-2.amazonaws.com/seoul-now/lab/backend"
HEALTH_URL="https://d28b5sbbw9in9y.cloudfront.net/api/v1/health/ready"
MIGRATION_SECRET="arn:aws:secretsmanager:ap-northeast-2:963701985499:secret:seoul-now/lab/migration-ECQfdl"
API_SECRET="arn:aws:secretsmanager:ap-northeast-2:963701985499:secret:seoul-now/lab/api-5C2yzm"
DEPLOYMENTS=(seoul-now-api seoul-now-collector)
CONFIG_LABEL="seoul-now/config=generated"

DEPLOY_ACTION="${DEPLOY_ACTION:-release}"
DEPLOY_MODE="${DEPLOY_MODE:-deploy}"
GIT_SHA="${GIT_SHA:-unknown}"
# Test only, never set by the workflow: "rollback" applies a config-only change
# after a successful rollout and then fails on purpose, so the automatic rollback
# really runs and must bring back the pre-run image and config.
DEPLOY_DRILL="${DEPLOY_DRILL:-}"
[[ -z "$DEPLOY_DRILL" || "$DEPLOY_DRILL" == rollback ]] || { echo "DEPLOY_DRILL must be empty or rollback" >&2; exit 1; }
: "${CLUSTER_NAME:?}" "${KUBE_NAMESPACE:?}" "${AWS_REGION:?}" "${IMAGE_DIGEST:?}"

log() { printf '%s [deploy] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
fail() { log "ERROR: $*"; exit 1; }

[[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "IMAGE_DIGEST must be sha256:<64 hex>"
[[ "$DEPLOY_MODE" == deploy || "$DEPLOY_MODE" == verify ]] || fail "DEPLOY_MODE must be deploy or verify"
[[ "$DEPLOY_ACTION" == release || "$DEPLOY_ACTION" == rollback || "$DEPLOY_ACTION" == cutover ]] || fail "invalid DEPLOY_ACTION"
[[ "$DEPLOY_ACTION" != cutover || -z "$DEPLOY_DRILL" ]] || fail "cutover cannot run a rollback drill"
IMAGE="${REGISTRY_REPO}@${IMAGE_DIGEST}"
RUN_ID="$(date -u +%Y%m%d%H%M%S)-${GIT_SHA:0:7}"
WORK="$(mktemp -d)"
export KUBECONFIG="$WORK/kubeconfig"
K=(kubectl -n "$KUBE_NAMESPACE" --request-timeout=60s)

APPLIED=0   # workloads were applied by this run
DONE=0      # every check after the apply passed
MAINTENANCE_STARTED=0
declare -A before_generation before_revision before_template

cleanup() {
  # Remove this run's one-shot Jobs and their script ConfigMaps.
  "${K[@]}" delete job,configmap -l "seoul-now/deploy-run=${RUN_ID}" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  rm -rf "$WORK"
}

# Shared with the shell regression tests: rollback failure must still run cleanup.
source infra/cicd/rollback.sh
trap on_exit EXIT

log "action=${DEPLOY_ACTION} mode=${DEPLOY_MODE} commit=${GIT_SHA} image=${IMAGE_DIGEST} run=${RUN_ID}"
aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$AWS_REGION" --kubeconfig "$KUBECONFIG" >/dev/null
kubectl version --output=yaml | grep -E 'gitVersion' || true

# The deploy identity must hold exactly what the pipeline needs, nothing cluster-wide.
for check in create:jobs create:configmaps delete:configmaps patch:deployments \
  list:replicasets get:pods/log; do
  "${K[@]}" auth can-i "${check%%:*}" "${check#*:}" >/dev/null \
    || fail "missing permission: ${check/:/ }"
done
if kubectl auth can-i create namespaces >/dev/null 2>&1; then
  fail "deploy identity unexpectedly has cluster-scoped rights"
fi

# The workflow verifies the image exists in ECR (this role has no ECR access);
# a missing image would fail the revision Job before anything changes.

# Workloads with the requested digest (both API and Collector).
cp -R infra/kubernetes "$WORK/k8s"
sed -i.bak -E "s#^(    digest: )sha256:[0-9a-f]{64}#\1${IMAGE_DIGEST}#" "$WORK/k8s/kustomization.yaml"
grep -q "digest: ${IMAGE_DIGEST}" "$WORK/k8s/kustomization.yaml" || fail "digest not rendered"
kubectl kustomize "$WORK/k8s" > "$WORK/workloads.yaml"
[[ "$(grep -c "@${IMAGE_DIGEST}" "$WORK/workloads.yaml")" -eq 2 ]] || fail "expected 2 images"
# Manifests older than versioned config would overwrite the shared, unhashed
# ConfigMaps in place, which no rollback can undo.
[[ "$(grep -c "seoul-now/config: generated" "$WORK/workloads.yaml")" -eq 2 ]] \
  || fail "manifests of ${GIT_SHA} predate versioned ConfigMaps; deploy them by hand"

render_job() { # name role service-account secret script
  JOB_NAME="$1" RUNTIME_ROLE="$2" SERVICE_ACCOUNT="$3" SECRET_ID="$4" SCRIPT="$5" \
    IMAGE="$IMAGE" RUN_ID="$RUN_ID" python3 - <<'PY'
import os, string, sys
template = open("infra/cicd/db-job.template.yaml").read()
sys.stdout.write(string.Template(template).substitute(os.environ))
PY
}

job_name() { printf 'deploy-%s-%s' "$1" "$(echo "$RUN_ID" | tr '[:upper:]' '[:lower:]')" | cut -c1-63; }

run_job() { # step role service-account secret script; log saved to $WORK/<step>.log
  local name; name="$(job_name "$1")"
  log "job ${name}: $5"
  "${K[@]}" create configmap "$name" --from-file=w4_database.py=infra/scripts/w4_database.py \
    --dry-run=client -o yaml | "${K[@]}" label --local -f - "seoul-now/deploy-run=${RUN_ID}" -o yaml \
    | "${K[@]}" apply -f - >/dev/null
  render_job "$name" "$2" "$3" "$4" "$5" | "${K[@]}" apply -f - >/dev/null
  local deadline=$((SECONDS + 660)) state=""
  while (( SECONDS < deadline )); do
    state="$("${K[@]}" get job "$name" -o jsonpath='{.status.succeeded}/{.status.failed}')"
    [[ "$state" == 1/* || "$state" == */1 ]] && break
    sleep 5
  done
  "${K[@]}" logs "job/${name}" --tail=200 | tee "$WORK/$1.log" || true
  [[ "$state" == 1/* ]] || fail "job ${name} did not succeed (succeeded/failed=${state})"
}

# Read-only: which revision is the DB at, which migrations does the image ship.
check_database() {
  run_job revision migration seoul-now-migration "$MIGRATION_SECRET" \
    "python /work/w4_database.py revision"
  python3 infra/cicd/db_compat.py --action "$DEPLOY_ACTION" \
    --migrations migrations/versions --report "$WORK/revision.log" \
    || fail "${DEPLOY_ACTION} database compatibility check did not pass (see db_compat)"
}

# Keep generated ConfigMaps that any ReplicaSet (rollback history) or Deployment
# still references; delete the rest. Unlabelled ConfigMaps are never touched.
prune_configs() {
  local used name
  used="$("${K[@]}" get replicasets,deployments -o jsonpath='{range .items[*]}{.spec.template.spec.containers[*].envFrom[*].configMapRef.name}{" "}{.spec.template.spec.volumes[*].configMap.name}{"\n"}{end}' | tr ' ' '\n' | sed '/^$/d')"
  [[ -n "$used" ]] || { log "prune: no ConfigMap references found, skipping"; return 0; }
  for name in $("${K[@]}" get configmaps -l "$CONFIG_LABEL" -o jsonpath='{.items[*].metadata.name}'); do
    if ! grep -qxF -- "$name" <<<"$used"; then
      log "prune: deleting unreferenced ConfigMap ${name}"
      "${K[@]}" delete configmap "$name" --ignore-not-found >/dev/null
    fi
  done
}

if [[ "$DEPLOY_MODE" == verify ]]; then
  log "verify: server-side dry runs, read-only DB check; no workload is changed"
  "${K[@]}" apply --dry-run=server -f "$WORK/workloads.yaml" >/dev/null
  for step in revision migrate probe; do
    render_job "$(job_name "$step")" migration seoul-now-migration "$MIGRATION_SECRET" "true" \
      | "${K[@]}" apply --dry-run=server -f - >/dev/null
  done
  check_database
  set +e; "${K[@]}" diff -f "$WORK/workloads.yaml" > "$WORK/diff.txt"; rc=$?; set -e
  (( rc <= 1 )) || fail "kubectl diff failed (${rc})"
  log "verify: changes a ${DEPLOY_ACTION} of this image would make:"
  grep -E '^[-+] .*(image:|digest|configMapRef|name: seoul-now-(runtime|api-settings))' "$WORK/diff.txt" \
    || log "verify: no image or config change"
  curl -fsS -m 10 -o /dev/null -w "verify: public health HTTP %{http_code}\n" "$HEALTH_URL"
  log "verify: OK"
  exit 0
fi

# 1. Database. A release migrates first while the old Pods keep running, so every
#    pending migration must declare `backward_compatible = True`. check_database
#    rejects incompatible/unmarked changes before any migration or workload mutation.
#    A rollback leaves the database exactly as it is.
check_database
if [[ "$DEPLOY_ACTION" != rollback ]] && grep -q 'APP_CHAT_V2_ENABLED: "true"\|APP_CHAT_V2_ENABLED: true' "$WORK/workloads.yaml"; then
  run_job chat-reference api seoul-now-api "$API_SECRET" "python /work/w4_database.py chat-reference"
fi
if [[ "$DEPLOY_ACTION" == cutover ]]; then
  log "V2 maintenance: stopping API; failures stay stopped for forward repair"
  MAINTENANCE_STARTED=1
  "${K[@]}" patch deployment seoul-now-api --type=merge -p '{"spec":{"replicas":0}}'
  "${K[@]}" wait --for=delete pod -l app.kubernetes.io/component=api --timeout=180s
fi
if [[ "$DEPLOY_ACTION" != rollback ]]; then
  run_job migrate migration seoul-now-migration "$MIGRATION_SECRET" \
    "python /work/w4_database.py migrate && python /work/w4_database.py grants"
  run_job probe api seoul-now-api "$API_SECRET" "python /work/w4_database.py probe"
  if grep -q 'APP_CHAT_V2_ENABLED: "true"\|APP_CHAT_V2_ENABLED: true' "$WORK/workloads.yaml"; then
    run_job chat-preflight api seoul-now-api "$API_SECRET" "python /work/w4_database.py chat-preflight"
  fi
else
  log "rollback: database unchanged; skipping migrate, grants and probe"
fi

# 2. Workloads. Record each Deployment's revision to return to on failure.
for d in "${DEPLOYMENTS[@]}"; do
  before_generation[$d]="$("${K[@]}" get deployment "$d" -o jsonpath='{.metadata.generation}')"
  before_revision[$d]="$("${K[@]}" get deployment "$d" -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}')"
  before_template[$d]="$(template_of "$d")"
  [[ -n "${before_revision[$d]}" ]] || fail "$d has no rollout revision"
  log "$d before: revision ${before_revision[$d]}: ${before_template[$d]}"
done

log "applying workloads"
APPLIED=1
"${K[@]}" apply -f "$WORK/workloads.yaml"

for d in "${DEPLOYMENTS[@]}"; do
  "${K[@]}" rollout status "deployment/$d" --timeout=300s
done

if [[ "$DEPLOY_DRILL" == rollback ]]; then
  log "drill: config-only change to both Deployments, then failing on purpose"
  mkdir "$WORK/drill"
  cat > "$WORK/drill/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../k8s
configMapGenerator:
  - name: seoul-now-runtime
    namespace: seoul-now
    behavior: merge
    literals:
      - DEPLOY_DRILL_MARKER=${RUN_ID}
EOF
  kubectl kustomize "$WORK/drill" | "${K[@]}" apply -f -
  for d in "${DEPLOYMENTS[@]}"; do
    "${K[@]}" rollout status "deployment/$d" --timeout=300s
    log "drill: $d now $(template_of "$d")"
  done
  fail "drill: simulated failure after rollout"
fi

# 3. Public check through CloudFront -> ALB -> new Pods.
ok=0
for _ in $(seq 1 12); do
  if curl -fsS -m 10 -o /dev/null "$HEALTH_URL"; then ok=1; break; fi
  sleep 5
done
(( ok == 1 )) || fail "public health check failed"

DONE=1
for d in "${DEPLOYMENTS[@]}"; do
  log "$d -> $(template_of "$d")"
done
prune_configs || log "WARN: ConfigMap prune failed; old versions remain (harmless)"
log "${DEPLOY_ACTION} OK"

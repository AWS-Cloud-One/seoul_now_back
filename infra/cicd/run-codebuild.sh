#!/usr/bin/env bash
# Start the VPC deploy CodeBuild with a bundle from S3, wait, print its log, exit with its result.
# Used by .github/workflows/deploy.yml and for a manual `DEPLOY_MODE=verify` check.
#
# Inputs: DEPLOY_PROJECT, ARTIFACT_BUCKET, BUNDLE_KEY, IMAGE_DIGEST, GIT_SHA,
#         DEPLOY_ACTION (release|rollback), DEPLOY_MODE (deploy|verify), AWS_REGION
set -Eeuo pipefail
: "${DEPLOY_PROJECT:?}" "${ARTIFACT_BUCKET:?}" "${BUNDLE_KEY:?}" "${IMAGE_DIGEST:?}" "${AWS_REGION:?}"
GIT_SHA="${GIT_SHA:-unknown}"
DEPLOY_ACTION="${DEPLOY_ACTION:-release}"
DEPLOY_MODE="${DEPLOY_MODE:-deploy}"
DEPLOY_DRILL="${DEPLOY_DRILL:-}"   # test only (see deploy.sh); the workflow never sets it
[[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "invalid IMAGE_DIGEST" >&2; exit 1; }
[[ "$GIT_SHA" =~ ^[0-9a-f]{7,40}$|^unknown$ ]] || { echo "invalid GIT_SHA" >&2; exit 1; }
[[ "$DEPLOY_ACTION" == release || "$DEPLOY_ACTION" == rollback ]] || { echo "invalid DEPLOY_ACTION" >&2; exit 1; }
[[ "$DEPLOY_MODE" == deploy || "$DEPLOY_MODE" == verify ]] || { echo "invalid DEPLOY_MODE" >&2; exit 1; }
[[ -z "$DEPLOY_DRILL" || "$DEPLOY_DRILL" == rollback ]] || { echo "invalid DEPLOY_DRILL" >&2; exit 1; }

overrides=(
  "name=IMAGE_DIGEST,value=${IMAGE_DIGEST},type=PLAINTEXT"
  "name=GIT_SHA,value=${GIT_SHA},type=PLAINTEXT"
  "name=DEPLOY_ACTION,value=${DEPLOY_ACTION},type=PLAINTEXT"
  "name=DEPLOY_MODE,value=${DEPLOY_MODE},type=PLAINTEXT"
)
[[ -n "$DEPLOY_DRILL" ]] && overrides+=("name=DEPLOY_DRILL,value=${DEPLOY_DRILL},type=PLAINTEXT")

build_id="$(aws codebuild start-build \
  --region "$AWS_REGION" \
  --project-name "$DEPLOY_PROJECT" \
  --source-type-override S3 \
  --source-location-override "${ARTIFACT_BUCKET}/${BUNDLE_KEY}" \
  --environment-variables-override "${overrides[@]}" \
  --query build.id --output text)"
echo "CodeBuild ${build_id} (action=${DEPLOY_ACTION} mode=${DEPLOY_MODE})"

status=IN_PROGRESS
deadline=$((SECONDS + 45 * 60))
while [[ "$status" == IN_PROGRESS ]] && (( SECONDS < deadline )); do
  sleep 15
  status="$(aws codebuild batch-get-builds --region "$AWS_REGION" --ids "$build_id" \
    --query 'builds[0].buildStatus' --output text)"
  phase="$(aws codebuild batch-get-builds --region "$AWS_REGION" --ids "$build_id" \
    --query 'builds[0].currentPhase' --output text)"
  echo "  status=${status} phase=${phase}"
done

group="$(aws codebuild batch-get-builds --region "$AWS_REGION" --ids "$build_id" \
  --query 'builds[0].logs.groupName' --output text)"
stream="$(aws codebuild batch-get-builds --region "$AWS_REGION" --ids "$build_id" \
  --query 'builds[0].logs.streamName' --output text)"
if [[ "$group" != None && "$stream" != None ]]; then
  echo "----- CodeBuild log (${group}/${stream}) -----"
  token=""
  while :; do
    args=(--region "$AWS_REGION" --log-group-name "$group" --log-stream-name "$stream" \
      --start-from-head --output json)
    [[ -n "$token" ]] && args+=(--next-token "$token")
    page="$(aws logs get-log-events "${args[@]}")"
    python3 -c 'import json,sys; [print(e["message"], end="") for e in json.load(sys.stdin)["events"]]' <<<"$page"
    next="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["nextForwardToken"])' <<<"$page")"
    [[ "$next" == "$token" ]] && break
    token="$next"
  done
  echo "----- end of log -----"
fi

echo "CodeBuild result: ${status}"
[[ "$status" == SUCCEEDED ]]

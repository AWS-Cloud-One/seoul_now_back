#!/usr/bin/env bash
# Build the CodeBuild source bundle for deploy.sh.
#   make-bundle.sh OUT.zip [MANIFEST_COMMIT]
# Pipeline code (infra/cicd, w4_database.py, migrations/versions) always comes from the
# checked-out tree, so a rollback still uses the newest scripts and migration graph.
# infra/kubernetes comes from MANIFEST_COMMIT when given (a rollback deploys the
# manifests and config of the older image's commit), otherwise from the tree.
set -Eeuo pipefail
out="${1:?usage: make-bundle.sh OUT.zip [MANIFEST_COMMIT]}"
commit="${2:-}"
manifests=(infra/kubernetes/kustomization.yaml infra/kubernetes/seoul-lab.workloads.yaml)

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/infra/scripts" "$stage/migrations"
cp -R infra/cicd "$stage/infra/cicd"
cp infra/scripts/w4_database.py "$stage/infra/scripts/"
cp -R migrations/versions "$stage/migrations/versions"
find "$stage" -name __pycache__ -type d -prune -exec rm -rf {} +
if [[ -n "$commit" ]]; then
  [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || { echo "MANIFEST_COMMIT must be a full sha" >&2; exit 1; }
  git archive "$commit" "${manifests[@]}" | tar -x -C "$stage"
else
  mkdir -p "$stage/infra/kubernetes"
  cp "${manifests[@]}" "$stage/infra/kubernetes/"
fi

rm -f "$out"
out_abs="$(cd "$(dirname "$out")" && pwd)/$(basename "$out")"
(cd "$stage" && zip -q -r "$out_abs" .)
echo "bundle ${out} (manifests from ${commit:-working tree})"

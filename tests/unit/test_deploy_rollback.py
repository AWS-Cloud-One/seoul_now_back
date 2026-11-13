"""Run the real rollback/EXIT functions with a fake kubectl, without AWS access."""

import os
import subprocess
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2] / "infra/cicd/rollback.sh"
HARNESS = r"""
set -Eeuo pipefail
source "$1"
log() { printf '%s\n' "$*"; }
cleanup() { printf 'cleaned\n' > "$CLEANUP"; }
K=(fake_kubectl)
DEPLOYMENTS=(seoul-now-api seoul-now-collector)
declare -A before_generation before_revision before_template
before_generation[seoul-now-api]=1
before_generation[seoul-now-collector]=1
before_revision[seoul-now-api]=7
before_revision[seoul-now-collector]=9
before_template[seoul-now-api]='old-api-image | runtime-old api-old'
before_template[seoul-now-collector]='old-collector-image | runtime-old'
APPLIED=1
DONE=0
[[ "$SCENARIO" == cutover ]] && MAINTENANCE_STARTED=1

fake_kubectl() {
  printf '%s\n' "$*" >> "$CALLS"
  local d="${3#deployment/}"
  case "$1 $2" in
    'get deployment')
      if [[ "$*" == *metadata.generation* ]]; then
        if [[ "$d" == seoul-now-api ]]; then
          case "$SCENARIO" in
            read-fails) return 1 ;;
            empty-generation) return 0 ;;
            unchanged) printf 1; return 0 ;;
          esac
        fi
        printf 2
      else
        if [[ "$d" == seoul-now-api && "$SCENARIO" == template-read-fails ]]; then return 1; fi
        if [[ "$d" == seoul-now-api && "$SCENARIO" == wrong-config ]]; then
          printf 'old-api-image | NEW-config'
        else
          printf '%s' "${before_template[$d]}"
        fi
      fi ;;
    'rollout undo')
      [[ "$d" != seoul-now-api || "$SCENARIO" != undo-fails ]] ;;
    'rollout status')
      [[ "$d" != seoul-now-api || "$SCENARIO" != not-ready ]] ;;
    'patch deployment') return 0 ;;
    *) return 99 ;;
  esac
}
if [[ "$USE_EXIT_TRAP" == 1 ]]; then
  trap on_exit EXIT
  exit 42
fi
rollback
"""


@pytest.fixture
def run_rollback(tmp_path):
    version = subprocess.run(
        ["bash", "-c", 'printf "%s" "${BASH_VERSINFO[0]}"'],
        text=True,
        capture_output=True,
        check=True,
    )
    if int(version.stdout) < 4:
        pytest.skip("deploy.sh requires Bash 4+; run these tests in the Compose tests container")

    def run(scenario, *, exit_trap=False):
        calls, cleanup = tmp_path / "calls", tmp_path / "cleanup"
        result = subprocess.run(
            ["bash", "-c", HARNESS, "rollback-test", str(HELPER)],
            env={
                "PATH": os.environ["PATH"],
                "SCENARIO": scenario,
                "CALLS": str(calls),
                "CLEANUP": str(cleanup),
                "USE_EXIT_TRAP": str(int(exit_trap)),
            },
            text=True,
            capture_output=True,
        )
        return result, calls.read_text(), cleanup.exists()

    return run


def test_success_restores_both_exact_revisions_and_checks_readiness(run_rollback):
    result, calls, _ = run_rollback("success")
    assert result.returncode == 0
    assert result.stdout.count("rollback verified:") == 2
    assert "rollout undo deployment/seoul-now-api --to-revision=7" in calls
    assert "rollout undo deployment/seoul-now-collector --to-revision=9" in calls
    assert calls.count("rollout status") == 2


def test_failed_cutover_keeps_api_stopped_without_v1_rollback(run_rollback):
    result, calls, cleaned = run_rollback("cutover", exit_trap=True)
    assert result.returncode == 1 and cleaned
    assert "patch deployment seoul-now-api" in calls
    assert "rollout undo" not in calls
    assert "Repair forward" in result.stdout


@pytest.mark.parametrize(
    "scenario",
    [
        "not-ready",
        "undo-fails",
        "read-fails",
        "empty-generation",
        "template-read-fails",
        "wrong-config",
    ],
)
def test_a_failed_api_rollback_is_reported_and_collector_recovery_still_runs(
    run_rollback, scenario
):
    result, calls, _ = run_rollback(scenario)
    assert result.returncode == 1
    assert "ERROR:" in result.stdout
    assert "rollback verified: seoul-now-api" not in result.stdout
    assert "rollout undo deployment/seoul-now-collector --to-revision=9" in calls
    assert "rollback verified: seoul-now-collector" in result.stdout


def test_unchanged_deployment_is_not_rolled_back(run_rollback):
    result, calls, _ = run_rollback("unchanged")
    assert result.returncode == 0
    assert "rollout undo deployment/seoul-now-api" not in calls
    assert "rollout undo deployment/seoul-now-collector" in calls


def test_failed_rollback_preserves_failed_exit_and_cleans_up(run_rollback):
    result, _, cleaned = run_rollback("not-ready", exit_trap=True)
    assert result.returncode == 1
    assert "manual action required" in result.stdout
    assert "rollback verified: seoul-now-api" not in result.stdout
    assert cleaned

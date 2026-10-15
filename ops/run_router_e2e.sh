#!/usr/bin/env bash
# Dedicated disposable Free Router stack; a real Q2 restart verifies durable retries.
set -uo pipefail
if [[ "${E2E_DOCKER_CONTEXT:-}" != "kk-minsk" && ( -z "${CI:-}" || -n "${E2E_DOCKER_CONTEXT:-}" ) ]]; then
  echo 'Local E2E requires E2E_DOCKER_CONTEXT=kk-minsk' >&2
  exit 1
fi
docker_args=()
if [[ -n "${E2E_DOCKER_CONTEXT:-}" ]]; then
  docker_args=(--context "$E2E_DOCKER_CONTEXT")
fi
compose=(docker "${docker_args[@]}" compose -p mazory-router-e2e -f compose.e2e.yml -f compose.e2e.router.yml)
# This named stack contains only synthetic fixtures and must start with empty quotas.
"${compose[@]}" down --volumes --remove-orphans || exit $?
"${compose[@]}" up -d --build nginx qcluster outbox || exit $?
"${compose[@]}" up --build --no-deps --abort-on-container-exit --exit-code-from browser browser &
runner_pid=$!
while kill -0 "$runner_pid" 2>/dev/null; do
  if "${compose[@]}" exec -T backend test -f /e2e/router-restart-required 2>/dev/null &&
     ! "${compose[@]}" exec -T backend test -f /e2e/router-restart-done 2>/dev/null; then
    if ! "${compose[@]}" restart -t 10 qcluster; then
      kill "$runner_pid"; wait "$runner_pid"; exit 1
    fi
    "${compose[@]}" exec -T backend touch /e2e/router-restart-done || exit $?
  fi
  sleep 2
done
wait "$runner_pid"

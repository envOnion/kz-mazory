#!/usr/bin/env bash
# Real Q2 restart at a durable between-chunk checkpoint in the isolated stack.
set -uo pipefail
if [[ -z "${CI:-}" && "${E2E_DOCKER_CONTEXT:-}" != "kk-minsk" ]]; then
  echo 'Local E2E requires E2E_DOCKER_CONTEXT=kk-minsk' >&2
  exit 1
fi
docker_args=()
if [[ -n "${E2E_DOCKER_CONTEXT:-}" ]]; then
  docker_args=(--context "$E2E_DOCKER_CONTEXT")
fi
docker "${docker_args[@]}" compose -f compose.e2e.yml up --build --no-deps --abort-on-container-exit --exit-code-from browser browser &
runner_pid=$!
while kill -0 "$runner_pid" 2>/dev/null; do
  if docker "${docker_args[@]}" compose -f compose.e2e.yml exec -T backend test -f /e2e/thread-restart-required 2>/dev/null &&
     ! docker "${docker_args[@]}" compose -f compose.e2e.yml exec -T backend test -f /e2e/thread-restart-done 2>/dev/null; then
    if ! docker "${docker_args[@]}" compose -f compose.e2e.yml restart qcluster; then
      kill "$runner_pid"; wait "$runner_pid"; exit 1
    fi
    docker "${docker_args[@]}" compose -f compose.e2e.yml exec -T backend touch /e2e/thread-restart-done
  fi
  sleep 2
done
wait "$runner_pid"

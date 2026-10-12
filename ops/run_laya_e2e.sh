#!/usr/bin/env bash
set -euo pipefail
if [[ "${E2E_DOCKER_CONTEXT:-}" != kk-minsk ]]; then
  echo 'LAYA E2E requires E2E_DOCKER_CONTEXT=kk-minsk' >&2; exit 1
fi
task_project="${LAYA_E2E_PROJECT:-mazory-laya-e2e}"
[[ "$task_project" =~ ^mazory-laya-e2e(-v[0-9]+)?$ ]] || exit 1
compose=(docker --context kk-minsk compose -p "$task_project" -f compose.e2e.yml -f compose.e2e.real-model.yml -f compose.e2e.laya.yml)
for relay in llm-relay read-relay; do
  "${compose[@]}" exec -T "$relay" python -c 'import socket,os; socket.create_connection((os.environ["MAZORY_E2E_RELAY_BIND"],11435 if os.environ.get("MAZORY_E2E_REAL_MODEL")=="1" else 11436),timeout=2).close()'
done
run_suite() {
  "${compose[@]}" run --rm --no-deps -e MAZORY_E2E_QUALITY="${MAZORY_E2E_QUALITY:-0}" browser node node_modules/@playwright/test/cli.js test --config=playwright.local.config.ts --max-failures=1 "$@" &
  runner_pid=$!
  while kill -0 "$runner_pid" 2>/dev/null; do
    if "${compose[@]}" exec -T backend test -f /e2e/laya-restart-required 2>/dev/null &&
       ! "${compose[@]}" exec -T backend test -f /e2e/laya-restart-done 2>/dev/null; then
      docker --context kk-minsk restart --timeout 10 "${task_project}-qcluster-1"
      "${compose[@]}" exec -T backend touch /e2e/laya-restart-done
    fi
    sleep 2
  done
  wait "$runner_pid"
}
run_suite laya-file-local.spec.ts laya-routing-local.spec.ts
run_suite laya-faults-local.spec.ts
if [[ "${MAZORY_E2E_QUALITY:-0}" == 1 ]]; then
  run_suite laya-quality-local.spec.ts
fi

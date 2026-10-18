#!/usr/bin/env bash
# Real Q2 restart at a durable between-chunk checkpoint in the isolated stack.
set -uo pipefail
if [[ -z "${CI:-}" && "${E2E_DOCKER_CONTEXT:-}" != "kk-minsk" && "${E2E_DOCKER_CONTEXT:-}" != "socktainer" ]]; then
  echo 'Local E2E requires E2E_DOCKER_CONTEXT=kk-minsk (or socktainer for a local Apple Container engine)' >&2
  exit 1
fi
compose_args=(-f compose.e2e.yml)
if [[ "${E2E_DOCKER_CONTEXT:-}" == "socktainer" ]]; then
  export E2E_HOST_UID="$(id -u)" E2E_HOST_GID="$(id -g)"
  e2e_runtime="${E2E_RUNTIME_DIR:-.e2e-runtime}"
  mkdir -p "$e2e_runtime/fixtures" "$e2e_runtime/static" "$e2e_runtime/avatars"
  touch "$e2e_runtime/container.marker"
  chmod 777 "$e2e_runtime/fixtures" "$e2e_runtime/static" "$e2e_runtime/avatars"
  compose_args+=(-f compose.e2e.socktainer.yml)
fi
docker_args=()
if [[ -n "${E2E_DOCKER_CONTEXT:-}" ]]; then
  docker_args=(--context "$E2E_DOCKER_CONTEXT")
fi
# Socktainer shares the real checkpoint files through virtiofs. Avoid opening
# thousands of short-lived exec sessions just to stat them (the runtime retains
# kqueue descriptors). Other engines retain their container-side checks.
has_checkpoint() {
  if [[ "${E2E_DOCKER_CONTEXT:-}" == "socktainer" ]]; then
    [[ -f "$e2e_runtime/fixtures/$1" ]]
  else
    docker "${docker_args[@]}" compose "${compose_args[@]}" exec -T backend test -f "/e2e/$1" 2>/dev/null
  fi
}
mark_checkpoint() {
  if [[ "${E2E_DOCKER_CONTEXT:-}" == "socktainer" ]]; then
    touch "$e2e_runtime/fixtures/$1"
  else
    docker "${docker_args[@]}" compose "${compose_args[@]}" exec -T backend touch "/e2e/$1"
  fi
}
docker "${docker_args[@]}" compose "${compose_args[@]}" exec -T backend \
  python manage.py shell -c 'from e2e.messenger import seed; seed()' || exit $?
export E2E_RUN_ID="$(docker "${docker_args[@]}" compose "${compose_args[@]}" exec -T backend python -c 'import uuid; print(uuid.uuid4())')"
[[ -n "$E2E_RUN_ID" ]] || exit 1
export E2E_BROWSER_OUTPUT="/e2e/playwright/run-$E2E_RUN_ID"
docker "${docker_args[@]}" compose "${compose_args[@]}" up --build --no-deps --abort-on-container-exit --exit-code-from browser browser &
runner_pid=$!
while kill -0 "$runner_pid" 2>/dev/null; do
  for checkpoint in thread clarification sale-clarification; do
    if has_checkpoint "${checkpoint}-restart-required" && ! has_checkpoint "${checkpoint}-restart-done"; then
      if ! docker "${docker_args[@]}" compose "${compose_args[@]}" restart -t 10 qcluster; then
        kill "$runner_pid"; wait "$runner_pid"; exit 1
      fi
      mark_checkpoint "${checkpoint}-restart-done" || { kill "$runner_pid"; wait "$runner_pid"; exit 1; }
    fi
  done
  sleep 2
done
wait "$runner_pid" || exit $?
docker "${docker_args[@]}" compose "${compose_args[@]}" exec -T backend python -c \
  'import sys; from e2e.results import verify; verify(sys.argv[1], sys.argv[2])' \
  "$E2E_BROWSER_OUTPUT" "$E2E_RUN_ID" || exit $?


# Profile images must survive replacement of both application containers.
docker "${docker_args[@]}" compose "${compose_args[@]}" up -d --no-deps --force-recreate --wait backend nginx || exit $?
docker "${docker_args[@]}" compose "${compose_args[@]}" run --rm --no-deps \
  -e MAZORY_AVATAR_RECREATED=1 -e MAZORY_E2E_OUTPUT="$E2E_BROWSER_OUTPUT/avatar-recreated" browser \
  node e2e/run.cjs avatar-local.spec.ts \
  --grep 'persists after backend and nginx'
docker "${docker_args[@]}" compose "${compose_args[@]}" exec -T backend python -c \
  'import sys; from e2e.results import verify; verify(sys.argv[1], sys.argv[2], expected=1)' \
  "$E2E_BROWSER_OUTPUT/avatar-recreated" "$E2E_RUN_ID"

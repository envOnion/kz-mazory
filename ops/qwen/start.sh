#!/bin/sh
set -eu
printf '%s  %s\n' f5f1dd8920d417aac2718b0bda3403da274301efdd6760b4f0f4b864ff2ad57d /models/qwen3.8-27b.gguf | sha256sum --check --status
exec /app/llama-server "$@"

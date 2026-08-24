#!/bin/sh
set -eu

rm -f /tmp/ollama-models-ready

echo "Starting Ollama server..."
ollama serve &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' INT TERM EXIT

echo "Waiting for the Ollama API..."
until ollama list >/dev/null 2>&1; do
    sleep 1
done

echo "Ensuring LLM model is available: ${OLLAMA_MODEL}"
ollama pull "${OLLAMA_MODEL}"

echo "Ensuring embedding model is available: ${OLLAMA_EMBEDDING_MODEL}"
ollama pull "${OLLAMA_EMBEDDING_MODEL}"

touch /tmp/ollama-models-ready
echo "Ollama models are ready."

wait "$server_pid"

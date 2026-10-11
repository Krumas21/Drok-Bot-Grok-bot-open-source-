#!/usr/bin/env sh
# First-time setup: creates .env with random secrets and the data/workspace folders (owned by you, so the
# containers - which run as uid 1000 - can write to them).
set -eu
cd "$(dirname "$0")/.."

rand() { od -An -N24 -tx1 /dev/urandom | tr -d ' \n'; }

if [ -f .env ]; then
  echo ".env already exists - leaving it alone."
else
  cp .env.example .env
  token=$(rand)
  password=$(rand | cut -c1-20)
  sed -i.bak "s/^COMPUTER_TOKEN=.*/COMPUTER_TOKEN=$token/; s/^DROK_ADMIN_PASSWORD=.*/DROK_ADMIN_PASSWORD=$password/" .env
  rm -f .env.bak
  echo "Created .env"
  echo "  Web UI password: $password   (change DROK_ADMIN_PASSWORD in .env any time)"
fi

mkdir -p data workspace
if [ "$(id -u)" != "1000" ]; then
  echo "Note: containers run as uid 1000. If they can't write ./data or ./workspace, run:"
  echo "  sudo chown -R 1000:1000 data workspace"
fi

echo
echo "Next:"
echo "  1. Add at least one LLM API key to .env (OPENAI_API_KEY, ANTHROPIC_API_KEY, OPENROUTER_API_KEY, ...)"
echo "     and set DROK_DEFAULT_MODEL to a model from that provider."
echo "  2. Optional: TELEGRAM_BOT_TOKEN from @BotFather."
echo "  3. docker compose up -d --build"
echo "  4. Open http://localhost:8080"

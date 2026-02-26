#!/usr/bin/env bash
set -euo pipefail

alembic -c infra/alembic.ini upgrade head

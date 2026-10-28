#!/usr/bin/env bash
# Source before bulk git: refuse filter-branch / filter-repo.
for a in "$@"; do
  case "$a" in filter-branch|filter-repo) echo "REFUSED: history rewrite banned on finance desk (ops/NO_HISTORY_REWRITE.md)"; exit 2;; esac
done

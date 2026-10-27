#!/bin/sh
# Extract translatable strings, update the catalogs, compile them.
# Safe to run repeatedly: existing translations are preserved.
set -eu

cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}

"$PY" -m babel.messages.frontend extract -F babel.cfg -k N_ -o app/i18n/messages.pot \
    --project="Festival Planner" --copyright-holder="Festival Planner contributors" app

for lang in en de; do
    catalog="app/i18n/$lang/LC_MESSAGES/messages.po"
    if [ -f "$catalog" ]; then
        "$PY" -m babel.messages.frontend update -i app/i18n/messages.pot \
            -d app/i18n -l "$lang" --no-fuzzy-matching
    else
        "$PY" -m babel.messages.frontend init -i app/i18n/messages.pot \
            -d app/i18n -l "$lang"
    fi
done

"$PY" -m babel.messages.frontend compile -d app/i18n --use-fuzzy
echo "catalogs compiled"

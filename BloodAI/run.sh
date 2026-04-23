#!/bin/bash
# Quick launcher — caffeinate prevents Mac sleep while bot runs
cd "$(dirname "$0")"
exec caffeinate -dims ./venv/bin/python bot.py "$@"

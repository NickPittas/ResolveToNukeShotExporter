#!/bin/zsh
# Installs the Python Workflow Integration for the current Resolve installation.
# Run from Finder (right-click > Open) or Terminal; it prompts for administrator
# permission only because Blackmagic documents a system-wide plugin location.

set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
source_file="$script_dir/ResolveToNukeShotExporter.py"
target_dir="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins"
target_file="$target_dir/ResolveToNukeShotExporter.py"

if [[ ! -f "$source_file" ]]; then
  echo "Missing: $source_file" >&2
  exit 1
fi

sudo mkdir -p "$target_dir"
sudo install -m 644 "$source_file" "$target_file"

echo "Installed: $target_file"
echo "Quit and reopen DaVinci Resolve, then launch Workspace > Workflow Integrations > ResolveToNukeShotExporter."

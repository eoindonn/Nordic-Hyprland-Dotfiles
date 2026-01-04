#!/usr/bin/env bash

# Directory with wallpapers
WALLPAPER_DIR="$HOME/.config/hypr/wallpapers"

# File to store current index
INDEX_FILE="$HOME/.cache/current_wallpaper_index"

# List of wallpapers
WALLPAPERS=("$WALLPAPER_DIR"/*)

# Get current index or start at 0
if [[ -f "$INDEX_FILE" ]]; then
    INDEX=$(<"$INDEX_FILE")
else
    INDEX=0
fi

# Run the wallpaper command
awww img "${WALLPAPERS[$INDEX]}" --transition-type outer --transition-pos 0.71,0.99 --transition-fps 120

# Increment index (loop back if at end)
INDEX=$(( (INDEX + 1) % ${#WALLPAPERS[@]} ))

# Save new index
echo "$INDEX" > "$INDEX_FILE"

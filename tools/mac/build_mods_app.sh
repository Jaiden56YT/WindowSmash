#!/bin/bash
# Builds build-mac/OpenSmash Ultimate Mods.app (tools/mac/ModsShell.swift: a window around the mod manager,
# tools/modmanager/server.py) with the Command Line Tools' Swift, and puts it in ~/Applications as
# "OpenSmash Ultimate Mods" (Spotlight finds it): moved there, so there's one app, not a second copy
# left in build-mac. Run from anywhere.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
app="$root/build-mac/OpenSmash Ultimate Mods.app"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
swiftc -parse-as-library -O -swift-version 5 -target arm64-apple-macos14.0 \
    "$root/tools/mac/ModsShell.swift" -o "$app/Contents/MacOS/OpenSmash Ultimate Mods"
# its icon, from the mod manager's own (tools/modmanager/ui/icon.png)
set_dir="$(mktemp -d)/windowsmash.iconset"
mkdir -p "$set_dir"
for s in 16 32 128 256 512; do
    sips -z $s $s "$root/tools/modmanager/ui/icon.png" --out "$set_dir/icon_${s}x${s}.png" >/dev/null
    sips -z $((s * 2)) $((s * 2)) "$root/tools/modmanager/ui/icon.png" --out "$set_dir/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$set_dir" -o "$app/Contents/Resources/windowsmash.icns" || true
rm -rf "$(dirname "$set_dir")"
cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>OpenSmash Ultimate Mods</string>
  <key>CFBundleDisplayName</key><string>OpenSmash Ultimate Mods</string>
  <key>CFBundleExecutable</key><string>OpenSmash Ultimate Mods</string>
  <key>CFBundleIdentifier</key><string>io.github.sno2207.opensmash.mods</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleIconFile</key><string>windowsmash</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>WindowSmashRoot</key><string>$root</string>
</dict></plist>
PLIST
codesign --force --sign - "$app" 2>/dev/null || true
mkdir -p "$HOME/Applications"
rm -rf "$HOME/Applications/OpenSmash Ultimate Mods.app"
# the app's old name (WindowSmash Mods), when it's this folder's: one app, not two (another folder's is left alone)
old="$HOME/Applications/WindowSmash Mods.app"
if [ -d "$old" ] && [ "$(/usr/libexec/PlistBuddy -c 'Print :WindowSmashRoot' "$old/Contents/Info.plist" 2>/dev/null)" = "$root" ]; then
    rm -rf "$old"
fi
mv "$app" "$HOME/Applications/OpenSmash Ultimate Mods.app"
echo "built: ~/Applications/OpenSmash Ultimate Mods.app"

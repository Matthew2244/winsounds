#!/bin/sh
# Builds WinSounds.app (the settings window) into build/, then installs it
# to ~/Applications. No admin needed; ad-hoc signed.
set -e
cd "$(dirname "$0")"
APP=build/WinSounds.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/mappings"
/usr/bin/swiftc -O -parse-as-library -o "$APP/Contents/MacOS/WinSounds" app/WinSoundsApp.swift
cp winsounds.py "$APP/Contents/Resources/winsounds.py"
cp mappings/*.json "$APP/Contents/Resources/mappings/"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>WinSounds</string>
  <key>CFBundleDisplayName</key><string>WinSounds</string>
  <key>CFBundleIdentifier</key><string>com.matthew.winsounds.app</string>
  <key>CFBundleExecutable</key><string>WinSounds</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
codesign --force -s - "$APP" >/dev/null 2>&1 || true
mkdir -p "$HOME/Applications"
rm -rf "$HOME/Applications/WinSounds.app"
cp -R "$APP" "$HOME/Applications/WinSounds.app"
echo "Built and installed $HOME/Applications/WinSounds.app"

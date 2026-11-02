#!/bin/bash
# One-time setup on your Mac: keeps the JobApps Chrome extension up to date by itself.
# Served by your JobApps server as /mac-setup.sh with its address filled in; run it with
#   curl -fsSL __SERVER__/mac-setup.sh | bash
# It puts the extension in ~/JobApps/jobapps-extension and adds a small background job (a LaunchAgent)
# that re-downloads it from your server every 15 minutes. The extension reloads itself when the
# files change. Undo with:  bash ~/JobApps/uninstall.sh
set -euo pipefail

SERVER="__SERVER__"
DIR="$HOME/JobApps"
AGENT="$HOME/Library/LaunchAgents/com.jobapps.extension-sync.plist"
mkdir -p "$DIR" "$HOME/Library/LaunchAgents"

cat > "$DIR/sync.sh" <<EOF
#!/bin/bash
# Copies the latest JobApps extension from your home server into ~/JobApps/jobapps-extension.
# Offline or server down: does nothing and tries again next time.
set -uo pipefail
# Ask for the build fingerprint first (a few bytes); download only when it differs from what's installed.
LATEST="\$(curl -fsS --max-time 20 "$SERVER/jobapps-extension.build")" || exit 0
grep -qF "\"version_name\": \"\$LATEST\"" "$DIR/jobapps-extension/manifest.json" 2>/dev/null && exit 0
TMP="\$(mktemp -d)"
trap 'rm -rf "\$TMP"' EXIT
curl -fsS --max-time 60 "$SERVER/jobapps-extension.zip" -o "\$TMP/x.zip" || exit 0
unzip -q -o "\$TMP/x.zip" -d "\$TMP/new" || exit 0
NEW="\$TMP/new/jobapps-extension"
[ -f "\$NEW/manifest.json" ] || exit 0
mkdir -p "$DIR/jobapps-extension"
# Everything else first; manifest.json last, because a new manifest is what tells Chrome to reload.
rsync -rc --delete --exclude manifest.json "\$NEW/" "$DIR/jobapps-extension/"
cmp -s "\$NEW/manifest.json" "$DIR/jobapps-extension/manifest.json" || cp "\$NEW/manifest.json" "$DIR/jobapps-extension/manifest.json"
EOF
chmod +x "$DIR/sync.sh"

cat > "$DIR/uninstall.sh" <<EOF
#!/bin/bash
launchctl bootout "gui/\$(id -u)" "$AGENT" 2>/dev/null || true
rm -f "$AGENT"
echo "Stopped automatic updates. Remove JobApps in chrome://extensions, then delete $DIR if you like."
EOF
chmod +x "$DIR/uninstall.sh"

cat > "$AGENT" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.jobapps.extension-sync</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$DIR/sync.sh</string></array>
  <key>StartInterval</key><integer>900</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>$DIR/sync.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$AGENT" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$AGENT"
bash "$DIR/sync.sh"

if [ -f "$DIR/jobapps-extension/manifest.json" ]; then
  echo
  echo "Done. The extension is in $DIR/jobapps-extension and will update itself every 15 minutes."
  echo "Last step (once): in chrome://extensions, remove the old JobApps, click Load unpacked and pick that folder."
  open "$DIR"
else
  echo "Couldn't reach $SERVER. Is Tailscale on? Then run this command again." >&2
  exit 1
fi

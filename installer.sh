#!/bin/zsh
# Lance surveillance.py toutes les 5 minutes via launchd (le script sort aussitôt hors séance US).
# Usage : ./installer.sh        installe ou réinstalle
#         ./installer.sh retirer désinstalle
set -e
DOSSIER="${0:A:h}"
LABEL="com.analyse-marche.surveillance"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
if [[ "$1" == "retirer" ]]; then
  rm -f "$PLIST"
  echo "Surveillance désinstallée."
  exit 0
fi

mkdir -p "$DOSSIER/logs" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$DOSSIER/.venv/bin/python</string>
    <string>$DOSSIER/surveillance.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DOSSIER</string>
  <key>StartInterval</key><integer>300</integer>
  <key>StandardOutPath</key><string>$DOSSIER/logs/surveillance.log</string>
  <key>StandardErrorPath</key><string>$DOSSIER/logs/surveillance.log</string>
</dict>
</plist>
EOF
launchctl bootstrap "gui/$UID" "$PLIST"
echo "Surveillance installée : scan toutes les 5 min pendant la séance US (15h30-22h heure de Paris)."

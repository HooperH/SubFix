#!/bin/bash
# Keep the familiar app entry point and reuse the auditable uninstall backend.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_DIR="${OUTPUT_DIR:-$(pwd)}"
APP_PATH="$OUTPUT_DIR/${APP_NAME:-卸载_SubFix.app}"
SOURCE="$SCRIPT_DIR/卸载_SubFix.command"
mkdir -p "$OUTPUT_DIR"
if [[ -e "$APP_PATH" ]]; then
  echo "卸载构建目录包含旧应用，请使用新的输出目录。" >&2
  exit 1
fi
bash -n "$SOURCE"
/usr/bin/osacompile -o "$APP_PATH" <<'APPLESCRIPT'
on run
  try
    set scriptPath to POSIX path of (path to resource "uninstall.sh")
    set userHome to POSIX path of (path to home folder)
    set baseCommand to "/usr/bin/env HOME=" & quoted form of userHome & " /bin/bash " & quoted form of scriptPath
    set previewText to do shell script (baseCommand & " --dry-run")
    display dialog previewText with title "卸载 SubFix" buttons {"取消", "卸载"} default button "取消" cancel button "取消" with icon caution
    set needsAdmin to do shell script (baseCommand & " --needs-admin")
    set uninstallCommand to "printf 'UNINSTALL\\n' | " & baseCommand
    if needsAdmin is "yes" then
      set resultText to do shell script uninstallCommand with administrator privileges
    else
      set resultText to do shell script uninstallCommand
    end if
    display dialog resultText with title "卸载 SubFix" buttons {"完成"} default button "完成"
  on error errorText number errorNumber
    if errorNumber is not -128 then
      display dialog "卸载未完成：" & errorText & return & "请检查目录权限后重试。" with title "卸载 SubFix" buttons {"关闭"} default button "关闭" with icon stop
    end if
  end try
end run
APPLESCRIPT
cp "$SOURCE" "$APP_PATH/Contents/Resources/uninstall.sh"
chmod 755 "$APP_PATH/Contents/Resources/uninstall.sh"
# Adding resources invalidates osacompile's signature; sign the finished bundle.
/usr/bin/codesign --force --sign - "$APP_PATH"
/usr/bin/codesign --verify --deep --strict "$APP_PATH"
echo "✅ 已生成卸载程序：$APP_PATH"

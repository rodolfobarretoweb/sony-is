#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p build/.pyinstaller-config build/.pip-cache
export PYINSTALLER_CONFIG_DIR="$PWD/build/.pyinstaller-config"
export PIP_CACHE_DIR="$PWD/build/.pip-cache"

if [ -z "${PYTHON_BIN:-}" ]; then
  for candidate in \
    /Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14 \
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13; do
    if [ -x "$candidate" ] && "$candidate" -c 'import tkinter; import _tkinter' >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
  PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
fi

PYTHON_VERSION="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
case "$PYTHON_VERSION" in
  3.13|3.14) ;;
  *) echo "Python 3.13 or 3.14 is required; found $PYTHON_VERSION at $PYTHON_BIN." >&2; exit 1 ;;
esac

if ! "$PYTHON_BIN" -c 'import tkinter; import _tkinter' >/dev/null 2>&1; then
  case "$(cd "$(dirname "$PYTHON_BIN")" && pwd -P)" in
    /opt/homebrew/*|/usr/local/*)
      echo "Installing the Tk binding for Homebrew Python $PYTHON_VERSION..."
      brew install "python-tk@$PYTHON_VERSION"
      ;;
    *)
      echo "Tk is missing from $PYTHON_BIN. Install a Tk-enabled Python or set PYTHON_BIN to one." >&2
      exit 1
      ;;
  esac
fi
"$PYTHON_BIN" -c 'import tkinter; import _tkinter' >/dev/null || {
  echo "Tk is still unavailable in $PYTHON_BIN after setup." >&2
  exit 1
}
command -v ffmpeg >/dev/null || { echo 'ffmpeg is required on the build machine.' >&2; exit 1; }
command -v ffprobe >/dev/null || { echo 'ffprobe is required on the build machine.' >&2; exit 1; }

BUILD_VENV=".build-venv-$PYTHON_VERSION"
"$PYTHON_BIN" -m venv "$BUILD_VENV"
BUILD_PY="$BUILD_VENV/bin/python"
"$BUILD_PY" -m pip install -r requirements-build.txt
"$BUILD_PY" -m unittest -q test_stabilize test_app
"$BUILD_PY" -m PyInstaller --noconfirm --clean --onedir --windowed \
  --name 'Sony Video Stabilizer' \
  --osx-bundle-identifier 'com.sonygyro.stabilizer' \
  --add-binary "$(command -v ffmpeg):." \
  --add-binary "$(command -v ffprobe):." \
  app.py

if [ ! -d 'dist/Sony Video Stabilizer.app' ]; then
  echo 'The macOS application bundle was not created.' >&2
  exit 1
fi
"dist/Sony Video Stabilizer.app/Contents/MacOS/Sony Video Stabilizer" --worker --help >/dev/null

rm -rf build/dmg
mkdir -p 'build/dmg'
cp -R 'dist/Sony Video Stabilizer.app' 'build/dmg/'
ln -sfn /Applications 'build/dmg/Applications'
DMG="dist/Sony-Video-Stabilizer-macos-$(uname -m).dmg"
if hdiutil create -ov -volname 'Sony Video Stabilizer' -srcfolder 'build/dmg' "$DMG"; then
  echo "Created $DMG"
else
  if [ -e "$DMG" ]; then
    mv "$DMG" "build/previous-$(basename "$DMG")"
  fi
  ZIP="dist/Sony-Video-Stabilizer-macos-$(uname -m).zip"
  echo 'Disk-image creation is unavailable; packaging the app as a ZIP instead.'
  ditto -c -k --sequesterRsrc --keepParent 'dist/Sony Video Stabilizer.app' "$ZIP"
  echo "Created $ZIP"
fi

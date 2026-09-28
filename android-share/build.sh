#!/usr/bin/env bash
# Manual APK build without Gradle: aapt2 -> javac -> d8 -> inject dex -> zipalign -> apksigner
set -euo pipefail

SDK=/f/claudework/douyin-video-agent/tools/android-sdk
BT="$SDK/build-tools/34.0.0"
PLATFORM="$SDK/platforms/android-34/android.jar"
JAVA_HOME="/c/Program Files/Eclipse Adoptium/jdk-17.0.20.101-hotspot"
export JAVA_HOME
export PATH="$JAVA_HOME/bin:$PATH"

OUT=build
APK_NAME=VideoMindShare.apk

rm -rf "$OUT"
mkdir -p "$OUT/gen" "$OUT/obj" "$OUT/dex" "$OUT/apk"

echo "[1/7] aapt2 compile resources"
"$BT/aapt2.exe" compile --dir res -o "$OUT/res.zip"

echo "[2/7] aapt2 link -> base.apk"
"$BT/aapt2.exe" link -o "$OUT/apk/base.apk" \
  -I "$PLATFORM" \
  --manifest AndroidManifest.xml \
  --java "$OUT/gen" \
  --auto-add-overlay \
  "$OUT/res.zip"

echo "[3/7] javac (--release 11, classpath android.jar)"
find src -name '*.java' > "$OUT/sources.txt"
javac --release 11 -encoding UTF-8 \
  -classpath "$PLATFORM" \
  -d "$OUT/obj" \
  @"$OUT/sources.txt"

echo "[4/7] d8 -> classes.dex"
"$BT/d8.bat" --lib "$PLATFORM" --release --min-api 24 \
  --output "$OUT/dex" \
  $(find "$OUT/obj" -name '*.class')

echo "[5/7] inject classes.dex into apk (python zipfile)"
cp "$OUT/apk/base.apk" "$OUT/apk/unsigned.apk"
python - "$OUT/dex/classes.dex" "$OUT/apk/unsigned.apk" <<'PY'
import sys
import zipfile
dex, apk = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(apk, "a", zipfile.ZIP_DEFLATED) as z:
    z.write(dex, "classes.dex")
print("injected classes.dex")
PY

echo "[6/7] zipalign -f 4"
"$BT/zipalign.exe" -f 4 "$OUT/apk/unsigned.apk" "$OUT/apk/aligned.apk"

echo "[7/7] apksigner sign"
"$BT/apksigner.bat" sign \
  --ks debug.keystore --ks-pass pass:android \
  --key-pass pass:android \
  --out "$OUT/apk/$APK_NAME" \
  "$OUT/apk/aligned.apk"

"$BT/apksigner.bat" verify "$OUT/apk/$APK_NAME"
echo "DONE: $OUT/apk/$APK_NAME"

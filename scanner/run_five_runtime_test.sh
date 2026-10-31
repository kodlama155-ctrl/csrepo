#!/usr/bin/env bash
# Called INSIDE Android Emulator Runner after adb reports boot completed.
# Five real .cs3 plugin downloads and provider functions execute in CloudStream.
set -uo pipefail

cd cloudstream || exit 2
chmod +x ./gradlew
echo "Running five-plugin CloudStream Android runtime checks..."
echo "This can take time on an empty Gradle cache. Per-provider limits: 45s load + 50s runtime."
timeout --signal=TERM --kill-after=10s 1500s \
  ./gradlew :app:connectedDebugAndroidTest \
  -Pandroid.testInstrumentationRunnerArguments.class=com.lagradost.cloudstream3.CsrepoFivePluginTest \
  --no-daemon --console=plain > ../five-provider-gradle.log 2>&1
GRADLE_STATUS=$?
cd ..

echo "Gradle exit: ${GRADLE_STATUS}"
echo "Last 80 Gradle log lines:"
tail -n 80 five-provider-gradle.log || true
adb logcat -d -v brief -s CSREPO_E2E:I '*:S' > five-provider-logcat.txt || true

python3 scanner/parse_five_runtime_test.py
PARSE_STATUS=$?
echo "Parse exit: ${PARSE_STATUS}; Gradle exit: ${GRADLE_STATUS}"
if [ "${GRADLE_STATUS}" -ne 0 ]; then
  echo "CloudStream Android build/test did not complete successfully"
  exit 1
fi
exit "${PARSE_STATUS}"

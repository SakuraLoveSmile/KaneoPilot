#!/bin/bash
# Non-published, same-key APKs for a single isolated emulator upgrade. Builds run only on hosted Actions.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
sh "$ROOT/scripts/require_actions.sh"
python3 - "$ROOT" <<'PY'
import os, pathlib, re, subprocess, sys
number = os.environ.get('BUILD_NUMBER', '')
source = os.environ.get('SOURCE_SHA', '')
output = os.environ.get('KANPILOT_ANDROID_UPDATE_OUTPUT', '')
reports = os.environ.get('KANPILOT_ANDROID_UPDATE_REPORTS', '')
assert re.fullmatch(r'[1-9][0-9]*', number), 'BUILD_NUMBER must be a positive run number'
assert int(number) * 100 + 11 <= 2147483647, 'Acceptance versionCode would exceed Android bounds'
assert re.fullmatch(r'[0-9a-fA-F]{40}', source), 'SOURCE_SHA must identify the exact source commit'
assert source.lower() == subprocess.check_output(['git', '-C', sys.argv[1], 'rev-parse', 'HEAD'], text=True).strip().lower(), 'SOURCE_SHA differs from the compiled checkout'
folder = pathlib.Path(output)
assert output and folder.is_absolute(), 'KANPILOT_ANDROID_UPDATE_OUTPUT must be an absolute output directory'
assert not folder.exists() or (folder.is_dir() and not any(folder.iterdir())), 'Acceptance output directory must be empty'
folder.mkdir(parents=True, exist_ok=True)
report_folder = pathlib.Path(reports)
assert reports and report_folder.is_absolute(), 'KANPILOT_ANDROID_UPDATE_REPORTS must be an absolute reports directory'
assert report_folder != folder, 'Acceptance reports must be separate from APK output'
assert not report_folder.exists() or (report_folder.is_dir() and not any(report_folder.iterdir())), 'Acceptance reports directory must be empty'
report_folder.mkdir(parents=True, exist_ok=True)
PY
OUTPUT="$KANPILOT_ANDROID_UPDATE_OUTPUT"
BASELINE_CODE=$((BUILD_NUMBER * 100 + 10))
NEWER_CODE=$((BUILD_NUMBER * 100 + 11))
cd "$ROOT/android"

# Copy each APK before the next version changes generated BuildConfig and merged manifests.
bash ./gradlew --no-daemon --stacktrace -PupdateAcceptanceTests=true -PversionName=0.2.10 -PversionCode="$NEWER_CODE" assembleUpdateAcceptance
cp app/build/outputs/apk/updateAcceptance/app-updateAcceptance.apk "$OUTPUT/newer.apk"
bash ./gradlew --no-daemon --stacktrace -PupdateAcceptanceTests=true -PversionName=0.2.9 -PversionCode="$BASELINE_CODE" assembleUpdateAcceptance assembleUpdateAcceptanceAndroidTest
cp app/build/outputs/apk/updateAcceptance/app-updateAcceptance.apk "$OUTPUT/baseline.apk"
cp app/build/outputs/apk/androidTest/updateAcceptance/app-updateAcceptance-androidTest.apk "$OUTPUT/test.apk"

python3 - <<'PY'
import hashlib, json, os, pathlib, re, subprocess, xml.etree.ElementTree as ET
output = pathlib.Path(os.environ['KANPILOT_ANDROID_UPDATE_OUTPUT'])
reports = pathlib.Path(os.environ['KANPILOT_ANDROID_UPDATE_REPORTS'])
sdk = pathlib.Path(os.environ['ANDROID_HOME']) / 'build-tools' / '36.0.0'
apkanalyzer = pathlib.Path(os.environ['ANDROID_HOME']) / 'cmdline-tools' / 'latest' / 'bin' / 'apkanalyzer'
number = int(os.environ['BUILD_NUMBER'])

def capture_report(command, name):
    report = reports / name
    error_report = report.with_name(report.stem + '.stderr.txt')
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
    except OSError as error:
        report.write_text('', encoding='utf-8')
        error_report.write_text(str(error) + '\n', encoding='utf-8')
        raise
    report.write_text(result.stdout, encoding='utf-8')
    error_report.write_text(result.stderr, encoding='utf-8')
    return result

tool_results = {}
for role in ('baseline', 'newer', 'test'):
    path = output / (role + '.apk')
    tool_results[role] = (
        capture_report([str(sdk / 'aapt'), 'dump', 'badging', str(path)], role + '-badging.txt'),
        capture_report([str(sdk / 'apksigner'), 'verify', '--verbose', '--print-certs', str(path)], role + '-signature.txt'),
    )
test_manifest = capture_report([str(apkanalyzer), 'manifest', 'print', str(output / 'test.apk')], 'test-manifest.xml')
for results in tool_results.values():
    for result in results:
        result.check_returncode()
test_manifest.check_returncode()

records = {}
for role in ('baseline', 'newer', 'test'):
    path = output / (role + '.apk')
    badging, signature = (result.stdout for result in tool_results[role])
    package = re.search(r"^package: name='([^']+)' versionCode='([^']*)' versionName='([^']*)'", badging, re.M)
    assert package, role + ': package metadata missing'
    certs = re.findall(r'^Signer #\d+ certificate SHA-256 digest: ([0-9a-fA-F]{64})$', signature, re.M)
    assert len(certs) == 1, role + ': expected one verified signing certificate'
    raw_version_code = package[2]
    if role == 'test':
        assert raw_version_code == '' or re.fullmatch(r'[0-9]+', raw_version_code), 'Acceptance test versionCode is not numeric'
        version_code = int(raw_version_code) if raw_version_code else None
    else:
        assert re.fullmatch(r'[1-9][0-9]*', raw_version_code), role + ': versionCode must be a positive integer'
        version_code = int(raw_version_code)
    record = {'name': path.name, 'package_name': package[1], 'version_name': package[3], 'version_code': version_code,
              'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'size': path.stat().st_size,
              'certificate_sha256': certs[0].lower(), 'source_sha': os.environ['SOURCE_SHA'].lower()}
    if role == 'test':
        assert package[1] == 'com.sakurasep.kaneopilot.test', 'Acceptance test APK has another package name'
        manifest_root = ET.fromstring(test_manifest.stdout)
        instrumentation = list(manifest_root.iter('instrumentation'))
        assert manifest_root.tag == 'manifest' and len(instrumentation) == 1 and instrumentation[0] in list(manifest_root), 'Acceptance APK must declare exactly one instrumentation'
        android = '{http://schemas.android.com/apk/res/android}'
        runner = instrumentation[0].get(android + 'name')
        target_package = instrumentation[0].get(android + 'targetPackage')
        assert runner == 'androidx.test.runner.AndroidJUnitRunner', 'Acceptance test runner missing'
        assert target_package == 'com.sakurasep.kaneopilot', 'Acceptance instrumentation targets another package'
        record['target_package'] = target_package
        record['runner'] = runner
    else:
        assert package[1] == 'com.sakurasep.kaneopilot', role + ': wrong target package'
        expected_version, expected_code = ('0.2.9', number * 100 + 10) if role == 'baseline' else ('0.2.10', number * 100 + 11)
        assert package[3] == expected_version and version_code == expected_code, role + ': version identity drift'
        assert not re.search(r'^application-debuggable(?:\s|$)', badging, re.M), role + ': preview guard would be bypassed by a debuggable build'
    records[role] = record
assert len({entry['certificate_sha256'] for entry in records.values()}) == 1, 'Acceptance APKs were not signed by the same key'
manifest = {'schema_version': 1, 'purpose': 'isolated-android-update-acceptance-not-a-release',
            'source_sha': os.environ['SOURCE_SHA'].lower(), 'build_number': number,
            'package_name': 'com.sakurasep.kaneopilot', 'artifacts': records}
(output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
(output / 'SHA256SUMS').write_text(''.join(hashlib.sha256((output / name).read_bytes()).hexdigest() + '  ' + name + '\n'
                                        for name in ('baseline.apk', 'newer.apk', 'test.apk', 'manifest.json')), encoding='utf-8')
print('Exported non-published update acceptance APKs, metadata and checksums:', output)
PY

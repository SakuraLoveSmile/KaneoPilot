#!/usr/bin/env python3
"""Build immutable release metadata and resume only this CI run's draft release.

`publish` writes to GitHub and, for a manual test build, `identity` reads the release
list. Both use GITHUB_TOKEN; local tests inject a fake API.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

# One version grammar for every platform (docs/build-release.md and the shared
# vectors in scripts/tests/fixtures/update-version-vectors.json). ASCII digits
# only, no leading zeros, at most nine digits per number.
NUMBER = r"(?:0|[1-9][0-9]{0,8})"
SERIAL = r"[1-9][0-9]{0,8}"
_STABLE = r"(%s)\.(%s)\.(%s)" % (NUMBER, NUMBER, NUMBER)
STABLE_VERSION = re.compile(_STABLE)
TEST_VERSION = re.compile(_STABLE + r"-test\.(%s)" % SERIAL)
STABLE_TAG = re.compile("v" + _STABLE)
TEST_TAG = re.compile("v" + _STABLE + r"-test\.(%s)" % SERIAL)
MAX_NOTES = 4000
MAX_BRANCH = 100
TAGGER = {"name": "github-actions[bot]", "email": "41898282+github-actions[bot]@users.noreply.github.com"}
# The only Mac package is Apple Silicon. Both in-app updaters accept exactly this name and architecture set
# (docs/build-release.md, "内置 Python 运行时与架构").
MACOS_PACKAGE_SUFFIX = "macos-arm64.dmg"
MACOS_ARCHITECTURES = ["arm64"]


def parse_version(text):
    """Return (major, minor, patch, serial); serial is None for a stable version."""
    for pattern in (STABLE_VERSION, TEST_VERSION):
        match = pattern.fullmatch(text) if isinstance(text, str) else None
        if match:
            numbers = tuple(int(part) for part in match.groups())
            return numbers if len(numbers) == 4 else numbers + (None,)
    raise ValueError("Invalid version: " + repr(text))


def version_key(text):
    """Order versions: a test build sorts below the stable version it is a candidate for."""
    major, minor, patch, serial = parse_version(text)
    return (major, minor, patch, 1, 0) if serial is None else (major, minor, patch, 0, serial)


def tag_channel(tag, build_number):
    """'stable' or 'test' for a publishable tag. A test serial is the run's build number."""
    if STABLE_TAG.fullmatch(tag):
        return "stable"
    match = TEST_TAG.fullmatch(tag)
    if match:
        if int(match.group(4)) != int(build_number):
            raise ValueError("Test tag serial must equal the build number")
        return "test"
    raise ValueError("Only stable vX.Y.Z or test vX.Y.Z-test.N tags can publish")


def clean_branch(value):
    """The source branch is display text only: printable ASCII, at most 100 characters."""
    text = "".join(char if " " <= char <= "~" else "?" for char in str(value or "")).strip()[:MAX_BRANCH]
    if not text:
        raise ValueError("Test builds require the source branch name")
    return text


def published_versions(api):
    """Latest stable release and the highest published test base, from read-only GitHub queries."""
    prefix = "/repos/" + api.repository
    latest = api.request("GET", prefix + "/releases/latest")
    stable = None
    if latest is not None:
        match = STABLE_TAG.fullmatch(latest.get("tag_name") or "")
        if match is None or latest.get("draft") is not False or latest.get("prerelease") is not False:
            raise ValueError("Current latest release is not a recognizable stable release")
        stable = tuple(int(part) for part in match.groups())
    highest = None
    for item in api.request("GET", prefix + "/releases?per_page=100") or []:
        match = TEST_TAG.fullmatch(item.get("tag_name") or "")
        if match and item.get("draft") is False and item.get("prerelease") is True:
            base = tuple(int(part) for part in match.groups()[:3])
            highest = base if highest is None else max(highest, base)
    return stable, highest


def manual_test_identity(api, source_sha):
    notes = os.environ.get("RELEASE_NOTES", "").strip()
    if not notes:
        raise ValueError("Test releases require release notes")
    if len(notes) > MAX_NOTES:
        raise ValueError("Release notes must be at most %d characters" % MAX_NOTES)
    serial = os.environ.get("GITHUB_RUN_NUMBER", "")
    if not re.fullmatch(SERIAL, serial):
        raise ValueError("The Actions run number is not a valid build number")
    requested = os.environ.get("RELEASE_BASE_VERSION", "").strip()
    if requested and not STABLE_VERSION.fullmatch(requested):
        raise ValueError("base_version must be a stable X.Y.Z version")
    if api is None:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise ValueError("GITHUB_TOKEN is required")
        api = GitHub(os.environ["GITHUB_REPOSITORY"], token)
    stable, highest = published_versions(api)
    if requested:
        base = tuple(int(part) for part in requested.split("."))
    else:
        candidates = ([(stable[0], stable[1], stable[2] + 1)] if stable else []) + ([highest] if highest else [])
        if not candidates:
            raise ValueError("Cannot determine base_version; set it explicitly")
        base = max(candidates)
    if base <= (0, 0, 0):
        raise ValueError("base_version must be above 0.0.0")
    if stable is not None and base <= stable:
        raise ValueError("base_version must be above the latest stable release; a test build sorts below its own stable version")
    if highest is not None and base < highest:
        raise ValueError("base_version must not be below the highest published test base")
    version = "%d.%d.%d-test.%s" % (base + (serial,))
    return {"release": "true", "version": version, "sha": source_sha, "channel": "test", "tag": "v" + version}


def identity(api=None):
    """Resolve a preview, an annotated main-contained stable tag, or a manually requested test build."""
    event = os.environ["GITHUB_EVENT_NAME"]
    ref_type = os.environ["GITHUB_REF_TYPE"]
    tag = os.environ["GITHUB_REF_NAME"]
    channel = os.environ.get("RELEASE_CHANNEL", "")
    if event == "workflow_dispatch" and ref_type != "branch":
        raise ValueError("Manual builds require a branch; rerun the original tag workflow to recover a release")
    if channel not in ("", "preview", "test"):
        raise ValueError("Unknown release channel")
    if channel and event != "workflow_dispatch":
        raise ValueError("A release channel can only be selected for a manual build")
    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    release = event == "push" and ref_type == "tag"
    if release:
        if TEST_TAG.fullmatch(tag):
            raise ValueError("Test releases are created only by a manual channel=test build; a pushed test tag is not a release")
        if not STABLE_TAG.fullmatch(tag):
            raise ValueError("Only stable vX.Y.Z tags are supported")
        ref = "refs/tags/" + tag
        kind = subprocess.check_output(["git", "cat-file", "-t", ref], text=True).strip()
        if kind != "tag":
            raise ValueError("Release requires an annotated tag")
        raw = subprocess.check_output(["git", "cat-file", "-p", ref], text=True)
        header, _, notes = raw.partition("\n\n")
        fields = dict(line.split(" ", 1) for line in header.splitlines())
        if fields.get("type") != "commit" or fields.get("object") != source_sha or fields.get("tag") != tag:
            raise ValueError("Annotated tag must directly name the build commit")
        if not notes.strip():
            raise ValueError("Annotated tag must include release notes")
        contained = subprocess.run(["git", "merge-base", "--is-ancestor", source_sha, "origin/main"], capture_output=True)
        if contained.returncode != 0:
            raise ValueError("Release commit must be contained in origin/main")
        result = {"release": "true", "version": tag[1:], "sha": source_sha}
    elif channel == "test":
        # A manual branch build only: the commit need not be in main, so the tag is made by publish.
        result = manual_test_identity(api, source_sha)
    else:
        if os.environ.get("RELEASE_BASE_VERSION", "").strip() or os.environ.get("RELEASE_NOTES", "").strip():
            raise ValueError("base_version and notes are only used by channel=test")
        result = {"release": "false", "version": "0.0.0", "sha": source_sha}
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for key, value in result.items():
            output.write(key + "=" + value + "\n")
    return result


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def metadata(args):
    path = Path(args.package)
    if not re.fullmatch(r"[0-9a-f]{64}", args.fingerprint):
        raise ValueError("Signature fingerprint must be lowercase SHA256")
    serial = parse_version(args.version)[3]
    if serial is not None and serial != int(args.build_number):
        raise ValueError("Test version serial must equal the build number")
    architectures = args.architectures.split(",")
    if args.platform == "macos" and architectures != MACOS_ARCHITECTURES:
        raise ValueError("macOS packages are Apple Silicon (arm64) only")
    write_json(args.output, {
        "platform": args.platform, "name": path.name,
        "sha256": sha256(path), "size": path.stat().st_size,
        "signature_sha256": args.fingerprint, "version": args.version,
        "source_sha": args.source_sha, "build_number": int(args.build_number),
        "architectures": architectures,
        "signature_type": "certificate-sha256" if args.platform == "android" else "codedirectory-sha256",
    })


def prepare(directory, tag, source_sha, build_number, branch=""):
    channel = tag_channel(tag, build_number)
    root = Path(directory)
    artifacts = []
    for platform in ("android", "macos"):
        record = json.loads((root / (platform + "-metadata.json")).read_text())
        expected = (platform, tag[1:], source_sha, int(build_number))
        actual = tuple(record.get(key) for key in ("platform", "version", "source_sha", "build_number"))
        if actual != expected:
            raise ValueError("Artifact identity mismatch: " + platform)
        name = record["name"]
        suffix = ".apk" if platform == "android" else ".dmg"
        if Path(name).name != name or not name.startswith("KaneoPilot-") or not name.endswith(suffix):
            raise ValueError("Unexpected artifact name")
        if name != "KaneoPilot-%s-%s" % (tag[1:], "android.apk" if platform == "android" else MACOS_PACKAGE_SUFFIX):
            # The in-app updaters only accept these exact names, so a misnamed build, stable or test, must not publish.
            raise ValueError("Unexpected artifact name")
        if platform == "macos" and record.get("architectures") != MACOS_ARCHITECTURES:
            raise ValueError("Unexpected macOS architectures")
        if not re.fullmatch(r"[0-9a-f]{64}", record.get("signature_sha256", "")):
            raise ValueError("Missing signature fingerprint")
        package = root / name
        if record["sha256"] != sha256(package) or record["size"] != package.stat().st_size:
            raise ValueError("Artifact content mismatch: " + name)
        artifacts.append(record)
    manifest = {"version": tag[1:], "tag": tag, "source_sha": source_sha,
                "build_number": int(build_number), "artifacts": artifacts,
                "macos_requirements": "macOS 14+, Apple Silicon only; bundled Python runtime; Git/Plus as configured; ad-hoc signed, not notarized"}
    if channel == "test":
        # Stable manifests keep their exact historical shape: no channel key.
        manifest.update({"channel": "test", "base_version": tag[1:].split("-", 1)[0], "branch": clean_branch(branch)})
    write_json(root / "manifest.json", manifest)
    names = [item["name"] for item in artifacts] + ["manifest.json"]
    (root / "SHA256SUMS").write_text("".join(sha256(root / name) + "  " + name + "\n" for name in names))
    return manifest


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise ValueError("Refusing non-HTTPS GitHub redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


class GitHub:
    def __init__(self, repository, token):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise ValueError("Invalid repository")
        self.repository, self.token = repository, token
        self.opener = urllib.request.build_opener(SafeRedirect())

    def request(self, method, path, data=None, binary=False, upload=None):
        url = ("https://uploads.github.com" if upload else "https://api.github.com") + path
        headers = {"Authorization": "Bearer " + self.token, "X-GitHub-Api-Version": "2022-11-28",
                   "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
                   "User-Agent": "KaneoPilot-release"}
        if upload:
            body = Path(upload).read_bytes()
            headers["Content-Type"] = "application/octet-stream"
        elif data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        else:
            body = None
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=120) as response:
                raw = response.read()
                return raw if binary else (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as error:
            if method == "GET" and error.code == 404:
                return None
            # Never include request headers, token or arbitrary server response in logs.
            raise RuntimeError("GitHub %s failed (%s): %s" % (method, error.code, path)) from None


def make_latest(api, prefix, tag):
    latest = api.request("GET", prefix + "/releases/latest")
    if latest is None:
        return "true"
    current = STABLE_TAG.fullmatch(latest.get("tag_name", ""))
    if current is None or latest.get("draft") is not False or latest.get("prerelease") is not False:
        raise ValueError("Current latest release is not a recognizable stable release")
    return str(tuple(map(int, STABLE_TAG.fullmatch(tag).groups())) > tuple(map(int, current.groups()))).lower()


def verify_remote_tag(api, prefix, reference, tag, source_sha):
    """Return the annotated tag's message once it is proven to name exactly the build commit."""
    target = reference["object"]
    if target.get("type") != "tag":
        raise ValueError("Release requires a remote annotated tag")
    annotation = api.request("GET", prefix + "/git/tags/" + target["sha"])
    if not annotation or annotation.get("tag") != tag:
        raise ValueError("Remote annotated tag identity mismatch")
    target = annotation["object"]
    if target.get("type") != "commit" or target.get("sha") != source_sha:
        raise ValueError("Remote tag no longer points to the build commit")
    notes = annotation.get("message", "").strip()
    if not notes:
        raise ValueError("Annotated tag must include release notes")
    return notes


def find_release(api, prefix, tag):
    release = api.request("GET", prefix + "/releases/tags/" + urllib.parse.quote(tag, safe=""))
    if release is not None:
        return release
    # The tag endpoint may omit drafts. Search authenticated releases before creating one.
    page = 1
    while True:
        releases = api.request("GET", prefix + "/releases?per_page=100&page=" + str(page))
        matches = [item for item in releases if item.get("tag_name") == tag]
        if len(matches) > 1:
            raise ValueError("Multiple releases use this tag")
        if matches:
            return matches[0]
        if len(releases) < 100:
            return None
        page += 1


def annotation_message(notes, branch, source_sha, build_number, run_url):
    """Public annotation of a test tag: the person's notes, then where the build came from."""
    lines = [notes.strip(), "", "Channel: test", "Branch: " + clean_branch(branch),
             "Commit: " + source_sha, "Build: " + str(int(build_number))]
    if run_url:
        lines.append("Run: " + run_url)
    return "\n".join(lines) + "\n"


def create_test_tag(api, prefix, tag, source_sha, message):
    created = api.request("POST", prefix + "/git/tags", {
        "tag": tag, "message": message, "object": source_sha, "type": "commit",
        "tagger": dict(TAGGER, date=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))})
    api.request("POST", prefix + "/git/refs", {"ref": "refs/tags/" + tag, "sha": created["sha"]})


def publish(api, directory, tag, source_sha, build_number, channel="stable", notes="", branch="", run_url=""):
    root = Path(directory)
    manifest = json.loads((root / "manifest.json").read_text())
    if (manifest.get("tag"), manifest.get("source_sha"), manifest.get("build_number")) != (tag, source_sha, int(build_number)):
        raise ValueError("Manifest identity mismatch")
    if tag_channel(tag, build_number) != channel:
        raise ValueError("Tag does not belong to the %s channel" % channel)
    if channel == "test" and not notes.strip():
        raise ValueError("Test releases require release notes")
    # Recompute from metadata, detecting changed files before any GitHub mutation.
    if prepare(root, tag, source_sha, build_number, branch) != manifest:
        raise ValueError("Manifest does not match build metadata")
    prefix = "/repos/" + api.repository
    reference_path = prefix + "/git/ref/tags/" + urllib.parse.quote(tag, safe="")
    reference = api.request("GET", reference_path)
    if reference:
        notes = verify_remote_tag(api, prefix, reference, tag, source_sha)
    elif channel == "test":
        # A test build is not tagged by hand: this job creates the annotated tag, after every check below.
        notes = annotation_message(notes, branch, source_sha, build_number, run_url)
    else:
        raise ValueError("Remote tag is missing")
    marker = "<!-- kaneopilot-ci:%s:%s -->" % (source_sha, build_number)
    release = find_release(api, prefix, tag)
    if release is not None and marker not in (release.get("body") or ""):
        raise ValueError("Release belongs to another build; refusing to modify it")
    prerelease = channel == "test"
    if release is not None and (release.get("tag_name") != tag or release.get("prerelease") is not prerelease):
        raise ValueError("Existing release tag or %s channel mismatch" % channel)
    if not prerelease and (release is None or release.get("draft")):
        # Reject an unknown latest identity before creating a draft or uploading assets.
        make_latest(api, prefix, tag)
    names = [item["name"] for item in manifest["artifacts"]] + ["manifest.json", "SHA256SUMS"]
    if not reference:
        create_test_tag(api, prefix, tag, source_sha, notes)
        verify_remote_tag(api, prefix, api.request("GET", reference_path), tag, source_sha)
    if release is None:
        install = ("测试版（预发布）：来自分支构建，仅供开发验证；不会成为 latest，App 内只有开启「接收测试版」后才会提供。\n\nAndroid 8.0+：正式密钥签名 APK（与正式版同一证书和包名）。"
                   if prerelease else "Android 8.0+：固定密钥签名 APK。")
        release = api.request("POST", prefix + "/releases", {
            "tag_name": tag, "target_commitish": source_sha, "name": "KaneoPilot " + tag,
            "draft": True, "prerelease": prerelease,
            "body": marker + "\n\n" + notes + "\n\n" + install + "\n\nmacOS 14+（仅 Apple Silicon）：DMG 内置 Python 运行时，本地签名，未公证；执行任务仍需配置 Git、Plus 等现有环境。\n\n文件完整性见 SHA256SUMS；构建与签名身份见 manifest.json。"})
    release_path = prefix + "/releases/" + str(release["id"])
    # GitHub paginates assets. Preserve unrelated assets and inspect every page.
    assets = []
    page = 1
    while True:
        items = api.request("GET", release_path + "/assets?per_page=100&page=" + str(page))
        assets.extend(items)
        if len(items) < 100:
            break
        page += 1
    by_name = {}
    for asset in assets:
        if asset["name"] in by_name:
            raise ValueError("Duplicate release asset name")
        by_name[asset["name"]] = asset
    missing = []
    for name in names:
        asset = by_name.get(name)
        if asset is None:
            missing.append(name)
            continue
        raw = api.request("GET", prefix + "/releases/assets/" + str(asset["id"]), binary=True)
        if raw is None or hashlib.sha256(raw).hexdigest() != sha256(root / name):
            raise ValueError("Existing asset content differs: " + name)
    if not release.get("draft") and missing:
        raise ValueError("Published release is incomplete; refusing to change it")
    # All existing assets were checked before uploading even one missing asset.
    for name in missing:
        asset = api.request("POST", release_path + "/assets?name=" + urllib.parse.quote(name, safe=""), upload=root / name)
        raw = api.request("GET", prefix + "/releases/assets/" + str(asset["id"]), binary=True)
        if raw is None or hashlib.sha256(raw).hexdigest() != sha256(root / name):
            raise ValueError("Uploaded asset failed hash verification: " + name)
    if release.get("draft"):
        # A prerelease never becomes latest, so a test build neither sends make_latest nor reads latest.
        api.request("PATCH", release_path, {"draft": False, "prerelease": True} if prerelease
                    else {"draft": False, "make_latest": make_latest(api, prefix, tag)})
    return release.get("html_url", "")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("identity")
    meta = subs.add_parser("metadata")
    for name in ("package", "platform", "version", "source-sha", "build-number", "fingerprint", "architectures", "output"):
        meta.add_argument("--" + name, required=True)
    for command in ("prepare", "publish"):
        sub = subs.add_parser(command)
        for name in ("directory", "tag", "source-sha", "build-number"):
            sub.add_argument("--" + name, required=True)
        sub.add_argument("--channel", choices=("stable", "test"), default="stable")
        sub.add_argument("--branch", default="")
    args = parser.parse_args()
    if args.command == "identity":
        identity()
    elif args.command == "metadata":
        metadata(args)
    elif args.command == "prepare":
        prepare(args.directory, args.tag, args.source_sha, args.build_number, args.branch)
    else:
        if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
            raise ValueError("Release publishing is only supported in GitHub-hosted Actions")
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise ValueError("GITHUB_TOKEN is required")
        server, repository, run = (os.environ.get(key, "") for key in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
        run_url = "%s/%s/actions/runs/%s" % (server, repository, run) if server.startswith("https://") and repository and run.isdigit() else ""
        print(publish(GitHub(os.environ["GITHUB_REPOSITORY"], token), args.directory, args.tag, args.source_sha,
                      args.build_number, args.channel, os.environ.get("RELEASE_NOTES", ""), args.branch, run_url))


if __name__ == "__main__":
    main()

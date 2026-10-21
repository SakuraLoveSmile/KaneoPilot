#!/usr/bin/env python3
"""Build immutable release metadata and resume only this CI run's draft release.

Only `publish` contacts GitHub. It uses GITHUB_TOKEN; local tests inject a fake API.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

STABLE_TAG = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")


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
    write_json(args.output, {
        "platform": args.platform, "name": path.name,
        "sha256": sha256(path), "size": path.stat().st_size,
        "signature_sha256": args.fingerprint, "version": args.version,
        "source_sha": args.source_sha, "build_number": int(args.build_number),
        "architectures": args.architectures.split(","),
        "signature_type": "certificate-sha256" if args.platform == "android" else "codedirectory-sha256",
    })


def prepare(directory, tag, source_sha, build_number):
    if not STABLE_TAG.fullmatch(tag):
        raise ValueError("Only stable vX.Y.Z tags can publish")
    root = Path(directory)
    artifacts = []
    for platform in ("android", "macos"):
        record = json.loads((root / (platform + "-metadata.json")).read_text())
        expected = (platform, tag[1:], source_sha, int(build_number))
        actual = tuple(record.get(key) for key in ("platform", "version", "source_sha", "build_number"))
        if actual != expected:
            raise ValueError("Artifact identity mismatch: " + platform)
        name = record["name"]
        if Path(name).name != name or not name.startswith("KaneoPilot-") or not name.endswith(".apk" if platform == "android" else ".dmg"):
            raise ValueError("Unexpected artifact name")
        if not re.fullmatch(r"[0-9a-f]{64}", record.get("signature_sha256", "")):
            raise ValueError("Missing signature fingerprint")
        package = root / name
        if record["sha256"] != sha256(package) or record["size"] != package.stat().st_size:
            raise ValueError("Artifact content mismatch: " + name)
        artifacts.append(record)
    manifest = {"version": tag[1:], "tag": tag, "source_sha": source_sha,
                "build_number": int(build_number), "artifacts": artifacts,
                "macos_requirements": "macOS 14+; compatible external Python 3.9+; Git/Plus as configured; ad-hoc signed, not notarized"}
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


def publish(api, directory, tag, source_sha, build_number):
    root = Path(directory)
    manifest = json.loads((root / "manifest.json").read_text())
    if (manifest.get("tag"), manifest.get("source_sha"), manifest.get("build_number")) != (tag, source_sha, int(build_number)):
        raise ValueError("Manifest identity mismatch")
    # Recompute from metadata, detecting changed files before any GitHub mutation.
    if prepare(root, tag, source_sha, build_number) != manifest:
        raise ValueError("Manifest does not match build metadata")
    prefix = "/repos/" + api.repository
    reference = api.request("GET", prefix + "/git/ref/tags/" + urllib.parse.quote(tag, safe=""))
    if not reference:
        raise ValueError("Remote tag is missing")
    target = reference["object"]
    for _ in range(8):
        if target["type"] != "tag":
            break
        target = api.request("GET", prefix + "/git/tags/" + target["sha"])["object"]
    if target.get("type") != "commit" or target.get("sha") != source_sha:
        raise ValueError("Remote tag no longer points to the build commit")
    marker = "<!-- kaneopilot-ci:%s:%s -->" % (source_sha, build_number)
    release = api.request("GET", prefix + "/releases/tags/" + urllib.parse.quote(tag, safe=""))
    if release is None:
        # The tag endpoint may omit drafts. Search authenticated releases before creating one.
        page = 1
        while True:
            releases = api.request("GET", prefix + "/releases?per_page=100&page=" + str(page))
            matches = [item for item in releases if item.get("tag_name") == tag]
            if len(matches) > 1:
                raise ValueError("Multiple releases use this tag")
            if matches:
                release = matches[0]
                break
            if len(releases) < 100:
                break
            page += 1
    if release is not None and marker not in (release.get("body") or ""):
        raise ValueError("Release belongs to another build; refusing to modify it")
    names = [item["name"] for item in manifest["artifacts"]] + ["manifest.json", "SHA256SUMS"]
    if release is None:
        release = api.request("POST", prefix + "/releases", {
            "tag_name": tag, "target_commitish": source_sha, "name": "KaneoPilot " + tag,
            "draft": True, "prerelease": False,
            "body": marker + "\n\nAndroid 8.0+：固定密钥签名 APK。\n\nmacOS 14+：Intel / Apple Silicon 通用 DMG，本地签名，未公证。需要外部 Python 3.9+；执行任务仍需配置 Git、Plus 等现有环境。\n\n文件完整性见 SHA256SUMS；构建与签名身份见 manifest.json。"})
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
        api.request("PATCH", release_path, {"draft": False})
    return release.get("html_url", "")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    meta = subs.add_parser("metadata")
    for name in ("package", "platform", "version", "source-sha", "build-number", "fingerprint", "architectures", "output"):
        meta.add_argument("--" + name, required=True)
    for command in ("prepare", "publish"):
        sub = subs.add_parser(command)
        for name in ("directory", "tag", "source-sha", "build-number"):
            sub.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if args.command == "metadata":
        metadata(args)
    elif args.command == "prepare":
        prepare(args.directory, args.tag, args.source_sha, args.build_number)
    else:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise ValueError("GITHUB_TOKEN is required")
        print(publish(GitHub(os.environ["GITHUB_REPOSITORY"], token), args.directory, args.tag, args.source_sha, args.build_number))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Download and unpack the pinned python-build-standalone runtime for the Mac App.

scripts/python-runtime.lock.json names one release asset by URL, size and SHA-256. Nothing else is
accepted: another host, a redirect off the allowed hosts, a size or hash that differs, or an archive
member outside the runtime folder stops the build instead of changing what ships. Standard library only.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCK = ROOT / "scripts" / "python-runtime.lock.json"
LOCK_FIELDS = {"schema", "source", "repository", "release_tag", "python_version", "asset", "url",
               "size", "sha256", "archs", "minos_max", "licenses_release"}
# The release page, then the signed CDN hosts GitHub redirects its release downloads to.
DOWNLOAD_HOSTS = ("github.com",)
REDIRECT_HOSTS = ("release-assets.githubusercontent.com", "objects.githubusercontent.com")
MAX_ARCHIVE_SIZE = 200 * 1024 * 1024
MAX_UNPACKED_SIZE = 2 * 1024 ** 3
ATTEMPTS = 3
TIMEOUT = 120
TOP_LEVEL = "python"


class FetchError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise FetchError(message)


def load_lock(path):
    """The lock file, checked so that the asset name, URL and digests cannot drift apart."""
    try:
        lock = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise FetchError("锁定文件无法读取：%s" % error) from error
    require(isinstance(lock, dict) and set(lock) == LOCK_FIELDS, "锁定文件字段缺失或含未知字段")
    require(lock["schema"] == 1, "锁定文件版本不受支持")
    require(lock["source"] == "python-build-standalone"
            and lock["repository"] == "astral-sh/python-build-standalone", "运行时来源不是 python-build-standalone")
    tag, version = lock["release_tag"], lock["python_version"]
    require(isinstance(tag, str) and re.fullmatch(r"[0-9]{8}", tag), "发布标签格式无效")
    require(isinstance(version, str) and re.fullmatch(r"3\.[0-9]{1,2}\.[0-9]{1,3}", version), "Python 版本格式无效")
    asset = "cpython-%s+%s-aarch64-apple-darwin-install_only_stripped.tar.gz" % (version, tag)
    require(lock["asset"] == asset, "归档名必须是 install_only_stripped 的 aarch64 macOS 归档")
    url = "https://github.com/%s/releases/download/%s/%s" % (lock["repository"], tag, urllib.parse.quote(asset, safe=""))
    require(lock["url"] == url, "下载地址与发布标签、归档名不一致")
    require(type(lock["size"]) is int and 0 < lock["size"] <= MAX_ARCHIVE_SIZE, "归档大小无效")
    require(isinstance(lock["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", lock["sha256"]), "SHA-256 必须是 64 位小写十六进制")
    require(lock["archs"] == ["arm64"], "运行时只允许 arm64")
    require(isinstance(lock["minos_max"], str) and re.fullmatch(r"[0-9]+\.[0-9]+", lock["minos_max"]), "minos_max 格式无效")
    return lock


def allowed_hop(url, hosts):
    parts = urllib.parse.urlsplit(url)
    return (parts.scheme == "https" and parts.username is None and parts.password is None
            and parts.port in (None, 443) and (parts.hostname or "").lower() in hosts)


class StrictRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to https on one of the CDN hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_hop(newurl, REDIRECT_HOSTS):
            raise FetchError("下载被重定向到不允许的地址：%s" % newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_opener():
    return urllib.request.build_opener(StrictRedirects)


def download(lock, destination, opener=None, sleep=time.sleep):
    """Save the locked archive. Size and SHA-256 must both match; a bad file is deleted, never kept."""
    require(allowed_hop(lock["url"], DOWNLOAD_HOSTS), "下载地址不在允许的主机上")
    destination = Path(destination)
    opener = opener or build_opener()
    failure = None
    for attempt in range(ATTEMPTS):
        if attempt:
            sleep(5 * attempt)
        try:
            request = urllib.request.Request(lock["url"], headers={
                "User-Agent": "KaneoPilot-build", "Accept": "application/octet-stream"})
            received, digest = 0, hashlib.sha256()
            with opener.open(request, timeout=TIMEOUT) as response, destination.open("wb") as output:
                require(getattr(response, "status", 200) == 200, "下载响应不是 200")
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    received += len(block)
                    require(received <= lock["size"], "归档超过锁定的大小")
                    digest.update(block)
                    output.write(block)
            break
        except FetchError:
            destination.unlink(missing_ok=True)
            raise
        except (OSError, urllib.error.URLError) as error:
            failure = error
            destination.unlink(missing_ok=True)
    else:
        raise FetchError("下载失败（已重试 %d 次）：%s" % (ATTEMPTS, failure))
    if received != lock["size"] or digest.hexdigest() != lock["sha256"]:
        destination.unlink(missing_ok=True)
        raise FetchError("归档的大小或 SHA-256 与锁定值不符（%d 字节，%s）" % (received, digest.hexdigest()))
    return destination


def inside(path, root):
    path = os.path.normpath(path)
    return path == root or path.startswith(root + os.sep)


def extract(archive, destination):
    """Unpack into `destination`, which must not exist. Returns the runtime folder `destination/python`."""
    destination = Path(destination).resolve()
    destination.mkdir(parents=True)
    runtime = str(destination / TOP_LEVEL)
    unpacked = 0
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            name = PurePosixPath(member.name)
            if member.isdir() and not name.parts:
                continue  # a bare "./" entry
            require(not name.is_absolute() and ".." not in name.parts and name.parts, "归档含不安全的路径：%s" % member.name)
            require(name.parts[0] == TOP_LEVEL, "归档的顶层目录必须是 %s/：%s" % (TOP_LEVEL, member.name))
            target = os.path.normpath(str(destination / name))
            require(inside(target, runtime), "归档成员越界：%s" % member.name)
            parent = os.path.dirname(target)
            real_parent = os.path.realpath(parent)
            require(real_parent == str(destination) or inside(real_parent, os.path.realpath(runtime)),
                    "归档成员经符号链接越界：%s" % member.name)
            os.makedirs(parent, exist_ok=True)
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                os.chmod(target, (member.mode & 0o777) | 0o700)
            elif member.isreg():
                unpacked += member.size
                require(unpacked <= MAX_UNPACKED_SIZE, "归档解包后超过大小上限")
                require(not os.path.lexists(target), "归档含重复成员：%s" % member.name)
                with tar.extractfile(member) as source, open(target, "wb") as output:
                    shutil.copyfileobj(source, output)
                os.chmod(target, (member.mode & 0o777) | 0o600)
                os.utime(target, (member.mtime, member.mtime))
            elif member.issym():
                link = member.linkname
                require(link and not link.startswith("/"), "符号链接必须是相对路径：%s" % member.name)
                require(inside(os.path.normpath(os.path.join(parent, link)), runtime), "符号链接指向运行时之外：%s" % member.name)
                require(not os.path.lexists(target), "归档含重复成员：%s" % member.name)
                os.symlink(link, target)
            elif member.islnk():
                source = os.path.normpath(str(destination / member.linkname))
                require(inside(source, runtime) and os.path.isfile(source) and not os.path.islink(source),
                        "硬链接必须指向已解包的普通文件：%s" % member.name)
                require(not os.path.lexists(target), "归档含重复成员：%s" % member.name)
                os.link(source, target)
            else:
                raise FetchError("归档含不支持的成员类型：%s" % member.name)
    require(os.path.exists(os.path.join(runtime, "bin", "python3")), "解包结果里没有 bin/python3")
    return Path(runtime)


def fetch(lock, output, opener=None, sleep=time.sleep):
    """Download, verify and unpack into `output`. A failed run leaves nothing behind."""
    output = Path(output)
    require(not output.exists(), "输出目录已存在：%s" % output)
    output.mkdir(parents=True)
    try:
        archive = download(lock, output / lock["asset"], opener=opener, sleep=sleep)
        runtime = extract(archive, output / "unpacked")
        (output / "runtime.json").write_text(json.dumps(lock, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return runtime
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise


def main(argv=None):
    # Check the build context before parsing arguments or creating files.
    if subprocess.call(["sh", str(ROOT / "scripts/require_actions.sh"), "--allow-local-macos"]):
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", default=str(DEFAULT_LOCK))
    parser.add_argument("--output", required=True, help="a folder that does not exist yet")
    args = parser.parse_args(argv)
    try:
        lock = load_lock(args.lock)
        runtime = fetch(lock, args.output)
    except FetchError as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 1
    print("Python runtime %s (python-build-standalone %s): %s" % (lock["python_version"], lock["release_tag"], runtime))
    # Later workflow steps (scripts/build_app.sh) read the folder from the job environment.
    if os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as environment:
            environment.write("PYTHON_RUNTIME_DIR=%s\n" % runtime)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Seed task-private offline tool caches; never run verification or a shell."""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from . import util

_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _require(condition, message):
    util.require(condition, message, "codex_environment_unavailable")


def _open_directory(parent, name, *, create=True):
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
    return os.open(name, _DIRECTORY, dir_fd=parent)


@contextlib.contextmanager
def _private_root(spec):
    cwd = Path(spec["cwd"])
    run_id = spec["run_id"]
    _require(cwd.is_absolute() and str(cwd.resolve(strict=True)) == str(cwd)
             and type(run_id) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", run_id),
             "Codex 离线缓存工作区或轮次路径无效")
    fd = os.open(cwd, _DIRECTORY)
    try:
        for name in (".pi", "kanpilot", "codex", run_id):
            child = _open_directory(fd, name)
            os.close(fd)
            fd = child
        root = cwd / ".pi" / "kanpilot" / "codex" / run_id
        environment = spec["tool_environment"]["environment"]
        for key, name in (("PUB_CACHE", "pub-cache"), ("GRADLE_USER_HOME", "gradle")):
            if key in environment:
                _require(environment[key] == str(root / name), "Codex 私有工具缓存路径与轮次不符")
        yield fd
    finally:
        os.close(fd)


def _source(path):
    path = Path(path)
    _require(path.is_absolute() and str(path.resolve()) == str(path), "Codex 宿主缓存路径不是规范路径")
    return path


def _regular(parent, name):
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    _require(stat.S_ISREG(info.st_mode), "Codex 私有缓存文件被符号链接或特殊文件替换")


def _copy_file(source, destination, name):
    """Copy only absent regular files, with descriptor-relative destination IO."""
    try:
        target = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=destination)
    except FileExistsError:
        _regular(destination, name)
        return
    try:
        original = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source)
        try:
            info = os.fstat(original)
            _require(stat.S_ISREG(info.st_mode), "Codex 宿主缓存含不支持的特殊文件")
            with os.fdopen(original, "rb", closefd=False) as reader, os.fdopen(target, "wb", closefd=False) as writer:
                while True:
                    chunk = reader.read(1 << 20)
                    if not chunk:
                        break
                    writer.write(chunk)
            os.fchmod(target, info.st_mode & 0o777)
        finally:
            os.close(original)
    except BaseException:
        os.unlink(name, dir_fd=destination)
        raise
    finally:
        os.close(target)


def _copy_directory(source, destination):
    with os.scandir(source) as entries:
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                original = os.open(entry.name, _DIRECTORY, dir_fd=source)
                try:
                    target = _open_directory(destination, entry.name)
                    try:
                        _copy_directory(original, target)
                    finally:
                        os.close(target)
                finally:
                    os.close(original)
            else:
                _require(stat.S_ISREG(info.st_mode), "Codex 宿主缓存含符号链接或特殊文件")
                _copy_file(source, destination, entry.name)


def _copy_tree(path, destination, name):
    _require(path.resolve(strict=True) == path, "Codex 宿主缓存目录含符号链接")
    original = os.open(path, _DIRECTORY)
    try:
        target = _open_directory(destination, name)
        try:
            _copy_directory(original, target)
        finally:
            os.close(target)
    finally:
        os.close(original)


def _link(destination, name, source):
    _require(source.is_dir() and not source.is_symlink()
             and source.resolve(strict=True) == source, "Codex 依赖缓存目录含符号链接")
    try:
        os.symlink(str(source), name, dir_fd=destination)
    except FileExistsError:
        _require(stat.S_ISLNK(os.stat(name, dir_fd=destination, follow_symlinks=False).st_mode)
                 and os.readlink(name, dir_fd=destination) == str(source),
                 "Codex 私有依赖缓存链接被替换")


def _replace_text(destination, name, text):
    try:
        _regular(destination, name)
    except FileNotFoundError:
        pass
    temporary = ".kanpilot-" + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=destination)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as stream:
            stream.write(text)
        os.replace(temporary, name, src_dir_fd=destination, dst_dir_fd=destination)
    finally:
        os.close(fd)
        try:
            os.unlink(temporary, dir_fd=destination)
        except FileNotFoundError:
            pass


def _lock_packages(cwd):
    lock = Path(cwd) / "pubspec.lock"
    if not lock.is_file():
        return {}
    data = util.yaml.safe_load(lock.read_text(encoding="utf-8"))
    _require(type(data) is dict and type(data.get("packages")) is dict, "Flutter 锁文件格式无效，无法准备离线依赖")
    return data["packages"]


def _hosted_directory(url):
    # Pub's compatibility encoding uses decimal ASCII values, not hexadecimal.
    # See dart-lang/pub lib/src/source/hosted.dart, _urlToDirectory.
    def normalize(match):
        localhost = "localhost" if match[2] else ""
        scheme = "" if match[1] == "https://" or localhost else match[1]
        return scheme + localhost
    value = re.sub(r"^(https?://)(127\.0\.0\.1|\[::1\]|localhost)?", normalize, url)
    return re.sub(r'[<>:"\\/|?*%]', lambda match: "%" + str(ord(match[0])), value)


def _hosted_dependencies(cwd):
    dependencies = set()
    for name, package in _lock_packages(cwd).items():
        if not isinstance(package, dict) or package.get("source") != "hosted":
            continue
        description = package.get("description")
        version = package.get("version")
        _require(type(name) is str and re.fullmatch(r"[A-Za-z0-9_]+", name)
                 and type(version) is str and re.fullmatch(r"[A-Za-z0-9.+-]+", version),
                 "Flutter hosted 锁定依赖名称或版本无效")
        url = description.get("url", "https://pub.dev") if type(description) is dict else "https://pub.dev"
        _require(type(url) is str and not re.search(r"[\x00-\x20\x7f]", url), "Flutter hosted 锁定依赖地址无效")
        parsed = urlsplit(url)
        _require(parsed.scheme in ("https", "http") and parsed.hostname
                 and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
                 "Flutter hosted 依赖地址含凭据或不支持的协议")
        dependencies.add((_hosted_directory(url), name, version))
    return sorted(dependencies)


def _git_dependencies(cwd):
    dependencies = {}
    for package in _lock_packages(cwd).values():
        if not isinstance(package, dict) or package.get("source") != "git":
            continue
        description = package.get("description")
        _require(type(description) is dict, "Flutter Git 锁定依赖缺少来源")
        url, revision = description.get("url"), description.get("resolved-ref")
        _require(type(url) is str and not re.search(r"[\x00-\x20\x7f]", url)
                 and type(revision) is str and re.fullmatch(r"[0-9a-fA-F]{40,64}", revision),
                 "Flutter Git 锁定依赖地址或提交无效")
        parsed = urlsplit(url)
        _require((parsed.scheme in ("https", "http", "ssh", "git")
                  or (parsed.scheme == "file" and parsed.path.startswith("/") and not parsed.netloc)
                  or (not parsed.scheme and re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:.+", url)))
                 and not (parsed.scheme in ("https", "http") and (parsed.username or parsed.password))
                 and not parsed.query and not parsed.fragment,
                 "Flutter Git 依赖地址含凭据或不支持的协议")
        name = re.sub(r"[^A-Za-z0-9._-]", "_", url.rsplit("/", 1)[-1].removesuffix(".git"))[:50]
        path = description.get("path", ".")
        _require(bool(name) and type(path) is str and not Path(path).is_absolute()
                 and ".." not in Path(path).parts and not re.search(r"[\x00-\x1f\x7f\\]", path),
                 "Flutter Git 依赖缓存名称或子目录无效")
        dependencies.setdefault((name, url, revision), set()).add(path)
    return [(name, url, revision, sorted(paths)) for (name, url, revision), paths in sorted(dependencies.items())]


def _drop_expected_link(destination, name, source):
    try:
        info = os.stat(name, dir_fd=destination, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        # A previous offline pub invocation may already have made a private clone.
        return
    _require(stat.S_ISLNK(info.st_mode) and os.readlink(name, dir_fd=destination) == str(source),
             "Codex 私有依赖缓存链接被替换")
    os.unlink(name, dir_fd=destination)


def _seed_pub(spec, root, source):
    count, git_count = 0, 0
    target = _open_directory(root, "pub-cache")
    try:
        for host, name, version in _hosted_dependencies(spec["cwd"]):
            package = source / "hosted" / host / (name + "-" + version)
            if not package.exists():
                continue
            destination = _open_directory(target, "hosted")
            try:
                packages = _open_directory(destination, host)
                try:
                    _link(packages, package.name, package)
                    count += 1
                    metadata = package.parent / ".cache"
                    if metadata.exists():
                        _require(metadata.resolve(strict=True) == metadata, "Pub hosted 元数据含符号链接")
                        original = os.open(metadata, _DIRECTORY)
                        try:
                            copied = _open_directory(packages, ".cache")
                            try:
                                for suffix in ("-versions.json", "-advisories.json"):
                                    if (metadata / (name + suffix)).exists():
                                        _copy_file(original, copied, name + suffix)
                            finally:
                                os.close(copied)
                        finally:
                            os.close(original)
                finally:
                    os.close(packages)
            finally:
                os.close(destination)
            hashes = source / "hosted-hashes" / host
            filename = name + "-" + version + ".sha256"
            if (hashes / filename).exists():
                _require(hashes.resolve(strict=True) == hashes, "Pub hosted hash 目录含符号链接")
                original = os.open(hashes, _DIRECTORY)
                try:
                    destination = _open_directory(target, "hosted-hashes")
                    try:
                        copied = _open_directory(destination, host)
                        try:
                            _copy_file(original, copied, filename)
                        finally:
                            os.close(copied)
                    finally:
                        os.close(destination)
                finally:
                    os.close(original)
        git = _open_directory(target, "git")
        try:
            caches = _open_directory(git, "cache")
            try:
                for name, url, revision, required_paths in _git_dependencies(spec["cwd"]):
                    checkout = source / "git" / (name + "-" + revision)
                    if checkout.exists():
                        _require(checkout.resolve(strict=True) == checkout, "Pub Git checkout 缓存目录含符号链接")
                        package_list = checkout / ".git/pub-packages"
                        if package_list.exists():
                            _require(package_list.resolve(strict=True) == package_list and package_list.is_file(),
                                     "Pub Git checkout 元数据含符号链接或特殊文件")
                            paths = package_list.read_text(encoding="utf-8").splitlines()
                        else:
                            paths = ["."]
                        # Pub updates .git/pub-packages for a newly used subpath.
                        # Leave that checkout absent so it clones from the private bare seed.
                        if all(path in paths for path in required_paths):
                            _link(git, checkout.name, checkout)
                            git_count += 1
                        else:
                            _drop_expected_link(git, checkout.name, checkout)
                    bare = source / "git/cache" / (name + "-" + hashlib.sha1(url.encode("utf-8")).hexdigest())
                    if not bare.exists():
                        continue
                    _require(bare.resolve(strict=True) == bare, "Pub Git 仓库缓存目录含符号链接")
                    _require(not (bare / "objects/info/alternates").exists(),
                             "Pub Git 仓库缓存引用外部对象目录，不支持安全离线准备")
                    original = os.open(bare, _DIRECTORY)
                    try:
                        copied = _open_directory(caches, bare.name)
                        try:
                            for part in ("objects", "refs"):
                                if (bare / part).exists():
                                    _copy_tree(bare / part, copied, part)
                            for part in ("HEAD", "packed-refs", "shallow"):
                                if (bare / part).exists():
                                    _copy_file(original, copied, part)
                            escaped = url.replace("\\", "\\\\").replace('"', '\\"')
                            _replace_text(copied, "config", '[core]\n\tbare = true\n[remote "origin"]\n'
                                          '\turl = "' + escaped + '"\n\tfetch = +refs/*:refs/*\n\tmirror = true\n')
                        finally:
                            os.close(copied)
                    finally:
                        os.close(original)
            finally:
                os.close(caches)
        finally:
            os.close(git)
    finally:
        os.close(target)
    return count, git_count


def _properties(path):
    _require(path.is_file() and not path.is_symlink(), "Android Gradle wrapper 配置缺失或含符号链接")
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        key, separator, value = line.partition("=")
        _require(bool(separator), "Android Gradle wrapper 配置格式不支持离线准备")
        result[key.strip()] = value.strip().replace("\\:", ":").replace("\\=", "=")
    return result


def _base36(number):
    result = ""
    while number:
        number, remainder = divmod(number, 36)
        result = "0123456789abcdefghijklmnopqrstuvwxyz"[remainder] + result
    return result or "0"


def _seed_gradle(spec, root, source):
    properties = _properties(Path(spec["cwd"]) / "android/gradle/wrapper/gradle-wrapper.properties")
    url = properties.get("distributionUrl", "")
    parsed = urlsplit(url)
    filename = parsed.path.rsplit("/", 1)[-1]
    _require(parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
             and not parsed.query and not parsed.fragment
             and re.fullmatch(r"gradle-[A-Za-z0-9.-]+-(?:all|bin)\.zip", filename),
             "Android Gradle 分发地址不支持离线准备")
    for key in ("distributionBase", "zipStoreBase"):
        _require(properties.get(key, "GRADLE_USER_HOME") == "GRADLE_USER_HOME", "Android Gradle 分发必须位于私有 GRADLE_USER_HOME")
    for key in ("distributionPath", "zipStorePath"):
        _require(properties.get(key, "wrapper/dists") == "wrapper/dists", "Android Gradle 自定义分发目录不支持离线准备")
    distribution = filename[:-4]
    url_hash = _base36(int.from_bytes(hashlib.md5(url.encode("utf-8")).digest(), "big"))
    cached = source / "wrapper/dists" / distribution / url_hash
    version = re.sub(r"-(?:all|bin)$", "", distribution)
    expanded = cached / version
    marker = filename + ".ok"
    _require(expanded.is_dir() and (expanded / "bin/gradle").is_file()
             and (expanded / "lib").is_dir()
             and len(list((expanded / "lib").glob("gradle-launcher-*.jar"))) == 1
             and (cached / marker).is_file(),
             "Android 离线 Gradle 分发缓存缺失：" + distribution + "；请先在受信任环境准备该版本")
    _require(cached.resolve(strict=True) == cached, "Android Gradle 分发缓存目录含符号链接")
    target = _open_directory(root, "gradle")
    try:
        for part in ("wrapper", "dists", distribution, url_hash):
            child = _open_directory(target, part)
            os.close(target)
            target = child
        runtime = _open_directory(target, version)
        try:
            # Documentation, sources, host init scripts and the large archive are not required at runtime.
            for part in ("bin", "lib"):
                _copy_tree(expanded / part, runtime, part)
        finally:
            os.close(runtime)
        original = os.open(cached, _DIRECTORY)
        try:
            _copy_file(original, target, marker)
        finally:
            os.close(original)
        # Wrapper needs its own writable lock, never the host's existing .lck.
        _replace_text(target, filename + ".lck", "")
    finally:
        os.close(target)
    return 1


def seed_caches(spec):
    """Populate only frozen private cache targets from read-only host seeds."""
    receipt = {"pub_packages": 0, "git_checkouts": 0, "gradle_distributions": 0}
    environment = spec.get("tool_environment")
    if not isinstance(environment, dict) or environment.get("v") not in (2, 3):
        return receipt
    setup = environment["setup"]
    if not setup["pub_offline"] and not setup["android"]:
        return receipt
    try:
        with _private_root(spec) as root:
            if setup["pub_offline"]:
                receipt["pub_packages"], receipt["git_checkouts"] = _seed_pub(spec, root, _source(setup["pub_cache_source"]))
            if setup["android"]:
                _require(setup.get("gradle_cache_source"), "Android 离线 Gradle 缓存来源缺失")
                receipt["gradle_distributions"] = _seed_gradle(spec, root, _source(setup["gradle_cache_source"]))
        return receipt
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, util.yaml.YAMLError) as exc:
        raise util.KanpilotError("codex_environment_unavailable", "Codex 离线依赖缓存无法准备，路径无效或含符号链接") from exc


def setup_argv(spec):
    """Return setup only; caller supplies the frozen launcher and captures output."""
    environment = spec.get("tool_environment")
    if not isinstance(environment, dict) or environment.get("v") not in (2, 3):
        return None
    setup = environment["setup"]
    return [setup["flutter"], "pub", "get", "--offline", "--enforce-lockfile"] if setup["pub_offline"] else None

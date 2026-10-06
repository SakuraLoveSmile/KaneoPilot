"""最小 pytest API 桩——仅供 `python3 -m unittest discover` 使用。

本机无 pytest 安装：unittest discover 把 ``tests/`` 目录推入 sys.path[0]，
模块内 ``import pytest`` 解析到本文件。真实 pytest 运行时 ``import pytest``
始终先命中 ``sys.modules`` 中的真模块，本文件不生效（函数级 fixture/parametrize
语义由 tests/unittest_compat.py 的消费方兑现）。

提供的 API 子集（与本套件实际用量一致）：
- ``@pytest.fixture`` / ``@pytest.fixture(autouse=True)``（函数级、yield 收口）；
- ``@pytest.mark.parametrize(names, values)``；
- ``pytest.raises(...)`` / ``pytest.fail(...)`` / ``pytest.skip(...)``；
- ``pytest.mark.skipif``/``pytest.mark.skip`` 宽松透传。

fixture 注册表按「定义所在模块」分桶（``_module_fixtures``），类内方法
fixture 只打标不入桶（由兼容层经类 MRO 查找）。
"""

from __future__ import annotations

import unittest


class FixtureDef:
    """``@pytest.fixture`` 元数据（函数级 scope 即「每用例一次」）。"""

    __slots__ = ("func", "name", "autouse", "params")

    def __init__(self, func, name=None, autouse=False, params=None):
        self.func = func
        self.name = name or func.__name__
        self.autouse = autouse
        self.params = params


class _MarkApplier:
    def __init__(self, name):
        self._name = name

    def __call__(self, *args, **kwargs):
        if self._name == "parametrize":
            argnames, argvalues = args[0], args[1]
            ids = kwargs.get("ids")

            def deco(func):
                marks = getattr(func, "__parametrize__", None) or []
                marks.append((argnames, list(argvalues), ids))
                func.__parametrize__ = marks
                return func

            return deco
        if self._name in ("skip", "skipif"):
            # 宽松透传：本套件未使用；标注即可，发现层不解释。
            def deco(obj):
                return obj

            return deco
        raise AttributeError(f"pytest.mark.{self._name} 未在本桩中实现")


class _MarkNamespace:
    """``pytest.mark.<name>(...)`` 命名空间。"""

    def __getattr__(self, name):
        return _MarkApplier(name)


mark = _MarkNamespace()


_module_fixtures: dict = {}


def _module_key(func) -> str:
    module = getattr(func, "__module__", "") or ""
    return module.rsplit(".", 1)[-1]


def _register(func, name=None, autouse=False, params=None):
    fdef = FixtureDef(func, name=name, autouse=autouse, params=params)
    func.__pytest_fixture__ = fdef
    # 类内 fixture（qualname 含 '.'）只在类作用域可见，不进模块注册表。
    if "." not in getattr(func, "__qualname__", ""):
        _module_fixtures.setdefault(_module_key(func), {})[fdef.name] = fdef
    return func


def fixture(func=None, *, name=None, autouse=False, params=None, **_ignored):
    if func is not None:
        return _register(func, name=name, autouse=autouse, params=params)

    def deco(f):
        return _register(f, name=name, autouse=autouse, params=params)

    return deco


class _RaisesContext:
    def __init__(self, expected, match=None):
        self.expected = expected
        self.match = match
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            fail(f"预期抛出 {self.expected}，但未抛出异常")
        if issubclass(exc_type, self.expected):
            self.value = exc
            return True
        return None


def raises(expected_exception, match=None):
    if isinstance(expected_exception, type) or (
            isinstance(expected_exception, tuple)
            and all(isinstance(e, type) for e in expected_exception)):
        return _RaisesContext(expected_exception, match=match)
    raise TypeError("pytest.raises 桩仅接受异常类型")


def fail(reason=""):
    raise AssertionError(f"pytest.fail：{reason}")


def skip(reason=""):
    raise unittest.SkipTest(reason or "pytest.skip")


# —— 兼容层内部查询接口（tests/unittest_compat.py 使用）——

def module_fixtures(module_name: str) -> dict:
    return _module_fixtures.get(module_name, {})

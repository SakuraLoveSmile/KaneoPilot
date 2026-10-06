"""`python3 -m unittest discover` 的 pytest 式用例收集/执行引擎。

本套件为 pytest 风格（fixture 注入 + 无 TestCase 基类 + parametrize），而
本机无 pytest。README 既定方案（「无 pytest 时需补 unittest 兼容层」）由本
文件兑现：每个 ``test_*.py`` 末尾追加

    def load_tests(loader, standard_tests, pattern):
        import sys
        try:
            import unittest_compat
        except ImportError:  # 包上下文（tests.test_x 导入形态）
            from tests import unittest_compat
        return unittest_compat.collect_module(sys.modules[__name__])

即可让 unittest discover 运行模块内全部 pytest 式用例。语义子集（与本套件
实际用量一致；见 tests/README.md）：

- 收集：模块级 ``test_*`` 函数 + ``Test*`` 类的 ``test_*`` 方法（按定义行
  号排序，参数化按 ``@pytest.mark.parametrize`` 展开为多实例）；
- fixture：函数级，按参数名解析。解析序 = parametrize 参数 > 类 fixture
  （MRO，绑定 self）> 本模块 fixture > conftest fixture > 内建 ``tmp_path``；
- ``yield`` fixture 经 ExitStack 收口（每用例一套临时目录/DB/假 Kaneo 服务）；
- ``autouse=True`` fixture（模块/conftest/类三级）在每用例解析参数前先执行；
- 用例间互不共享 fixture 实例（函数级 scope 语义）。

fixture 注册表由 tests/pytest.py 桩持有（``import pytest`` 在 discover
下解析到该桩；真实 pytest 运行时不经本文件）。
"""

from __future__ import annotations

import inspect
import shutil
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path

import pytest  # unittest discover 下 = tests/pytest.py 桩（sys.path[0]=tests/）


def _module_key(module) -> str:
    name = getattr(module, "__name__", "") or ""
    return name.rsplit(".", 1)[-1]


def _conftest_fixtures() -> dict:
    """conftest fixture 桶（discover 下为顶层 ``conftest``，包导入时兜底）。"""
    found = pytest.module_fixtures("conftest")
    if found:
        return found
    for mod in (sys.modules.get("conftest"), sys.modules.get("tests.conftest")):
        if mod is not None:
            key = _module_key(mod)
            found = pytest.module_fixtures(key)
            if found:
                return found
    return {}


class _FixtureContext:
    """单用例的 fixture 解析缓存 + 收口栈。"""

    def __init__(self, module, cls=None, instance=None):
        self.module = module
        self.cls = cls
        self.instance = instance
        self.stack = ExitStack()
        self.cache = {}

    def close(self):
        self.stack.close()

    def resolve(self, name, params):
        if name in params:
            return params[name]
        if name in self.cache:
            return self.cache[name]
        value = self._produce(name, params)
        self.cache[name] = value
        return value

    def _produce(self, name, params):
        # 1) 类级 fixture（含继承；同名就近取子类定义）。
        if self.cls is not None:
            for klass in self.cls.__mro__:
                fdef = vars(klass).get(name)
                if fdef is not None and hasattr(fdef, "__pytest_fixture__"):
                    return self._invoke(fdef.__pytest_fixture__, params,
                                        bind=True)
        # 2) 本模块 fixture。
        fdef = pytest.module_fixtures(_module_key(self.module)).get(name)
        if fdef is not None:
            return self._invoke(fdef, params)
        # 3) conftest fixture。
        fdef = _conftest_fixtures().get(name)
        if fdef is not None:
            return self._invoke(fdef, params)
        # 4) 内建：tmp_path（pytest 等价物）。
        if name == "tmp_path":
            path = Path(tempfile.mkdtemp(prefix="kp-unittest-"))
            self.stack.callback(
                lambda p=path: shutil.rmtree(p, ignore_errors=True))
            return path
        raise AssertionError(
            f"未知 fixture：{name!r}（{_module_key(self.module)} 与 conftest "
            f"均未定义）")

    def _invoke(self, fdef, params, bind=False):
        func = fdef.func
        if bind and self.instance is not None:
            func = func.__get__(self.instance)
        deps = {}
        for dep in inspect.signature(fdef.func).parameters:
            if bind and dep == "self":
                continue
            deps[dep] = self.resolve(dep, params)
        if inspect.isgeneratorfunction(fdef.func):
            gen = func(**deps)
            value = next(gen)
            self.stack.callback(self._finish_gen, gen, fdef.name)
            return value
        return func(**deps)

    @staticmethod
    def _finish_gen(gen, name):
        try:
            next(gen)
        except StopIteration:
            return
        raise AssertionError(f"yield fixture {name} 产生了多个值")

    def autouse_names(self):
        names = []
        if self.cls is not None:
            for klass in self.cls.__mro__:
                for obj in vars(klass).values():
                    fdef = getattr(obj, "__pytest_fixture__", None)
                    if fdef is not None and fdef.autouse:
                        names.append(fdef.name)
        for bucket in (pytest.module_fixtures(_module_key(self.module)),
                       _conftest_fixtures()):
            for name, fdef in bucket.items():
                if fdef.autouse:
                    names.append(name)
        return names


class _NamedFunctionTestCase(unittest.FunctionTestCase):
    def __init__(self, func, label, description=None):
        super().__init__(func, description=description)
        self._label = label

    def id(self):
        return self._label

    def __str__(self):
        return self._label


def _expand_params(func):
    """@pytest.mark.parametrize 叠加展开 → [{name: value}, ...]（可为 [{}]）。"""
    marks = getattr(func, "__parametrize__", None)
    if not marks:
        return [({}, "")]
    cases = [({}, "")]
    for argnames, argvalues, ids in marks:
        names = [n.strip() for n in argnames.split(",")]
        expanded = []
        for index, value in enumerate(argvalues):
            if len(names) == 1:
                pair = {names[0]: value}
                tag = str(value)
            else:
                pair = dict(zip(names, value))
                tag = "-".join(str(v) for v in value)
            if ids and index < len(ids) and ids[index] is not None:
                tag = str(ids[index])
            for base, base_tag in cases:
                merged = dict(base)
                merged.update(pair)
                expanded.append((merged, f"{base_tag}[{tag}]"))
        cases = expanded
    return cases


def _make_case(module, cls, func, params, tag):
    label = f"{_module_key(module)}.{cls.__name__}.{func.__name__}{tag}" \
        if cls is not None else f"{_module_key(module)}.{func.__name__}{tag}"

    def run():
        instance = cls() if cls is not None else None
        ctx = _FixtureContext(module, cls=cls, instance=instance)
        try:
            for name in ctx.autouse_names():
                ctx.resolve(name, params)
            target = func if instance is None else func.__get__(instance)
            kwargs = {}
            for pname in inspect.signature(func).parameters:
                if instance is not None and pname == "self":
                    continue
                kwargs[pname] = ctx.resolve(pname, params)
            target(**kwargs)
        finally:
            ctx.close()

    doc = inspect.getdoc(func)
    return _NamedFunctionTestCase(
        run, label, description=doc.splitlines()[0] if doc else None)


def _line_of(obj):
    try:
        return inspect.getsourcelines(obj)[1]
    except (OSError, TypeError):
        return 0


def collect_module(module):
    """模块 → unittest.TestSuite（pytest 语义子集；load_tests 契约）。"""
    items = []
    for name, obj in vars(module).items():
        if name.startswith("test_") and inspect.isfunction(obj):
            items.append((None, obj))
        elif inspect.isclass(obj) and name.startswith("Test"):
            items.append((obj, None))
    items.sort(key=lambda item: _line_of(item[1] if item[1] is not None
                                         else item[0]))
    cases = []
    for cls, func in items:
        if func is None:
            # 类：收集 test_* 方法（含继承），按行号排序。
            methods = [
                member
                for mname, member in inspect.getmembers(
                    cls, predicate=inspect.isfunction)
                if mname.startswith("test_")
            ]
            methods.sort(key=_line_of)
            for method in methods:
                for params, tag in _expand_params(method):
                    cases.append(_make_case(module, cls, method, params, tag))
        else:
            for params, tag in _expand_params(func):
                cases.append(_make_case(module, None, func, params, tag))
    suite = unittest.TestSuite()
    suite.addTests(cases)
    return suite

"""Reload safety for a module that defines an exception.

``importlib.reload`` re-executes a module in place, and every class it defines
comes back as a new object. A module that imported such a class keeps the old
object, so an exception the reloaded module raises stops matching the class that
module compares against it: ``pytest.raises`` in an untouched file fails, and the
failure points at a file the change never touched.

The hazard is made detectable rather than discoverable: the package's own modules
that define an exception are read from their source, and a reload of one is
refused where it is written. A module that defines no exception is left alone.

One boundary is reported rather than refused: ``exec(source, module.__dict__)``
runs a module's source again in its own namespace, which re-creates the classes it
defines exactly as a reload does while no reload happens, so there is nothing here
to refuse. The suite's sources are read for that shape instead.
``runpy.run_module``, ``runpy.run_path``, an ``exec`` into a namespace of its own,
and a child interpreter all leave the loaded classes in place, and are left alone.
"""

from __future__ import annotations

import ast
import importlib
from collections.abc import Collection, Mapping
from pathlib import Path
from types import ModuleType

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "mnemoseed_local"

#: The classes the standard library raises, which the package's exceptions derive
#: from, plus the ones a raised class is spelled with.
STDLIB_EXCEPTIONS = frozenset(
    {
        "ArithmeticError",
        "AssertionError",
        "AttributeError",
        "BaseException",
        "BufferError",
        "ConnectionError",
        "EOFError",
        "Exception",
        "FileNotFoundError",
        "IOError",
        "ImportError",
        "IndexError",
        "KeyError",
        "KeyboardInterrupt",
        "LookupError",
        "MemoryError",
        "NotImplementedError",
        "OSError",
        "OverflowError",
        "PermissionError",
        "ReferenceError",
        "RuntimeError",
        "StopIteration",
        "SyntaxError",
        "SystemExit",
        "TimeoutError",
        "TypeError",
        "UnicodeError",
        "ValueError",
        "ZeroDivisionError",
    }
)
EXCEPTION_SUFFIXES = ("Error", "Exception")

_ORIGINAL_RELOAD = importlib.reload
_ACTIVE = False


class ReloadUnsafeError(RuntimeError):
    """A reload that would re-create a module's exception classes."""


def exception_classes(tree: ast.AST) -> tuple[str, ...]:
    """The classes one module defines that an exception's identity rides on."""
    parents = {
        node.name: _base_identifiers(node) for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    }
    found: set[str] = set()
    growing = True
    while growing:
        growing = False
        for class_name, bases in parents.items():
            if class_name in found:
                continue
            if class_name.endswith(EXCEPTION_SUFFIXES) or bases & (STDLIB_EXCEPTIONS | found):
                found.add(class_name)
                growing = True
    return tuple(sorted(found))


def _base_identifiers(node: ast.ClassDef) -> set[str]:
    """The classes one declaration derives from, as its own source spells them."""
    bases: set[str] = set()
    for base in node.bases:
        if isinstance(base, ast.Name):
            bases.add(base.id)
        elif isinstance(base, ast.Attribute):
            bases.add(base.attr)
    return bases


def unsafe_modules(root: Path = PACKAGE_ROOT) -> dict[str, tuple[str, ...]]:
    """The package's modules that define an exception, by dotted identifier."""
    modules: dict[str, tuple[str, ...]] = {}
    for path in sorted(root.rglob("*.py")):
        classes = exception_classes(ast.parse(path.read_text(encoding="utf-8")))
        if classes:
            modules[_identifier(path, root)] = classes
    return modules


def _identifier(path: Path, root: Path) -> str:
    """The dotted identifier of one module of the package."""
    parts = path.relative_to(root.parent).with_suffix("").parts
    return ".".join(part for part in parts if part != "__init__")


def refusal(module: str, classes: tuple[str, ...]) -> str:
    """Why a reload is refused, and what to do instead of it."""
    return (
        f"{module} defines {', '.join(classes)}. The reload re-creates those classes and leaves "
        f"every other module holding the old ones, so an exception this module raises stops "
        f"matching the class an untouched file compares it against. Read the state in a child "
        f"interpreter instead of reloading the module in this session."
    )


def reloads_of_unsafe_modules(source: str, unsafe: Collection[str]) -> tuple[tuple[int, str], ...]:
    """The lines of one source that reload a module defining an exception.

    Only the import statements of that source are resolved, so a module reached
    through a local alias is reported by nothing here; the runtime guard reads the
    module object itself and refuses it either way.
    """
    tree = ast.parse(source)
    imported = _imported_modules(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _callee_identifier(node) != "reload" or not node.args:
            continue
        target = _module_of(node.args[0], imported)
        if target is not None and target in unsafe:
            found.append((node.lineno, target))
    return tuple(found)


def in_place_reexecutions(source: str) -> tuple[tuple[int, str], ...]:
    """The lines of one source that run a module's source again in its own namespace.

    Such a re-execution re-creates the classes the module defines without a reload
    happening, so the guard has nothing to refuse. It is read from the source here
    instead of being left to surface as a failure in a file the change never touched.
    """
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _callee_identifier(node) != "exec":
            continue
        for argument in (*node.args[1:], *(keyword.value for keyword in node.keywords)):
            module = _module_namespace_of(argument)
            if module is not None:
                found.append((node.lineno, module))
    return tuple(found)


def _module_namespace_of(node: ast.expr) -> str | None:
    """The module whose own namespace one expression hands over, as a dotted path,
    or an empty string when the path is not readable from the source."""
    if isinstance(node, ast.Attribute) and node.attr == "__dict__":
        return _dotted(node.value) or ""
    if isinstance(node, ast.Call) and _callee_identifier(node) == "vars" and node.args:
        return _dotted(node.args[0]) or ""
    return None


def _callee_identifier(call: ast.Call) -> str:
    target = call.func
    return target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")


def _imported_modules(tree: ast.AST) -> dict[str, str]:
    """The modules one source imports, by the identifier it binds them to."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound[alias.asname or alias.name.split(".")[0]] = alias.asname or alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                bound[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bound


def _module_of(node: ast.expr, imported: Mapping[str, str]) -> str | None:
    """The module one expression denotes: an imported identifier, or a dotted path."""
    dotted = _dotted(node)
    if dotted is None:
        return None
    return imported.get(dotted, dotted)


def _dotted(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def activate() -> None:
    """Refuse a reload of a module that defines an exception, from here on."""
    global _ACTIVE
    if _ACTIVE:
        return
    unsafe = unsafe_modules()

    def reload_guarded(module: ModuleType) -> ModuleType:
        qualified = getattr(module, "__name__", "")
        if qualified in unsafe:
            raise ReloadUnsafeError(refusal(qualified, unsafe[qualified]))
        return _ORIGINAL_RELOAD(module)

    importlib.reload = reload_guarded
    _ACTIVE = True


def pytest_configure(config: object) -> None:
    """Arm the guard for a session that loads this module as a plugin."""
    activate()

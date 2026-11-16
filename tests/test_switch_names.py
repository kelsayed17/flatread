"""One underscore name, one kind: a method on every piece that has it, or a value on every piece that has it.

The readers compose by multiple inheritance, so a name that is a method on one piece and a value on another
(``LongFile._record()`` and ``XmlFile._record``, a tag) cannot live on one class: the value replaces the method, and
nothing raises where the reader is built. A value counts wherever it is set: a class attribute, a declared one, or one
an ``__init__`` sets on ``self``.
"""

import annotationlib
import ast
import collections
import importlib
import inspect
import pkgutil
import textwrap

import flatread


def _pieces() -> list[type]:
    """Return every class flatread's modules define."""
    found: list[type] = []
    for info in pkgutil.iter_modules(flatread.__path__, "flatread."):
        module = importlib.import_module(info.name)
        found += [cls for cls in vars(module).values() if inspect.isclass(cls) and cls.__module__ == info.name]
    return found


def _kinds(cls: type) -> dict[str, str]:
    """Return each single-underscore name *cls* itself sets, declares or sets on ``self``: a method or a value."""
    kinds = dict.fromkeys(annotationlib.get_annotations(cls, format=annotationlib.Format.FORWARDREF), "value")
    for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(cls)))):
        targets = (
            node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        kinds |= {
            target.attr: "value"
            for target in targets
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self"
        }
    kinds |= {
        name: "method" if inspect.isroutine(value) or isinstance(value, property) else "value"
        for name, value in vars(cls).items()
    }
    return {name: kind for name, kind in kinds.items() if name.startswith("_") and not name.startswith("__")}


def test_no_name_is_a_method_on_one_piece_and_a_value_on_another() -> None:
    seen: dict[str, dict[str, list[str]]] = collections.defaultdict(lambda: collections.defaultdict(list))
    for cls in _pieces():
        for name, kind in _kinds(cls).items():
            seen[name][kind].append(cls.__qualname__)
    both = [
        f"{name}: a method on {kinds['method']}, a value on {kinds['value']}"
        for name, kinds in seen.items()
        if len(kinds) == 2
    ]
    assert not both, "\n".join(sorted(both))

"""Every public function, method and constructor flatread defines has a signature that resolves at run time.

On Python 3.14 annotations are evaluated when read, so a name imported only under ``TYPE_CHECKING`` makes
``inspect.signature`` raise ``NameError``, and with it ``help()``, IDE hovers and anything that builds on signatures.
"""

import importlib
import inspect
import pkgutil

import flatread


def test_every_public_signature_resolves() -> None:
    unresolved = []
    for info in pkgutil.iter_modules(flatread.__path__, "flatread."):
        module = importlib.import_module(info.name)
        for name, value in vars(module).items():
            if name.startswith("_") or getattr(value, "__module__", None) != module.__name__:
                continue
            members = vars(value).items() if inspect.isclass(value) else [(name, value)]
            for member, routine in members:
                if (member.startswith("_") and member != "__init__") or not inspect.isroutine(routine):
                    continue
                try:
                    inspect.signature(routine, eval_str=True)
                except NameError as error:
                    unresolved.append(f"{info.name}.{name}.{member}: {error}")
    assert not unresolved, "\n".join(unresolved)

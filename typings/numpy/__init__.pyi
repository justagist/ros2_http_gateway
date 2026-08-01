"""Deliberately permissive stub that shadows numpy's own.

numpy ships stubs written in PEP 695 syntax, which mypy can only parse when targeting
Python 3.12 or newer. Targeting 3.12 would mean mypy could no longer catch our own code
using something that does not exist on Humble's 3.10 -- and it did not catch exactly that
(``datetime.UTC``, added in 3.11), which reached CI instead.

So numpy is stubbed out here and ``python_version`` stays at the real floor. Nothing in this
package needs numpy type-checked: it appears as one namespace entry handed to generated code.
"""

from typing import Any

def __getattr__(name: str) -> Any: ...

"""Single source of truth for the package version.

``pyproject.toml`` reads this attribute (``[tool.setuptools.dynamic]``), ``setup.py``
executes this file for the ament build, and ``package.xml`` mirrors the value under a
test that fails on drift. Do not hard-code the version anywhere else.
"""

__version__ = "0.1.0"

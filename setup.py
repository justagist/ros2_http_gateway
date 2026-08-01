"""Thin shim so ``colcon build`` (ament_python) can build this package.

All distribution metadata lives in ``pyproject.toml``; nothing here duplicates it.
This file exists only because ament_python invokes ``setup.py`` directly and needs
``data_files`` entries for the ROS 2 resource index, which cannot be expressed in
``pyproject.toml`` on the older setuptools shipped with Humble.

The version is imported from the single source of truth,
``ros2_http_gateway/_version.py``, which ``pyproject.toml`` also reads.
"""

from __future__ import annotations

import runpy
from pathlib import Path

from setuptools import setup

PACKAGE_NAME = "ros2_http_gateway"
_HERE = Path(__file__).parent

# Executed rather than imported so that setup.py never pulls the package (and hence
# never touches rclpy) at build time.
__version__: str = runpy.run_path(str(_HERE / PACKAGE_NAME / "_version.py"))["__version__"]

setup(
    name="ros2-http-gateway",
    version=__version__,
    # The one metadata field that cannot live in pyproject.toml. colcon identifies a
    # package by running `setup.py --dry-run` and calling ast.literal_eval() on
    # repr(dist.__dict__); setuptools turns a pyproject `requires-python` into a
    # packaging.SpecifierSet whose repr ("<SpecifierSet('>=3.10')>") is not a Python
    # literal, so colcon dies with a SyntaxError before the build starts. Passed as a
    # plain string here, it stays a string. test_packaging.py pins this down.
    python_requires=">=3.10",
    packages=[PACKAGE_NAME],
    package_data={PACKAGE_NAME: ["py.typed", "config.schema.json"]},
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{PACKAGE_NAME}"]),
        (f"share/{PACKAGE_NAME}", ["package.xml"]),
        (f"share/{PACKAGE_NAME}/examples", ["examples/dev.yaml", "examples/locked_down.yaml"]),
    ],
    entry_points={
        "console_scripts": [
            f"ros2-http-gateway = {PACKAGE_NAME}.cli:main",
            f"gateway = {PACKAGE_NAME}.cli:main",
        ],
    },
    zip_safe=True,
)

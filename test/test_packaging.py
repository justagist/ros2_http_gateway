"""Guards the dual-install contract: one version string, one set of entry points."""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any

from ros2_http_gateway._version import __version__

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on the 3.10 CI leg
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_text() -> str:
    return (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")


def _pyproject() -> dict[str, Any]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_package_xml_version_matches_single_source() -> None:
    """package.xml mirrors ros2_http_gateway/_version.py.

    The ROS package format requires a literal version, so it cannot read the Python
    attribute. This test is the mechanism that keeps the mirror honest.
    """
    root = ElementTree.parse(REPO_ROOT / "package.xml").getroot()
    node = root.find("version")
    assert node is not None and node.text is not None
    assert node.text.strip() == __version__


def test_pyproject_does_not_hard_code_a_version() -> None:
    """pyproject.toml must read the version dynamically, never restate it."""
    assert "version" in _pyproject()["project"]["dynamic"]
    assert re.search(r'(?m)^\s*version\s*=\s*"\d', _pyproject_text()) is None


def test_version_is_pep440() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[.\-+].+)?", __version__)


def test_package_xml_declares_ament_python_and_rclpy() -> None:
    """rclpy is a ROS-side dependency only; it must never become a pip dependency."""
    root = ElementTree.parse(REPO_ROOT / "package.xml").getroot()
    build_types = [e.text for e in root.iterfind("export/build_type")]
    assert build_types == ["ament_python"]

    exec_depends = {e.text for e in root.iterfind("exec_depend")}
    assert "rclpy" in exec_depends

    project = _pyproject()["project"]
    runtime_deps = project["dependencies"] + [
        dep for group in project["optional-dependencies"].values() for dep in group
    ]
    assert not [dep for dep in runtime_deps if "rclpy" in dep]


def test_ament_resource_marker_exists() -> None:
    assert (REPO_ROOT / "resource" / "ros2_http_gateway").is_file()


def test_requires_python_stays_out_of_pyproject() -> None:
    """Regression guard for a colcon crash, not a style preference.

    colcon identifies the package by ``ast.literal_eval(repr(dist.__dict__))``. A
    pyproject ``requires-python`` becomes a ``packaging.SpecifierSet``, whose repr is not
    a Python literal, and the build dies with a SyntaxError before it starts.
    """
    assert "requires-python" in _pyproject()["project"]["dynamic"]
    assert 'python_requires=">=3.10"' in (REPO_ROOT / "setup.py").read_text(encoding="utf-8")


def test_console_scripts_install_where_ros2_run_looks() -> None:
    """``ros2 run <pkg> <exe>`` only searches lib/<pkg>/, never bin/."""
    cfg = (REPO_ROOT / "setup.cfg").read_text(encoding="utf-8")
    assert "script_dir = $base/lib/ros2_http_gateway" in cfg
    assert "install_scripts = $base/lib/ros2_http_gateway" in cfg


def test_exactly_one_console_script_and_it_is_namespaced() -> None:
    """A bare `gateway` alias would claim ~/.local/bin/gateway on every pip install.

    Wheel installs generate console scripts from these same entry points, and setup.cfg's
    install_scripts redirect only applies to the colcon/setup.py path. So the alias cannot
    exist without grabbing a generic name on the user's PATH.
    """
    scripts = _pyproject()["project"]["scripts"]
    assert scripts == {"ros2-http-gateway": "ros2_http_gateway.cli:main"}

    # setup.py carries the same single entry point for the ament install path.
    setup_py = (REPO_ROOT / "setup.py").read_text(encoding="utf-8")
    assert "ros2-http-gateway = " in setup_py
    assert '"gateway = ' not in setup_py and 'f"gateway = ' not in setup_py


def test_every_runtime_dependency_carries_a_justification() -> None:
    """The dep tree stays small only if adding to it requires writing down why."""
    block = _pyproject_text().split("\ndependencies = [", 1)[1].split("\n]", 1)[0]
    justified = False
    unjustified: list[str] = []
    for raw in block.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            justified = True
        elif line:
            if not justified:
                unjustified.append(line)
            justified = False
    assert not unjustified

"""The console in a real browser, against the real demo node. Marked `e2e`, outside the CI gate.

Everything else about the console is tested either side of the DOM: its logic through node, its
server through pytest. This is the wiring between them -- the part where a working manifest and
working logic still leave you with a blank page.

    pip install -e '.[dev,e2e]' && playwright install chromium
    pytest -m e2e

Two guards keep it out of CI, because it needs a 115 MB browser, starts two ROS processes and takes
about a minute. `-m 'not e2e'` in the pytest config deselects it -- but a command line passing its
own `-m` overrides that, so the second guard is the one that matters: playwright lives in its own
`e2e` extra that CI never installs, and the `importorskip` below then skips the module rather than
failing in a browserless environment.
"""

from __future__ import annotations

import contextlib
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("rclpy", reason="needs a sourced ROS 2 environment")
sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="needs playwright and a chromium download"
).sync_playwright

pytestmark = [pytest.mark.ros, pytest.mark.e2e]

REPO_ROOT = Path(__file__).resolve().parents[1]
OPERATOR = "demo-operator-token"
VIEWER = "demo-viewer-token"

# The demo node's state holds for 8 s each way, so anything waiting on a flip needs longer.
STATE_PERIOD_S = 8.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Demo:
    """The demo node plus a gateway, with the node stoppable so staleness can be tested.

    Stopping the publisher is the only honest way to watch a tile expire, and a test that leaves
    the module without one would break every test written after it. So the node is restartable
    rather than merely killable.
    """

    def __init__(self, config: Path, port: int) -> None:
        self._config = config
        self.base = f"http://127.0.0.1:{port}/api/v1"
        self._environment = {**os.environ, "PYTHONUNBUFFERED": "1"}
        self._node: subprocess.Popen[bytes] | None = None
        self._gateway = self._spawn("ros2_http_gateway", str(config), "--log-level", "warning")
        self.start_node()
        _wait_for(f"{self.base}/health")

    def _spawn(self, module: str, *arguments: str) -> Any:
        return subprocess.Popen(
            [sys.executable, "-m", module, *arguments],
            cwd=REPO_ROOT,
            env=self._environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def start_node(self) -> None:
        if self._node is None or self._node.poll() is not None:
            self._node = self._spawn("ros2_http_gateway.demo_node")
        _wait_for(
            f"{self.base}/mission/status",
            token=OPERATOR,
            until=lambda body: '"stale":false' in body,
        )

    def stop_node(self) -> None:
        if self._node is not None and self._node.poll() is None:
            self._node.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._node.wait(timeout=10)

    def close(self) -> None:
        self.stop_node()
        self._gateway.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self._gateway.wait(timeout=10)
        for process in (self._node, self._gateway):
            if process is not None and process.poll() is None:
                process.kill()


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Demo]:
    """A gateway over `examples/demo.yaml`, with the TTL shortened so eviction is watchable.

    The shipped file says 300 s, which no test should wait out; everything else is used as
    published, so a break in the example shows up here.
    """
    import yaml

    port = free_port()
    document = yaml.safe_load((REPO_ROOT / "examples" / "demo.yaml").read_text())
    document["http"]["port"] = port
    document["actions"] = {"result_ttl_s": 12}
    config = tmp_path_factory.mktemp("e2e") / "demo.yaml"
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    config.chmod(0o600)

    instance = Demo(config, port)
    try:
        yield instance
    finally:
        instance.close()


def _wait_for(
    url: str, *, token: str | None = None, until: Any = None, timeout: float = 45.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        request = urllib.request.Request(url)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with contextlib.suppress(OSError, urllib.error.URLError, urllib.error.HTTPError):
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read().decode()
                if until is None or until(body):
                    return
        time.sleep(0.25)
    raise AssertionError(f"{url} never became ready")


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch()
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def console(browser: Any, demo: Demo) -> Iterator[Any]:
    """A fresh page with a clean local storage, so the wallet starts empty every test."""
    context = browser.new_context()
    page = context.new_page()
    failures: list[str] = []
    page.on("pageerror", lambda error: failures.append(f"uncaught: {error}"))
    # Console errors, minus the ones the console provokes on purpose: the auth matrix fires
    # requests that are meant to be refused, and chromium logs every non-2xx response as an error.
    page.on(
        "console",
        lambda message: (
            failures.append(message.text)
            if message.type == "error" and "Failed to load resource" not in message.text
            else None
        ),
    )
    page.goto(f"{demo.base}/console")
    page.wait_for_selector("#connect")
    try:
        yield page
        # A console that renders but throws is not working, and nothing else here would notice.
        assert not failures, f"the page reported errors: {failures}"
    finally:
        context.close()


def connect(page: Any, token: str, label: str = "op") -> None:
    page.fill("#label", label)
    page.fill("#token", token)
    page.click("#connect")
    page.wait_for_selector(".card")


def card_for(page: Any, path: str) -> Any:
    """The card whose heading ends with this endpoint path."""
    return page.locator(
        ".card", has=page.locator("h2", has_text=re.compile(rf"\s{re.escape(path)}$"))
    )


# --------------------------------------------------------------------------------------
# the wallet and role filtering
# --------------------------------------------------------------------------------------


def test_the_page_loads_and_the_wallet_connects(console: Any) -> None:
    connect(console, OPERATOR)
    assert "demo-operator" in console.inner_text("#identity")
    assert "operator" in console.inner_text("#identity")


def test_switching_token_reduces_what_is_shown(console: Any) -> None:
    """Manifest is filtered per token; the page has to follow when the active one changes."""
    connect(console, OPERATOR, "op")
    operator_paths = console.locator(".card h2").all_inner_texts()
    assert any("/pick" in text for text in operator_paths)

    console.fill("#label", "view")
    console.fill("#token", VIEWER)
    console.click("#connect")
    console.wait_for_function(
        "() => document.querySelectorAll('.card').length > 0 && "
        "![...document.querySelectorAll('.card h2')].some(h => h.textContent.includes('/pick'))"
    )
    viewer_paths = console.locator(".card h2").all_inner_texts()
    assert all("/pick" not in text for text in viewer_paths)
    assert all("/chatter/say" not in text for text in viewer_paths)
    assert any("/mission/status" in text for text in viewer_paths)

    # Both chips are in the wallet, and switching back restores the wider view.
    assert console.locator(".token-chip").count() == 2
    console.locator(".token-chip", has_text="op").first.click()
    console.wait_for_function(
        "() => [...document.querySelectorAll('.card h2')]"
        ".some(h => h.textContent.includes('/pick'))"
    )


# --------------------------------------------------------------------------------------
# the auth matrix
# --------------------------------------------------------------------------------------


def test_the_auth_matrix_renders_the_grid(console: Any) -> None:
    """Four identities against a write endpoint, and the probe must not publish anything."""
    connect(console, OPERATOR, "op")
    console.fill("#label", "view")
    console.fill("#token", VIEWER)
    console.click("#connect")
    console.locator(".token-chip", has_text="op").first.click()
    console.wait_for_function(
        "() => [...document.querySelectorAll('.card h2')]"
        ".some(h => h.textContent.includes('/pick'))"
    )

    card = card_for(console, "/chatter/say")
    card.locator("summary").click()
    card.locator(".matrix button", has_text="Run").click()
    card.locator(".matrix tr").nth(4).wait_for()

    rows = [row.split("\t") for row in card.locator(".matrix tr").all_inner_texts()[1:]]
    by_identity = {row[0]: row[1] for row in rows}
    assert by_identity["no token"] == "401"
    assert by_identity["bad token"] == "401"
    assert by_identity["op"] == "400", "authorised, and the probe body was refused"
    assert by_identity["view"] == "403"
    assert "nothing executed" in card.locator(".matrix tr", has_text="op").first.inner_text()


# --------------------------------------------------------------------------------------
# topic tiles
# --------------------------------------------------------------------------------------


def test_a_tile_reads_and_shows_its_countdown(console: Any) -> None:
    connect(console, OPERATOR)
    card = card_for(console, "/chatter")
    card.locator("button", has_text="GET").click()
    card.locator(".sample-status", has_text="seq").wait_for()

    status = card.locator(".sample-status").inner_text()
    assert "stale in" in status, status
    assert "fresh" in status or "ageing" in status, status
    assert card.locator("pre.sample").inner_text().startswith("{")


def test_a_write_round_trips_into_its_read_tile(console: Any) -> None:
    connect(console, OPERATOR)
    marker = f"e2e-{int(time.time())}"

    write = card_for(console, "/chatter/say")
    write.locator(".editor input").first.fill(marker)
    write.locator("button", has_text="Publish").click()
    write.locator(".result-line", has_text="200").wait_for()
    assert "success" in write.locator(".result-body").inner_text()

    read = card_for(console, "/chatter")
    read.locator("button", has_text="GET").click()
    read.locator("pre.sample", has_text=marker).wait_for(timeout=5000)


# --------------------------------------------------------------------------------------
# the gate, against the state it gates on
# --------------------------------------------------------------------------------------


def test_the_gate_box_predicts_the_call_and_flips_with_the_state(console: Any) -> None:
    """The acceptance criterion: watch it flip both ways, and agree with the call both times.

    The demo cycles IDLE/RUNNING every 8 s, so this waits for each phase rather than assuming
    which one it starts in.
    """
    connect(console, OPERATOR)
    card = card_for(console, "/mission/{command}")
    card.locator(".params input").fill("start")

    for expected_verdict, expected_outcome in (("allowed", "ok"), ("blocked", "gated")):
        card.locator(f".gate.gate-{expected_verdict}").wait_for(timeout=20_000)
        gate = card.locator(".gate").inner_text()
        card.locator("button", has_text="Call").click()
        card.locator(f".result.outcome-{expected_outcome}").wait_for(timeout=10_000)

        result = card.locator(".result-line").inner_text()
        if expected_outcome == "ok":
            assert "success" in result, f"{gate} -> {result}"
        else:
            assert "state precondition" in result, f"{gate} -> {result}"
            assert "PRECONDITION_FAILED" in card.locator(".result-body").inner_text()

        # And the audit hint follows the same distinction.
        hint = card.locator(".result .audit-hint").inner_text()
        assert ("decision=allow" if expected_outcome == "ok" else "decision=deny") in hint, hint


# --------------------------------------------------------------------------------------
# goal cards
# --------------------------------------------------------------------------------------


def test_a_goal_runs_to_a_result_with_feedback(console: Any) -> None:
    connect(console, OPERATOR)
    card = card_for(console, "/pick")
    card.locator(".editor input").first.fill("8")
    card.locator("button", has_text="Submit goal").click()

    goal = card.locator(".goal").first
    goal.locator(".goal-feedback", has_text="feedback").wait_for(timeout=20_000)
    goal.locator(".goal-status", has_text="succeeded").wait_for(timeout=30_000)
    assert "sequence" in goal.locator(".goal-result").inner_text()


def test_cancelling_a_goal_and_then_cancelling_again(console: Any) -> None:
    connect(console, OPERATOR)
    card = card_for(console, "/pick")
    card.locator(".editor input").first.fill("30")
    card.locator("button", has_text="Submit goal").click()

    goal = card.locator(".goal").first
    goal.locator(".goal-status", has_text="executing").wait_for(timeout=20_000)
    goal.locator("button", has_text="Cancel").click()
    goal.locator(".goal-cancel", has_text="requested").wait_for(timeout=10_000)
    goal.locator(".goal-status", has_text="canceled").wait_for(timeout=20_000)

    # A second cancel is answered, not silently dropped -- and on its own line, because the
    # status keeps reporting what the goal actually is.
    goal.locator("button", has_text="Cancel").click()
    goal.locator(".goal-cancel", has_text="ALREADY_TERMINAL").wait_for(timeout=10_000)
    assert "canceled" in goal.locator(".goal-status").inner_text()


def test_a_finished_goal_is_evicted_and_then_answers_404(console: Any) -> None:
    """`result_ttl_s` is 12 in this fixture's config, so the eviction is watchable."""
    connect(console, OPERATOR)
    card = card_for(console, "/pick")
    card.locator(".editor input").first.fill("3")
    card.locator("button", has_text="Submit goal").click()

    goal = card.locator(".goal").first
    goal.locator(".goal-status", has_text="succeeded").wait_for(timeout=25_000)
    goal.locator(".goal-ttl", has_text="evicted in").wait_for(timeout=5_000)
    goal.locator(".goal-status", has_text="evicted").wait_for(timeout=30_000)


# --------------------------------------------------------------------------------------
# the event stream
# --------------------------------------------------------------------------------------


def test_the_stream_delivers_over_fetch_and_over_eventsource(console: Any) -> None:
    """Both transports, since only one of them can carry a bearer header."""
    connect(console, OPERATOR)
    panel = console.locator("section.stream")

    for transport in ("fetch", "eventsource"):
        panel.locator("select").first.select_option(transport)
        panel.get_by_role("button", name="Connect", exact=True).click()
        panel.locator(".stream-line", has_text="/mission/status").first.wait_for(timeout=20_000)
        assert "frames" in panel.locator(".stream-stats").inner_text()
        panel.get_by_role("button", name="Disconnect", exact=True).click()


def test_heartbeats_are_visible_over_fetch_and_not_over_eventsource(
    console: Any, demo: Demo
) -> None:
    """The cost of the query-token transport, made visible rather than papered over.

    A heartbeat only arrives after 15 s of silence, so the publisher has to stop first -- and
    `EventSource` never surfaces comment frames at all, which is the point of the comparison.
    """
    connect(console, OPERATOR)
    panel = console.locator("section.stream")
    demo.stop_node()
    try:
        panel.locator("select").first.select_option("fetch")
        panel.get_by_role("button", name="Connect", exact=True).click()
        panel.locator(".stream-line", has_text="heartbeat").first.wait_for(timeout=25_000)
        panel.get_by_role("button", name="Disconnect", exact=True).click()

        panel.locator("select").first.select_option("eventsource")
        panel.get_by_role("button", name="Connect", exact=True).click()
        panel.locator(".stream-line", has_text="not visible on this transport").wait_for(
            timeout=10_000
        )
        time.sleep(18)
        lines = panel.locator(".stream-line").all_inner_texts()
        assert not [line for line in lines if line == "heartbeat"], lines
        panel.get_by_role("button", name="Disconnect", exact=True).click()
    finally:
        demo.start_node()


def test_slow_consumer_mode_stops_the_log_and_resumes(console: Any) -> None:
    """What the mode does locally. Whether the *gateway* then drops or disconnects is its own
    decision, tested server-side in `test_sse.py`: at the demo's few frames per second, filling
    the kernel's socket buffers to reach that point would take minutes.
    """
    connect(console, OPERATOR)
    panel = console.locator("section.stream")
    panel.locator("select").first.select_option("fetch")
    panel.get_by_role("button", name="Connect", exact=True).click()
    panel.locator(".stream-line", has_text="/mission/status").first.wait_for(timeout=20_000)

    checkbox = panel.locator("input[type=checkbox]")
    checkbox.check()
    time.sleep(1.0)
    paused_at = panel.locator(".stream-line").count()
    time.sleep(2.5)
    assert panel.locator(".stream-line").count() == paused_at, "paused reader still logged frames"

    checkbox.uncheck()
    console.wait_for_function(
        "count => document.querySelectorAll('.stream-line').length > count",
        arg=paused_at,
        timeout=15_000,
    )
    panel.get_by_role("button", name="Disconnect", exact=True).click()


# --------------------------------------------------------------------------------------
# staleness, last because it stops the publisher
# --------------------------------------------------------------------------------------


def test_a_tile_goes_stale_when_nothing_publishes(console: Any, demo: Demo) -> None:
    """Stopping the publisher is the only honest way to watch a tile expire."""
    connect(console, OPERATOR)
    card = card_for(console, "/mission/status")
    card.locator("input[type=checkbox]").check()
    card.locator(".panel.sample-fresh").wait_for(timeout=10_000)

    demo.stop_node()
    try:
        # `stale_after_ms` is 3000 in the shipped example.
        card.locator(".panel.sample-stale").wait_for(timeout=15_000)
        assert "stale" in card.locator(".sample-status").inner_text()
    finally:
        demo.start_node()

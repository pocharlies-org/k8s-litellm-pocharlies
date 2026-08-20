"""Contrato de admision acotada del bridge ChatGPT/Codex.

El upstream limita rafagas antes de agotar la cuota semanal. Dos replicas sin
gate multiplicaban los streams y el ThreadingHTTPServer retenia un hilo por cada
peticion durante hasta 600 s. Este contrato ejecuta la clase real del ConfigMap:
dos slots por pod, ocho waiters FIFO, timeout de 600 s y liberacion en todos los
caminos mediante el context manager que envuelve cada handler.
"""

import hashlib
import threading
import time
import types
from pathlib import Path

import pytest
import yaml


BRIDGE_YAML = Path(__file__).resolve().parents[1] / "k8s" / "codex-bridge.yaml"


def _documents():
    return [doc for doc in yaml.safe_load_all(BRIDGE_YAML.read_text()) if doc]


def _resource(kind, name):
    matches = [
        doc
        for doc in _documents()
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1
    return matches[0]


@pytest.fixture(scope="module")
def bridge():
    source = _resource("ConfigMap", "codex-bridge-code")["data"]["bridge.py"]
    module = types.ModuleType("codex_bridge_admission_contract")
    exec(compile(source, "<bridge.py>", "exec"), module.__dict__)
    return module


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition did not become true")


def test_gate_is_fifo_and_rejects_beyond_the_bounded_queue(bridge):
    gate = bridge.AdmissionGate(max_inflight=1, max_queue=2, timeout_seconds=1)
    root = gate.acquire()
    order = []
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def worker(name, entered, hold=None):
        with gate.acquire():
            order.append(name)
            entered.set()
            if hold is not None:
                assert hold.wait(1)

    first = threading.Thread(
        target=worker, args=("first", first_entered, release_first), daemon=True
    )
    second = threading.Thread(
        target=worker, args=("second", second_entered), daemon=True
    )
    first.start()
    _wait_until(lambda: gate.waiting == 1)
    second.start()
    _wait_until(lambda: gate.waiting == 2)

    with pytest.raises(bridge.AdmissionBusy):
        gate.acquire()

    root.release()
    assert first_entered.wait(1)
    assert not second_entered.is_set()
    release_first.set()
    first.join(1)
    second.join(1)
    assert not first.is_alive() and not second.is_alive()
    assert order == ["first", "second"]
    assert gate.active == 0
    assert gate.waiting == 0


def test_timeout_and_exceptions_release_without_leaking_capacity(bridge):
    gate = bridge.AdmissionGate(max_inflight=1, max_queue=1, timeout_seconds=0.02)
    root = gate.acquire()
    with pytest.raises(bridge.AdmissionTimeout):
        gate.acquire()
    assert gate.active == 1
    assert gate.waiting == 0
    root.release()

    with pytest.raises(RuntimeError, match="boom"):
        with gate.acquire():
            raise RuntimeError("boom")
    assert gate.active == 0
    assert gate.waiting == 0


def test_upstream_429_starts_a_cooldown_and_propagates_retry_after(bridge):
    gate = bridge.AdmissionGate(max_inflight=1, max_queue=1, timeout_seconds=20)
    original = bridge.ADMISSION_GATE
    bridge.ADMISSION_GATE = gate
    try:
        before = time.monotonic()
        exc = types.SimpleNamespace(code=429, headers={"Retry-After": "7"})
        assert bridge.upstream_error_headers(exc) == {"Retry-After": "7"}
        assert gate._blocked_until >= before + 6.9
    finally:
        bridge.ADMISSION_GATE = original


def test_full_queue_returns_503_without_reading_the_body(bridge):
    class FullGate:
        def acquire(self):
            raise bridge.AdmissionBusy()

    original = bridge.ADMISSION_GATE
    bridge.ADMISSION_GATE = FullGate()
    try:
        handler = object.__new__(bridge.Handler)
        handler.close_connection = False
        seen = {}
        handler._error = lambda code, message, headers=None: seen.update(
            code=code, message=message, headers=headers
        )
        assert handler._acquire_admission() is None
        assert handler.close_connection is True
        assert seen["code"] == 503
        assert seen["headers"] == {"Retry-After": 5, "Connection": "close"}
    finally:
        bridge.ADMISSION_GATE = original


def test_both_inference_routes_authorize_then_hold_the_gate_around_the_body(bridge):
    source = _resource("ConfigMap", "codex-bridge-code")["data"]["bridge.py"]

    chat = source.split("    def do_POST(self):", 1)[1].split(
        "    def _do_chat_completions_admitted", 1
    )[0]
    assert chat.index("self._authorized()") < chat.index("self._acquire_admission()")
    assert "with admission:" in chat
    assert chat.index("with admission:") < chat.index(
        "self._do_chat_completions_admitted()"
    )

    responses = source.split("    def _do_responses(self):", 1)[1].split(
        "    def _do_responses_admitted", 1
    )[0]
    assert responses.index("self._authorized()") < responses.index(
        "self._acquire_admission()"
    )
    assert "with admission:" in responses

    for admitted in (
        "_do_chat_completions_admitted",
        "_do_responses_admitted",
    ):
        body = source.split(f"    def {admitted}(self):", 1)[1].split("\n        def ", 1)[0]
        assert "self.rfile.read(length)" in body


def test_deployments_pin_limits_timeout_and_configmap_hash():
    config_map = _resource("ConfigMap", "codex-bridge-code")
    payload = "".join(
        f"{key}\n{value}" for key, value in sorted(config_map["data"].items())
    )
    revision = "bridge-" + hashlib.sha256(payload.encode()).hexdigest()[:12]

    for name in ("codex-bridge", "codex-bridge-edani"):
        deployment = _resource("Deployment", name)
        template = deployment["spec"]["template"]
        assert template["metadata"]["annotations"][
            "config.k8s.e-dani.com/revision"
        ] == revision
        bridge_container = next(
            container
            for container in template["spec"]["containers"]
            if container["name"] == "bridge"
        )
        env = {entry["name"]: entry.get("value") for entry in bridge_container["env"]}
        assert env["MAX_INFLIGHT"] == "2"
        assert env["MAX_QUEUE"] == "8"
        assert env["ADMISSION_TIMEOUT_SECONDS"] == "600"
        assert env["ADMISSION_RETRY_AFTER_SECONDS"] == "5"

    source = config_map["data"]["bridge.py"]
    assert "urllib.request.urlopen(req, timeout=600)" in source

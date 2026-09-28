"""Integracion: afinidad por CUENTA de Alibaba con el Router REAL de litellm.

Corre DENTRO de la imagen pineada del proxy (ghcr.io/berriai/litellm:v1.100.0,
la del Deployment) contra una Valkey real, con el callback REAL
(litellm_strip_params.proxy_handler_instance) y session_router tal como van en
el ConfigMap. Las llamadas llevan `mock_response`: no salen a Alibaba.

Cada fase es un proceso distinto = un pod distinto (memoria propia, Valkey
compartida). Lo lanza tests/integration/run_alibaba_account_affinity.sh:

  mix   <pod>   carga aleatoria: 10 grupos, N sesiones, turnos mezclados
  move  <pod>   la cuenta de UNA sesion entra en cooldown de verdad (429 del
                deployment -> cooldown del Router) y se comprueba la mudanza
                entera; al volver la cuenta, la sesion NO regresa
  after <pod>   otro pod ve la sesion mudada en su cuenta nueva
  check         lee todas las observaciones: ninguna sesion en dos cuentas

Salida: una linea JSON por peticion en $OUT (sid, grupo, model_id, pod).
"""
import asyncio
import json
import os
import random
import sys
import time

import yaml

REPO = os.environ.get("REPO", "/repo")
OUT = os.environ.get("OUT", "/tmp/affinity-observations.jsonl")
SESSIONS = int(os.environ.get("SESSIONS", "120"))
TURNS = int(os.environ.get("TURNS", "1500"))
MOVE_SID = "ses-integracion-mudanza"
MOVE_KEY = "c" * 64


def _configmap():
    docs = [d for d in yaml.safe_load_all(open(f"{REPO}/k8s/manifest.yaml")) if d]
    return next(d for d in docs
                if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config")


def _install_modules():
    """session_router.py y litellm_strip_params.py del ConfigMap, importables."""
    cm = _configmap()
    moddir = "/tmp/affinity-mods"
    os.makedirs(moddir, exist_ok=True)
    for name in ("session_router.py", "litellm_strip_params.py", "active_request_tracking.py"):
        if name in cm["data"]:
            with open(f"{moddir}/{name}", "w") as f:
                f.write(cm["data"][name])
    sys.path.insert(0, moddir)
    return yaml.safe_load(cm["data"]["config.yaml"])


def _alibaba_model_list(config):
    out = []
    for m in config["model_list"]:
        if not str(m["model_name"]).startswith("alibaba-"):
            continue
        m = json.loads(json.dumps(m))
        params = m["litellm_params"]
        params["api_key"] = "sk-test"
        params["mock_response"] = "ok"
        params["order"] = 1          # lo que hace ensure_alibaba_key2_active con la key 2
        params.pop("timeout", None)
        m["model_info"]["cooldown_time"] = 2
        out.append(m)
    return out


def _router(config):
    import litellm
    from litellm import Router

    os.environ.setdefault("DASHSCOPE_API_KEY_2", "sk-test-2")
    import session_router  # noqa: F401 — el mismo modulo que importa strip_params
    import litellm_strip_params

    session_router.REDIS_URL = os.environ["REDIS_URL"]
    litellm.callbacks = [litellm_strip_params.proxy_handler_instance]
    router = Router(model_list=_alibaba_model_list(config), num_retries=0,
                    allowed_fails=0, cooldown_time=2)
    return router, session_router


async def _call(router, group, sid, key, pod, out):
    kwargs = dict(model=group, messages=[{"role": "user", "content": "hola"}],
                  metadata={"session_id": sid, "user_api_key_hash": key})
    try:
        resp = await router.acompletion(**kwargs)
        model_id = resp._hidden_params.get("model_id")
        ok = True
    except Exception as exc:  # el 429 provocado en la fase move
        model_id, ok = getattr(exc, "litellm_model_id", None), False
    out.write(json.dumps({"sid": sid, "key": key, "group": group, "model_id": model_id,
                          "ok": ok, "pod": pod, "t": time.time()}) + "\n")
    return model_id, ok


def _acct(model_id):
    return str(model_id).rsplit("-", 1)[-1] if model_id else None


async def phase_mix(pod):
    router, _ = _router(_install_modules())
    groups = sorted({m["model_name"] for m in router.model_list})
    rnd = random.Random(pod)
    with open(OUT, "a") as out:
        sem = asyncio.Semaphore(32)

        async def one():
            async with sem:
                sid = f"ses-{rnd.randrange(SESSIONS)}"
                key = rnd.choice(["a" * 64, "b" * 64])
                await _call(router, rnd.choice(groups), sid, key, pod, out)

        await asyncio.gather(*(one() for _ in range(TURNS)))


async def phase_move(pod):
    router, sr = _router(_install_modules())
    groups = sorted({m["model_name"] for m in router.model_list})
    with open(OUT, "a") as out:
        first, _ = await _call(router, "alibaba-q38-max", MOVE_SID, MOVE_KEY, pod, out)
        home = _acct(first)
        other = "k2" if home == "k1" else "k1"
        # La cuenta de la sesion devuelve 429 en q38-max: fallo real -> cooldown real.
        broken = next(d for d in router.model_list
                      if d["model_info"]["id"] == f"alibaba-q38-max-{home}")
        broken["litellm_params"]["mock_response"] = "litellm.RateLimitError"
        _, ok = await _call(router, "alibaba-q38-max", MOVE_SID, MOVE_KEY, pod, out)
        assert not ok, "el 429 provocado tenia que fallar"
        moved, ok = await _call(router, "alibaba-q38-max", MOVE_SID, MOVE_KEY, pod, out)
        assert ok and _acct(moved) == other, f"no se mudo: {moved}"
        # La cuenta vieja se recupera (fin del cooldown, respuesta sana otra vez).
        broken["litellm_params"]["mock_response"] = "ok"
        await asyncio.sleep(3)
        for g in groups:
            for _ in range(3):
                mid, ok = await _call(router, g, MOVE_SID, MOVE_KEY, pod, out)
                assert ok and _acct(mid) == other, f"volvio a {home} en {g}: {mid}"
        assert sr.ACCOUNT_AFFINITY_STATS["moves"] == 1, sr.ACCOUNT_AFFINITY_STATS
    with open(OUT + ".moved", "w") as f:
        f.write(other)


async def phase_after(pod):
    router, _ = _router(_install_modules())
    groups = sorted({m["model_name"] for m in router.model_list})
    expect = open(OUT + ".moved").read().strip()
    with open(OUT, "a") as out:
        for g in groups:
            mid, ok = await _call(router, g, MOVE_SID, MOVE_KEY, pod, out)
            assert ok and _acct(mid) == expect, f"el otro pod no ve la mudanza en {g}: {mid}"


def phase_check():
    by_session = {}
    rows = [json.loads(l) for l in open(OUT)]
    moved_to = open(OUT + ".moved").read().strip()
    for r in rows:
        if not r["ok"]:
            continue
        acc = _acct(r["model_id"])
        by_session.setdefault((r["key"], r["sid"]), []).append((r["t"], acc, r["group"], r["pod"]))
    mixed = []
    for (key, sid), obs in by_session.items():
        accounts = [a for _, a, _, _ in sorted(obs)]
        if sid == MOVE_SID:
            # una sola mudanza permitida, y sin vuelta: k_home... -> k_other...
            changes = sum(1 for a, b in zip(accounts, accounts[1:]) if a != b)
            if changes > 1 or accounts[-1] != moved_to:
                mixed.append((sid, accounts))
        elif len(set(accounts)) != 1:
            mixed.append((sid, accounts))
    groups = {g for obs in by_session.values() for _, _, g, _ in obs}
    pods = {p for obs in by_session.values() for _, _, _, p in obs}
    accounts = {}
    for obs in by_session.values():
        accounts[obs[0][1]] = accounts.get(obs[0][1], 0) + 1
    summary = {"requests": len(rows), "sessions": len(by_session), "groups": len(groups),
               "pods": sorted(pods), "sessions_per_account": accounts, "mixed": mixed[:5]}
    print(json.dumps(summary))
    assert not mixed, f"{len(mixed)} sesiones mezclaron cuenta"
    assert len(groups) == 10 and len(pods) >= 2
    assert len(accounts) == 2 and min(accounts.values()) > 0.3 * len(by_session)


if __name__ == "__main__":
    phase = sys.argv[1]
    if phase == "check":
        phase_check()
    else:
        asyncio.run({"mix": phase_mix, "move": phase_move, "after": phase_after}[phase](sys.argv[2]))
        print(f"{phase} {sys.argv[2]}: ok")

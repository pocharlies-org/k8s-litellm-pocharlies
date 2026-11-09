"""19-10-2026: ninguna entrada que apunte a un Service del residente aparca en el Router.

Dani: el residente no tiene otro modelo de respaldo que lo cubra, y un cooldown de 900 s
(o de 300 s, el default) deja a todo el perfil en 429 `all_deployments_in_cooldown` mientras
/health sigue en 200. Lo que saca un motor caido de la rotacion es la salud
(`live_backends`), no el Router.

Cubre cada entrada del model_list (ya resuelta por YAML, con sus anclas) cuyo `api_base`
apunta a `qwen38-flash-next*` o `tooling`: `cooldown_time: 0` en model_info (nunca en
litellm_params, que se copia a la llamada al proveedor) y `allowed_fails_policy` con las nueve
excepciones de `cooldown_handlers._EXCEPTION_POLICY_FIELDS` a un techo que no se alcanza.
El fuente de la fork (`litellm/router_utils/cooldown_handlers.py`, pineada en 160cffc07b)
es la verdad de ese nombre de campos; esta lista es su copia y el test la fija.
"""
import re

import pytest
import yaml

from test_aurora_fallback_policy_contract import MANIFEST

RESIDENTE_API_BASE = re.compile(r"^http://(qwen38-flash-next(-tp[12])?|tooling)\.llm\.svc\.cluster\.local:8000/")
CAMPOS_POLITICA = (
    "ContentPolicyViolationErrorAllowedFails",
    "BadRequestErrorAllowedFails",
    "AuthenticationErrorAllowedFails",
    "TimeoutErrorAllowedFails",
    "RateLimitErrorAllowedFails",
    "InternalServerErrorAllowedFails",
    "ServiceUnavailableErrorAllowedFails",
    "BadGatewayErrorAllowedFails",
    "NotFoundErrorAllowedFails",
)
TECHO_MINIMO = 10**9


def _config():
    return next(
        yaml.safe_load(doc["data"]["config.yaml"])
        for doc in yaml.safe_load_all(MANIFEST.read_text())
        if doc
        and doc.get("kind") == "ConfigMap"
        and doc["metadata"]["name"] == "litellm-config"
    )


def _entradas_residente():
    return [
        m for m in _config()["model_list"]
        if RESIDENTE_API_BASE.match((m.get("litellm_params") or {}).get("api_base", ""))
    ]


def test_hay_entradas_del_residente_que_cubrir():
    nombres = {m["model_name"] for m in _entradas_residente()}
    assert {"qwen38-flash-next", "qwen38-flash-next-uncensored", "correo-clasificador",
            "tooling", "tooling-uncensored", "qwen38-off", "qwen38-u-off"} <= nombres


@pytest.mark.parametrize("entrada", _entradas_residente(), ids=lambda m: m["model_name"])
def test_el_residente_no_aparca_nunca(entrada):
    info = entrada.get("model_info") or {}
    assert info.get("cooldown_time") == 0, f"{entrada['model_name']}: cooldown_time {info.get('cooldown_time')}"
    assert "cooldown_time" not in (entrada.get("litellm_params") or {}), (
        f"{entrada['model_name']}: cooldown_time en litellm_params se copia a la llamada al proveedor"
    )


@pytest.mark.parametrize("entrada", _entradas_residente(), ids=lambda m: m["model_name"])
def test_la_politica_de_fallos_cubre_todas_las_excepciones(entrada):
    politica = (entrada.get("model_info") or {}).get("allowed_fails_policy") or {}
    faltan = [c for c in CAMPOS_POLITICA if c not in politica]
    assert not faltan, f"{entrada['model_name']}: sin {faltan}"
    bajos = {c: politica[c] for c in CAMPOS_POLITICA if politica[c] < TECHO_MINIMO}
    assert not bajos, f"{entrada['model_name']}: techo demasiado bajo {bajos}"

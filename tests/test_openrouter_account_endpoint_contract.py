"""GET /openrouter/account: saldo, cupo y fallo degradado de la cuenta de OpenRouter.

POR QUE EXISTE ESTE FICHERO. El saldo se puede leer a mano con la key del secret
(`GET https://openrouter.ai/api/v1/credits` responde 200 con `is_management_key:false`
— medido el 26-08). Pero eso obliga a sacar la key de Vault para mirar un numero, y
el panel no puede tener esa credencial. La ruta la lee del entorno del proceso de
LiteLLM — donde ya esta, porque el model_list la usa como `os.environ/OPENROUTER_API_KEY`
— y devuelve SOLO cifras.

Lo que este test fija, en orden de lo que cuesta equivocarse:

  1. **La key no sale nunca.** Ni en el payload, ni en el motivo de un fallo. Un
     payload que filtre `Authorization` es un secret en un panel publico.
  2. **Fallo degradado, jamas 500.** OpenRouter es un tercero. Si no responde, la
     respuesta es 200 con `status:"unreachable"` y los numeros a `None`. Un 500 no es
     "no hay dato": es la tarjeta del panel cayendose a trozos en cada refresco.
  3. **El cupo no es uno, son dos.** 20 req/min fijo, y 50 o 1000 req/dia segun se
     hayan comprado creditos ALGUNA VEZ (permanente, no segun el saldo que quede).
     Confundirlos es decirle a alguien que tiene 1000 cuando tiene 50.
  4. **Un `/key` caido no se disfraza de "gasto de hoy = 0"**, y un `is_free_tier`
     deducido se marca como deducido.

El codigo que se prueba VIVE en el ConfigMap (es la copia que corre en el pod); se
extrae de ahi, no se duplica en un .py suelto — mismo criterio que
`test_refusal_dial.py`.
"""
import asyncio
import ast
import json
import os
import textwrap
import time
import types
from pathlib import Path

import httpx
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "manifest.yaml"

WANT_FN = {
    "_openrouter_credits_to_usd",
    "_openrouter_account_payload",
    "_openrouter_account_unreachable",
    "_openrouter_account_fetch",
    "_openrouter_account_cached",
}
WANT_CONST = {
    "OPENROUTER_ACCOUNT_PATH",
    "OPENROUTER_CREDITS_URL",
    "OPENROUTER_KEY_URL",
    "OPENROUTER_FREE_RPM_CAP",
    "OPENROUTER_FREE_DAILY_CAP_UNFUNDED",
    "OPENROUTER_FREE_DAILY_CAP_FUNDED",
    "OPENROUTER_FUNDED_CREDITS_USD",
    "OPENROUTER_ACCOUNT_TTL_S",
    "OPENROUTER_ACCOUNT_TIMEOUT_S",
    # Estado mutable del bloque: sin el, el test no puede observar el TTL.
    "_openrouter_account_cache",
    "_openrouter_account_lock",
}

FAKE_KEY = "sk-or-VIRTUAL-NO-USAR-9999"


def _hook_source():
    return next(
        d["data"]["litellm_strip_params.py"]
        for d in yaml.safe_load_all(MANIFEST.read_text())
        if d and d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )


@pytest.fixture()
def hook(tmp_path, monkeypatch):
    """Reejecuta el bloque del saldo con `os.environ` y `httpx` controlados.

    Hace falta un modulo nuevo por test porque la cache del saldo es estado global
    del proceso: compartiria respuestas entre tests y dejaria de probar el TTL.
    """
    tree = ast.parse(_hook_source())
    keep = [
        n for n in tree.body
        if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in WANT_FN)
        or (isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") in WANT_CONST for t in n.targets))
    ]
    missing = WANT_FN - {n.name for n in keep
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert not missing, f"el hook ya no define: {sorted(missing)}"

    # El bloque del hook vive al fondo de un modulo que abre con
    # `from litellm... import *`. Al extraerlo hay que reponer ESAS importaciones:
    # sin ellas el codigo extraido queda con `httpx` sin definir y el test daria un
    # `unreachable` por NameError que se pareceria sospechosamente al caso real.
    # Se reponen las mismas que usa el hook de verdad, y `keep` va DESPUES para que
    # las constantes del bloque ganen.
    imports = [
        n for n in tree.body
        if isinstance(n, (ast.Import, ast.ImportFrom))
        and getattr(n, "module", "").split(".")[0]
        in {"asyncio", "json", "logging", "os", "threading", "time"}
        or (isinstance(n, ast.Import) and any(a.name == "httpx" for a in n.names))
    ]
    assert imports, "el hook ya no importa lo que este bloque necesita"

    # El namespace se ejecuta DENTRO de `mod.__dict__` y con las dependencias ya
    # dentro: el bloque hace `from X import *`, que enlaza los nombres al dict del
    # modulo. Si se inyectan despues de ejecutar, reasignar `mod.httpx` desde un test
    # no se ve dentro de `_openrouter_account_fetch`.
    mod = types.ModuleType("openrouter_account")
    mod.os = types.SimpleNamespace(environ={})
    mod.time = time
    mod.asyncio = asyncio
    mod.httpx = httpx
    exec(
        compile(ast.Module(body=imports + keep, type_ignores=[]), "<hook>", "exec"),
        mod.__dict__,
    )  # noqa: S102
    return mod


def _with_key(mod, key=FAKE_KEY):
    mod.os.environ["OPENROUTER_API_KEY"] = key


class _Response:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("upstream", request=None, response=None)


def _client_factory(responses, seen):
    """httpx.AsyncClient falso que apunta lo que le piden y con que cabeceras."""

    class _Client:
        def __init__(self, timeout=None):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            seen.append((url, dict(headers or {})))
            for needle, value in responses.items():
                if needle in url:
                    return value if isinstance(value, _Response) else _Response(value)
            raise httpx.ConnectError("no hay nada ahi")

    return _Client


# ---------------------------------------------------------------- shape del payload

def test_the_measured_credits_shape_becomes_usd_and_the_funded_cap(hook):
    """El caso REAL medido: `/credits` dice 10 comprados y 0 usados, en una cuenta
    a la que se compraron 10 USD. Con creditos comprados alguna vez, el cupo diario
    es 1000 y no 50.

    La unidad son dolares, zanjado por medida: `/credits` dice 10 en una cuenta a la
    que se compraron 10 USD. Ver `_openrouter_credits_to_usd`.
    """
    payload = hook._openrouter_account_payload(
        {"data": {"total_credits": 10, "total_usage": 0}},
        {"usage_daily": 3.2, "is_free_tier": False},
        fetched_at=1000.0,
    )
    assert payload["total_credits"] == 10.0
    assert payload["total_usage"] == 0.0
    assert payload["balance"] == 10.0
    assert payload["usage_daily"] == 3.2
    assert payload["daily_request_cap"] == 1000
    assert payload["rpm_cap"] == 20
    assert payload["status"] == "ok"


def test_credit_values_are_taken_as_usd_without_any_conversion(hook):
    """No hay conversion de unidad: lo que dice /credits es USD.

    Se prueba que un saldo != 10 pasa tal cual, que es lo que no se podia dirimir
    con la cuenta de 10 USD (10 centavos/100 y 10 dolares dan el mismo numero).
    """
    payload = hook._openrouter_account_payload(
        {"data": {"total_credits": 12.34, "total_usage": 2.5}}, {}, fetched_at=1.0
    )
    assert payload["total_credits"] == 12.34
    assert payload["total_usage"] == 2.5
    assert payload["balance"] == 9.84
    assert "credits_unit" not in payload


def test_a_never_funded_account_gets_the_50_per_day_cap(hook):
    payload = hook._openrouter_account_payload(
        {"data": {"total_credits": 0, "total_usage": 0}},
        {"usage_daily": 49, "is_free_tier": True},
        fetched_at=1.0,
    )
    assert payload["daily_request_cap"] == 50
    assert payload["is_free_tier"] is True


def test_balance_is_credits_minus_usage_not_credits(hook):
    payload = hook._openrouter_account_payload(
        {"data": {"total_credits": 10, "total_usage": 2.5}}, {}, fetched_at=1.0
    )
    assert payload["balance"] == 7.5


def test_a_dead_key_endpoint_is_reported_and_not_guessed_silently(hook):
    """Sin `/key` no hay `usage_daily`: tiene que decir `None`, no 0.

    Un 0 aqui se lee "hoy no he gastado" y es mentira: puede que no se pueda leer.
    Y el `is_free_tier` que se deduce del saldo va marcado como deducido.
    """
    payload = hook._openrouter_account_payload(
        {"data": {"total_credits": 10, "total_usage": 0}}, {}, fetched_at=1.0
    )
    assert payload["usage_daily"] is None
    assert payload["sources"] == {"credits": True, "key": False}
    assert payload["is_free_tier_source"] == "inferred_from_credits"


def test_the_inferred_free_tier_is_flagged_as_inferred(hook):
    """Un saldo a 0 NO implica 50/dia: quien compró $10 y se los gasto sigue en 1000.

    La deduccion puede fallar, y por eso se marca de donde sale el booleano.
    """
    payload = hook._openrouter_account_payload(
        {"data": {"total_credits": 0, "total_usage": 10}}, {}, fetched_at=1.0
    )
    assert payload["is_free_tier"] is True
    assert payload["is_free_tier_source"] == "inferred_from_credits"

    from_key = hook._openrouter_account_payload(
        {"data": {"total_credits": 0, "total_usage": 10}},
        {"is_free_tier": False},
        fetched_at=1.0,
    )
    assert from_key["is_free_tier"] is False
    assert from_key["is_free_tier_source"] == "key_endpoint"
    assert from_key["daily_request_cap"] == 1000


def test_the_unreachable_shape_has_no_numbers_and_keeps_the_rpm_cap(hook):
    """`unreachable` = sin cifras, pero el rpm_cap SI: es de文档, no medido."""
    payload = hook._openrouter_account_unreachable("ConnectTimeout", fetched_at=5.0)
    assert payload["status"] == "unreachable"
    assert payload["balance"] is None
    assert payload["usage_daily"] is None
    assert payload["is_free_tier"] is None
    assert payload["daily_request_cap"] is None
    assert payload["rpm_cap"] == 20


# ---------------------------------------------------------------- fetch contra la red

def test_without_the_key_it_says_missing_api_key_instead_of_zero(hook):
    """Sin credencial no hay saldo: "sin medir", no 0 USD."""
    result = asyncio.run(hook._openrouter_account_fetch())
    assert result["status"] == "unreachable"
    assert result["reason"] == "missing_api_key"
    assert result["balance"] is None


def test_the_key_is_sent_and_never_comes_back(hook):
    """La key viaja en la cabecera y NO aparece en el payload.

    Es el unico test que puede fallar de la peor manera posible: un payload con la
    key dentro es un secret publicado en el panel.
    """
    _with_key(hook)
    seen = []
    hook.httpx = types.SimpleNamespace(AsyncClient=_client_factory(
        {
            "/credits": {"data": {"total_credits": 10, "total_usage": 0}},
            "/key": {"usage_daily": 7.0, "is_free_tier": False},
        },
        seen,
    ))

    result = asyncio.run(hook._openrouter_account_fetch())

    assert result["status"] == "ok"
    assert result["balance"] == 10.0
    assert result["usage_daily"] == 7.0
    assert result["daily_request_cap"] == 1000
    # Los dos endpoints, con la cabecera puesta.
    assert [url for url, _ in seen] == [hook.OPENROUTER_CREDITS_URL, hook.OPENROUTER_KEY_URL]
    assert all(h.get("Authorization") == "Bearer " + FAKE_KEY for _, h in seen)
    # Y la key no se filtra: ni como valor, ni como substring.
    assert FAKE_KEY not in json.dumps(result)


def test_both_endpoints_down_is_a_200_unreachable_not_an_exception(hook):
    """Aqui es donde un 500 tumbaria la tarjeta del panel."""
    _with_key(hook)
    hook.httpx = types.SimpleNamespace(AsyncClient=_client_factory({}, []))
    result = asyncio.run(hook._openrouter_account_fetch())
    assert result["status"] == "unreachable"
    assert "ConnectError" in result["reason"]
    assert result["balance"] is None


def test_one_endpoint_down_is_partial_not_a_lie(hook):
    """/credits vivo y /key caido: `partial`, y `sources` dice cual falta."""
    _with_key(hook)
    hook.httpx = types.SimpleNamespace(AsyncClient=_client_factory(
        {"/credits": {"data": {"total_credits": 10, "total_usage": 0}}},
        [],
    ))
    result = asyncio.run(hook._openrouter_account_fetch())
    assert result["status"] == "partial"
    assert result["sources"] == {"credits": True, "key": False}
    assert result["balance"] == 10.0
    assert result["usage_daily"] is None


# ---------------------------------------------------------------- cache

def test_the_cache_spares_openrouter_the_per_refresh_hits(hook):
    """60 s de TTL: el panel refresca a su ritmo, OpenRouter no lo aguanta.

    Dos lecturas seguidas = DOS llamadas (credits+key) y no cuatro.
    """
    _with_key(hook)
    seen = []
    hook.httpx = types.SimpleNamespace(AsyncClient=_client_factory(
        {
            "/credits": {"data": {"total_credits": 10, "total_usage": 0}},
            "/key": {"usage_daily": 1.0, "is_free_tier": False},
        },
        seen,
    ))

    async def twice():
        first = await hook._openrouter_account_cached()
        second = await hook._openrouter_account_cached()
        return first, second

    first, second = asyncio.run(twice())
    assert len(seen) == 2, f"la red recibi {len(seen)} llamadas, debian ser 2"
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["age_seconds"] >= 0


def test_an_unreachable_result_is_cached_for_much_less_than_the_ttl(hook):
    """Si OpenRouter revive, el panel lo ve en el siguiente refresco, no un minuto despues."""
    _with_key(hook)
    hook.httpx = types.SimpleNamespace(AsyncClient=_client_factory({}, []))

    async def once():
        return await hook._openrouter_account_cached()

    result = asyncio.run(once())
    assert result["status"] == "unreachable"
    # La ventana de cache de un fallo es corta: <= 5 s frente al TTL de 60.
    remaining = hook._openrouter_account_cache["expires"] - time.monotonic()
    assert 0 < remaining <= 5.0, remaining
    assert hook.OPENROUTER_ACCOUNT_TTL_S > 5.0


# ---------------------------------------------------------------- registro de la ruta

def test_the_route_is_get_only_and_registered_once():
    """`GET /openrouter/account`, protegida contra doble registro con un flag en app.state.

    El hook puede reimportarse al recargar la configuracion y Starlette NO
    deduplica rutas: sin el flag se apilan copias identitas.
    """
    source = _hook_source()
    tree = ast.parse(source)
    register = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_register_openrouter_account_route"
    )
    text = ast.unparse(register)

    assert "app.state" in text, "sin flag en app.state el config recarga apila rutas"
    assert "openrouter_account_route" in text
    assert "@app.get" in text
    assert "@app.post" not in text and "@app.put" not in text and "@app.delete" not in text, \
        "la ruta de saldo es de SOLO LECTURA"

    handler = next(
        n for node in ast.walk(register)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_openrouter_account"
        for n in [node]
    )
    assert handler is not None


def test_the_registration_is_wrapped_so_it_cannot_stop_the_proxy():
    """El hook esta en el camino de TODAS las peticiones: una ruta de control no
    puede impedir que arranque el proxy."""
    source = _hook_source()
    lines = source.split("\n")
    # La LLAMADA (no la definicion, que no lleva `def` delante).
    idx = next(
        i for i, l in enumerate(lines)
        if "_register_openrouter_account_route()" in l and not l.strip().startswith("def ")
    )
    window = "\n".join(lines[idx - 2:idx + 4])
    assert "try:" in window, window
    assert "except" in window, window
    assert "log.warning" in window, window


def test_the_endpoint_path_is_the_one_the_dashboard_calls():
    """El contrato con el panel: `GET /openrouter/account` en el puerto 4000.

    El 4001 es el sidecar `active-requests-api` y ahi esto da 404 — el gotcha que ya
    esta documentado en `routes_llm_uncensored.py` del dashboard.
    """
    source = _hook_source()
    assert 'OPENROUTER_ACCOUNT_PATH = "/openrouter/account"' in source
    assert "https://openrouter.ai/api/v1/credits" in source
    assert "https://openrouter.ai/api/v1/key" in source

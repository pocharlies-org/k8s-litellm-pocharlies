"""Prueba el bloque del dial EXTRAIDO del ConfigMap, con httpx y router falsos."""
import json, logging, os, sys, textwrap, threading, time, types, unittest
from pathlib import Path

import yaml

# El codigo que se prueba VIVE en el ConfigMap: es la copia que corre. Se extrae
# de ahi en vez de duplicarlo en un .py suelto, que es como se acaba probando una
# version que el cluster no ejecuta.
MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

SRC = None
for d in yaml.safe_load_all(open(MANIFEST)):
    if d and d.get('kind') == 'ConfigMap' and d['metadata']['name'] == 'litellm-config':
        SRC = d['data']['litellm_strip_params.py']
lines = SRC.split('\n')
start = next(i for i, l in enumerate(lines) if l.strip().startswith('REFUSAL_RUNTIMES_DEFAULT = ['))
# Hasta JUSTO ANTES del registro de rutas: eso necesita fastapi y el proxy vivo.
# Todo lo de arriba (registro, cache, resolucion de alias, estado publicado) es
# logica pura y se prueba aqui.
end = next(i for i, l in enumerate(lines) if l.strip() == 'def _register_refusal_routes():')
BLOCK = textwrap.dedent('\n'.join(lines[start:end]))

heads = {}          # admin_url -> body
calls = {"n": 0}

class _Resp:
    def __init__(self, body): self._b = body
    def json(self): return self._b

class _AsyncClient:
    def __init__(self, timeout=None): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url):
        calls["n"] += 1
        if url not in heads: raise RuntimeError("connection refused")
        return _Resp(heads[url])

httpx_stub = types.SimpleNamespace(
    AsyncClient=_AsyncClient,
    get=lambda url, timeout=None: (_Resp(heads[url]) if url in heads
                                   else (_ for _ in ()).throw(RuntimeError("refused"))),
)

router = types.SimpleNamespace(model_list=[])
proxy_server = types.ModuleType("litellm.proxy.proxy_server")
proxy_server.llm_router = router
sys.modules.setdefault("litellm", types.ModuleType("litellm"))
sys.modules.setdefault("litellm.proxy", types.ModuleType("litellm.proxy"))
sys.modules["litellm.proxy.proxy_server"] = proxy_server

ns = {"os": os, "json": json, "time": time, "threading": threading,
      "httpx": httpx_stub, "log": logging.getLogger("t")}
exec(compile(BLOCK, "dial", "exec"), ns)

DS = "http://deepseek-v4-flash-0731.llm.svc.cluster.local:8000/admin/refusal_lambda"
QW = "http://vllm-qwen38-27b-uncensored.llm.svc.cluster.local:8000/admin/refusal_lambda"


def run(coro):
    import asyncio
    return asyncio.run(coro)


class DialTest(unittest.TestCase):
    def setUp(self):
        # Se reinstala en CADA test, no una vez al importar: otros tests de esta
        # carpeta meten sus propios stubs de litellm en sys.modules y el que quede
        # el ultimo gana. Aislado pasaba; en la suite entera el bloque veia el
        # router de otro test y la resolucion alias -> runtime salia vacia.
        sys.modules["litellm.proxy.proxy_server"] = proxy_server
        heads.clear(); calls["n"] = 0
        for slot in ns["_refusal_cache"].values():
            slot.update({"ts": 0.0, "value": None, "reason": "never_read", "raw": None})
        ns["_alias_runtime_cache"].update({"ts": 0.0, "map": {}})
        router.model_list = [
            {"model_name": "tooling", "litellm_params": {
                "api_base": "http://vllm-qwen38-27b-uncensored.llm.svc.cluster.local:8000/v1"}},
            {"model_name": "max", "litellm_params": {
                "api_base": "http://deepseek-v4-flash-0731.llm.svc.cluster.local:8000/v1"}},
            {"model_name": "cloudblue/gpt", "litellm_params": {
                "api_base": "https://api.cloudblue.example/v1"}},
        ]

    def test_registro_tiene_on_lambda_distinto_por_runtime(self):
        by = ns["REFUSAL_RUNTIMES_BY_KEY"]
        self.assertEqual(by["deepseek-v4-flash-0731"]["on_lambda"], 1.5)
        self.assertEqual(by["qwen38-27b-nvfp4"]["on_lambda"], 1.0)

    def test_rango_experimental_llega_hasta_dos_y_medio(self):
        self.assertEqual(ns["REFUSAL_MIN_LAMBDA"], 0.0)
        self.assertEqual(ns["REFUSAL_MAX_LAMBDA"], 2.5)
        rt = ns["REFUSAL_RUNTIMES_BY_KEY"]["deepseek-v4-flash-0731"]
        self.assertEqual(ns["_refusal_lambda_from_payload"](
            {"lambda": 2.5}, rt), 2.5)
        for invalid in (-0.01, 2.5001, float("nan")):
            with self.assertRaisesRegex(ValueError, "fuera de rango"):
                ns["_refusal_lambda_from_payload"]({"lambda": invalid}, rt)

    def test_enabled_solo_queda_como_compatibilidad_de_rollout(self):
        rt = ns["REFUSAL_RUNTIMES_BY_KEY"]["deepseek-v4-flash-0731"]
        self.assertEqual(ns["_refusal_lambda_from_payload"](
            {"enabled": True}, rt), 1.5)
        self.assertEqual(ns["_refusal_lambda_from_payload"](
            {"enabled": False}, rt), 0.0)

    def test_cada_runtime_sella_SU_lambda_y_no_la_del_otro(self):
        heads[DS] = {"lambda": 1.5, "consistent": True, "per_rank": [1.5, 1.5]}
        heads[QW] = {"lambda": 0.0, "consistent": True, "per_rank": [0.0]}
        self.assertEqual(run(ns["_refusal_lambda_for_async"]("max", None)),
                         (1.5, "deepseek-v4-flash-0731"))
        self.assertEqual(run(ns["_refusal_lambda_for_async"]("tooling", None)),
                         (0.0, "qwen38-27b-nvfp4"))

    def test_head_caido_no_apaga_el_sellado_del_otro(self):
        heads[QW] = {"lambda": 1.0, "consistent": True, "per_rank": [1.0]}
        self.assertEqual(run(ns["_refusal_lambda_for_async"]("max", None)),
                         (None, "deepseek-v4-flash-0731"))
        self.assertEqual(run(ns["_refusal_lambda_for_async"]("tooling", None)),
                         (1.0, "qwen38-27b-nvfp4"))

    def test_alias_ajeno_no_se_sella(self):
        heads[QW] = {"lambda": 1.0}
        self.assertEqual(run(ns["_refusal_lambda_for_async"]("cloudblue/gpt", None)),
                         (None, None))

    def test_ttl_cachea_por_runtime_sin_mezclar(self):
        heads[DS] = {"lambda": 1.5}; heads[QW] = {"lambda": 0.0}
        run(ns["_refusal_lambda_for_async"]("max", None))
        run(ns["_refusal_lambda_for_async"]("tooling", None))
        n = calls["n"]
        run(ns["_refusal_lambda_for_async"]("max", None))
        run(ns["_refusal_lambda_for_async"]("tooling", None))
        self.assertEqual(calls["n"], n, "dentro del TTL no debe repreguntar")
        self.assertEqual(run(ns["_refusal_lambda_for_async"]("max", None))[0], 1.5)

    def test_tp_inconsistente_no_sella(self):
        heads[DS] = {"lambda": 1.5, "consistent": False, "per_rank": [1.5, 0.0]}
        self.assertEqual(run(ns["_refusal_lambda_for_async"]("max", None))[0], None)

    def test_un_solo_rank_sin_consistent_si_sella(self):
        heads[QW] = {"lambda": 1.0, "consistent": True, "per_rank": [1.0]}
        del heads[QW]["consistent"]
        self.assertEqual(run(ns["_refusal_lambda_for_async"]("tooling", None))[0], 1.0)

    def test_router_vacio_conserva_el_mapa_anterior(self):
        heads[QW] = {"lambda": 1.0}
        run(ns["_refusal_lambda_for_async"]("tooling", None))
        router.model_list = []
        ns["_alias_runtime_cache"]["ts"] = 0.0
        self.assertIsNotNone(ns["_refusal_runtime_for"]("tooling", None))

    def test_api_base_explicito_gana_sin_router(self):
        heads[DS] = {"lambda": 1.5}
        router.model_list = []
        ns["_alias_runtime_cache"].update({"ts": 0.0, "map": {}})
        self.assertEqual(
            run(ns["_refusal_lambda_for_async"](
                "loquesea", "http://deepseek-v4-flash-0731.llm.svc:8000/v1")),
            (1.5, "deepseek-v4-flash-0731"))

    def test_env_override_con_json_roto_cae_al_default(self):
        os.environ["LITELLM_REFUSAL_RUNTIMES"] = "{no json"
        try:
            self.assertEqual(len(ns["_load_refusal_runtimes"]()), 2)
        finally:
            del os.environ["LITELLM_REFUSAL_RUNTIMES"]

    def test_fleet_name_sale_del_host_del_admin_url(self):
        # Es el ancla con la que el panel casa la chapa contra la fila de la
        # flota. Si deja de derivarse del Service, la chapa desaparece sin ruido.
        by = ns["REFUSAL_RUNTIMES_BY_KEY"]
        self.assertEqual(
            ns["_refusal_fleet_name"](by["deepseek-v4-flash-0731"]),
            "deepseek-v4-flash-0731")
        self.assertEqual(
            ns["_refusal_fleet_name"](by["qwen38-27b-nvfp4"]),
            "vllm-qwen38-27b-uncensored")

    def test_estado_publicado_lleva_alias_on_lambda_y_per_rank(self):
        heads[QW] = {"lambda": 1.0, "consistent": True, "per_rank": [1.0]}
        run(ns["_refusal_lambda_for_async"]("tooling", None))
        st = ns["_refusal_runtime_state"](
            ns["REFUSAL_RUNTIMES_BY_KEY"]["qwen38-27b-nvfp4"],
            ns["_refusal_aliases_by_runtime"]())
        self.assertEqual(st["lambda"], 1.0)
        self.assertTrue(st["enabled"])
        self.assertTrue(st["stamping"])
        self.assertEqual(st["on_lambda"], 1.0)
        self.assertEqual(st["min_lambda"], 0.0)
        self.assertEqual(st["max_lambda"], 2.5)
        self.assertEqual(st["per_rank"], [1.0])
        self.assertIn("tooling", st["aliases"])
        self.assertEqual(st["fleet_name"], "vllm-qwen38-27b-uncensored")

    def test_head_caido_publica_sin_lectura_y_no_censurado(self):
        # No poder leer NO es "lambda=0": pintar censurado ahi seria afirmar algo
        # que no se sabe. enabled queda None y stamping False.
        st = ns["_refusal_runtime_state"](
            ns["REFUSAL_RUNTIMES_BY_KEY"]["deepseek-v4-flash-0731"],
            ns["_refusal_aliases_by_runtime"]())
        self.assertIsNone(st["lambda"])
        self.assertIsNone(st["enabled"])
        self.assertFalse(st["stamping"])

    def test_env_override_valido_se_respeta(self):
        os.environ["LITELLM_REFUSAL_RUNTIMES"] = json.dumps([
            {"key": "solo", "admin_url": "http://x/admin", "on_lambda": 0.7,
             "api_base_match": "x"}])
        try:
            rts = ns["_load_refusal_runtimes"]()
            self.assertEqual([r["key"] for r in rts], ["solo"])
            self.assertEqual(rts[0]["on_lambda"], 0.7)
        finally:
            del os.environ["LITELLM_REFUSAL_RUNTIMES"]


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""DGX-1417: los contadores del filtro de cuentas de Claude salen a Prometheus.

CONTRACT: dgx.litellm.claude-account-stats.v1 (ancla: litellm_claude_account_stats).

El dict `CLAUDE_ACCOUNT_STATS` es lo único que dice cuántas veces el filtro muda de cuenta o deja una key
sin cuentas, y vivía solo en memoria del proceso: con el pod reiniciado se perdía, y sin histórico no se
puede separar «el salto automático sobra» de «nadie tocó el techo». Sale por el `/metrics` que el proxy ya
tiene (servicio `prometheus`, scrapeado por VictoriaMetrics), así que la serie queda con 30 días de retención.

Lo que fija este test: que el colector existe, que expone las cinco claves del dict y que sus valores son
los del dict en el momento del scrape (es un colector de lectura, no un contador que hay que acordarse de
actualizar). `prometheus_client` no está en el lock de contratos de CI, así que se le pone un stub: el
módulo debe cargar igual sin él.
"""
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEV = ROOT / "dev" / "session_router.py"

CLAVES = ("hits", "claims", "moves", "empty", "redis_errors")


def _cargar_con_prometheus_falso():
    """Ejecuta el fuente con un `prometheus_client` de mentirijillas y devuelve (módulo, registrados)."""
    registrados = []

    class FamiliaFalsa:
        def __init__(self, nombre, documentacion, labels=()):
            self.nombre, self.documentacion, self.labels = nombre, documentacion, labels
            self.metricas = []

        def add_metric(self, etiquetas, valor):
            self.metricas.append((etiquetas, valor))

    core = types.ModuleType("prometheus_client.core")
    core.CounterMetricFamily = FamiliaFalsa
    prom = types.ModuleType("prometheus_client")
    prom.core = core
    prom.REGISTRY = types.SimpleNamespace(register=lambda c: registrados.append(c))
    guardados = {n: sys.modules.get(n) for n in
                 ("litellm", "litellm.anthropic_beta_headers_manager", "httpx",
                  "prometheus_client", "prometheus_client.core")}
    sys.modules["litellm"] = types.ModuleType("litellm")
    sys.modules["litellm"].anthropic_beta_headers_manager = types.ModuleType(
        "litellm.anthropic_beta_headers_manager")
    sys.modules["litellm"].anthropic_beta_headers_manager._load_beta_headers_config = lambda: {}
    sys.modules["litellm"].anthropic_beta_headers_manager.reload_beta_headers_config = lambda: {}
    sys.modules["httpx"] = types.ModuleType("httpx")
    sys.modules["prometheus_client"] = prom
    sys.modules["prometheus_client.core"] = core
    modulo = types.ModuleType("session_router")
    exec(compile(DEV.read_text(), "session_router.py", "exec"), modulo.__dict__)

    def restaurar():
        for n, v in guardados.items():
            if v is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = v

    return modulo, registrados, restaurar


def test_el_colector_de_cuentas_sale_a_prometheus_con_las_cinco_claves():
    modulo, registrados, restaurar = _cargar_con_prometheus_falso()
    try:
        assert registrados, "el filtro de cuentas no registró su colector en Prometheus"
        for nombre, valor in modulo.CLAUDE_ACCOUNT_STATS.items():
            assert valor == 0, "los contadores arrancan a cero; si no, otro test los ensucia"
        modulo.CLAUDE_ACCOUNT_STATS["moves"] = 7
        muestras = list(registrados[0].collect())
        assert len(muestras) == 1
        familia = muestras[0]
        assert familia.nombre == "litellm_claude_account_stats", familia.nombre
        assert familia.labels == ["contador"], f"sin etiqueta no se distingue un contador de otro: {familia.labels}"
        expuesto = {etiquetas[0]: valor for etiquetas, valor in familia.metricas}
        assert set(expuesto) == set(CLAVES), f"faltan claves: {set(CLAVES) - set(expuesto)}"
        assert expuesto["moves"] == 7, "el colector no lee el dict en el momento del scrape"
    finally:
        restaurar()


def test_sin_prometheus_client_el_modulo_carga_igual():
    """El lock de contratos de CI es mínimo (pytest, PyYAML, httpx): sin la librería no puede crujir."""
    guardado = {n: sys.modules.get(n) for n in ("prometheus_client", "prometheus_client.core")}
    sys.modules["prometheus_client"] = None            # import prometheus_client -> ImportError
    sys.modules.pop("prometheus_client.core", None)
    try:
        modulo = types.ModuleType("session_router")
        exec(compile(DEV.read_text(), "session_router.py", "exec"), modulo.__dict__)
        assert modulo._claude_stats_prometheus is None
        assert set(modulo.CLAUDE_ACCOUNT_STATS) == set(CLAVES), "el dict sigue siendo la fuente sin Prometheus"
    finally:
        for n, v in guardado.items():
            if v is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = v

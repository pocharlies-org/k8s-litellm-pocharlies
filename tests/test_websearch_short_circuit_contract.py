"""Contrato del short-circuit de WebSearch (INFRA-983, medido 2026-10-23).

El hueco que cierra este fichero: `test_anthropic_compat_contract.py` comprueba que el
callback `websearch_interception` sigue registrado y que apunta al SearXNG de siempre, pero
eso es decir que la pieza está enchufada, no qué hace. Lo que hace tiene dos caminos y el
uno no se parece al otro (medido contra el pin `v1.105.1`, misma pregunta, misma key):

  - petición con SOLO tools de búsqueda -> 200 en 0.8 s, `usage {input: 0, output: 0}` y
    un bloque `text` que es el resultado crudo (`Title: … URL: … Snippet: …`). El modelo
    no llega a ejecutarse: es `try_short_circuit_search`
    (`litellm/integrations/websearch_interception/handler.py:291`), que para un provider
    que no trata `web_search` nativamente (todos nuestros locales son `openai/`) ejecuta la
    búsqueda y devuelve una respuesta Anthropic sintética con el `usage` escrito a cero.
  - búsqueda + cualquier otra tool -> 200 en 11.4 s, `usage {input: 1575, output: 206}`,
    `thinking` + `text` con la respuesta sintetizada y sus fuentes (el loop agéntico).

No hay knob de config para apagarlo: `WebSearchInterceptionConfig`
(`litellm/types/integrations/websearch_interception.py:75`) solo expone
`enabled_providers`, `search_tool_name` y `max_agentic_loops`.

**Por qué los imports de litellm van dentro de cada test.** El job `LiteLLM contracts`
instala solo `.github/requirements/litellm-contracts.txt` (bloqueado con hashes y sin
litellm, a propósito: la suite es de manifiesto). Los dos tests de comportamiento se saltan
ahí y corren donde la librería existe — verificados el 2026-10-23 dentro del pod del proxy
con la imagen pineada, y con la aserción de `usage` invertida para comprobar que muerde.
`test_el_doc_distingue_los_dos_caminos` no necesita la librería y corre siempre: es la que
impide que la verdad del apartado se vaya del doc en un bump.
"""

import asyncio
import pathlib

import pytest

WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search", "max_uses": 2}
OTRA_TOOL = {
    "type": "function",
    "name": "leer_fichero",
    "input_schema": {"type": "object", "properties": {"ruta": {"type": "string"}}},
}
# Lo que devolvería SearXNG, en el formato que upstream arma él mismo
# (`transformation.py:545`: `Title: … \nURL: … \nSnippet: …`).
CRUDO = "Title: DGX Spark\nURL: https://nvidia.com/dgx-spark\nSnippet: 128 GB unificados"


def _handler(monkeypatch):
    pytest.importorskip("litellm.integrations.websearch_interception.handler")
    from litellm.integrations.websearch_interception.handler import WebSearchInterceptionLogger
    from litellm.types.integrations.websearch_interception import SearchSucceeded

    h = WebSearchInterceptionLogger(enabled_providers=["openai"], search_tool_name="searxng-local")

    async def _busqueda_falsa(query, kwargs=None):
        return SearchSucceeded(text=CRUDO, response=None)

    monkeypatch.setattr(h, "_short_circuit_search_outcome", _busqueda_falsa)
    return h


def _intentar(h, tools):
    return asyncio.run(
        h.try_short_circuit_search(
            model="qwen38-flash-next",
            messages=[{"role": "user", "content": "cuantos GB tiene la DGX Spark"}],
            tools=tools,
            custom_llm_provider="openai",
        )
    )


def test_solo_busqueda_corta_el_turno_sin_sintesis_y_sin_gasto(monkeypatch):
    """El camino corto, congelado tal cual se midió: sale la respuesta sintética, con el
    resultado crudo en un bloque `text` y `usage` a cero. Ese cero es el punto delicado:
    un turno por aquí no deja gasto en el spend log ni en Langfuse, y si upstream empieza
    a reportar tokens hay que re-medir y actualizar `doc/anthropic-compat.md`."""
    resp = _intentar(_handler(monkeypatch), [WEB_SEARCH])

    assert resp is not None, "con solo tools de búsqueda el short-circuit debe disparar"
    textos = [b["text"] for b in resp["content"] if b.get("type") == "text"]
    assert CRUDO in textos, f"esperaba el resultado crudo en un bloque text, salió {textos}"
    assert resp["usage"] == {"input_tokens": 0, "output_tokens": 0}, (
        "el short-circuit no ejecuta el modelo: si ahora hay tokens, el camino cambió y "
        "hay que re-medir y actualizar doc/anthropic-compat.md"
    )


def test_con_cualquier_otra_tool_no_corta_y_va_por_el_loop_agnetico(monkeypatch):
    """La condición que separa los dos caminos: BASTA con que la petición lleve una tool
    que no sea de búsqueda para que el short-circuit no dispare y el turno lo haga el
    residente (lo que mide la fila de 11.4 s del docstring). Si algún bump hace que también
    corte por aquí, Claude Code dejaría de sintetizar en los turnos normales."""
    assert _intentar(_handler(monkeypatch), [WEB_SEARCH, OTRA_TOOL]) is None


def test_el_doc_distingue_los_dos_caminos():
    """Sin librería: el doc tiene que seguir contando los dos caminos y el hueco de
    contabilidad del corto. Si alguien vuelve a dejar el apartado en un "VERDE" a secas,
    esto salta."""
    doc = (pathlib.Path(__file__).resolve().parent.parent / "doc" / "anthropic-compat.md").read_text()
    apartado = doc.split("### WebSearch", 1)[-1].split("\n## ", 1)[0]
    assert "try_short_circuit_search" in apartado, "el doc no nombra el short-circuit"
    assert "input: 0" in apartado, "el doc no dice que el camino corto no registra gasto"
    assert "1575" in apartado, "el doc no guarda la medida del camino con síntesis"

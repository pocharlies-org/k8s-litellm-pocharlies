"""Ornith esta RETIRADO. Este test guarda la retirada, no el backend.

HISTORIA (2026-08-13, ventana RHO backend-sync)
-----------------------------------------------
Este fichero afirmaba que `ornith-dgx1` era el unico backend de DGX1 y dueño de
`tooling`/`router`/`auto` mas sus dos nombres de canary. Dejo de ser cierto por
partes y en fechas distintas:

  - 10-08-2026: se BORRAN los pesos de Ornith del disco por decision del operador.
  - 08-08-2026: DeepSeek-V4-Flash TP=2 pasa a ocupar los DOS Sparks, con lo que
    el "asiento de residente de DGX1" deja de existir: mientras DeepSeek corra no
    cabe nadie en DGX1, no por politica sino por memoria.
  - 13-08-2026: se retiran de BACKENDS `ornith-dgx1` y `nvidia-qwen36-dgx1`, los
    dos candidatos a ese asiento. Ninguno tenia pesos en disco (la carpeta
    nvidia-qwen36-35b-a3b-nvfp4 tampoco existe en dgx1), asi que declararlos era
    describir un mundo que ya no esta.

El test antiguo tenia ademas un historial de asserts rancios: estuvo ROJO desde
antes del 27-07 sin que nadie lo viera, "porque CI solo corre el contrato de red".
Reescribirlo para que verifique la retirada es mas util que borrarlo: si alguien
reintroduce el backend sin reponer los pesos, esto lo cuenta.

CONSECUENCIA ABIERTA, deliberadamente NO cubierta aqui: `ornith-1.0` y
`ornith-canary` dejan de ser alias servibles. Ya fallaban antes de esta retirada
—nadie los servia—, igual que `dense`, `dense-reasoning`, `dense-uncensored` y
`taxonomy`, cuyo backend existe pero tampoco tiene checkpoint en disco. Que hacer
con esos seis nombres huerfanos es una decision de servicio, no de config.
"""
from pathlib import Path


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _bloque_backends(texto: str) -> str:
    return texto[texto.index("BACKENDS = ("):texto.index("RETIRED_MANAGED_IDS")]


def test_ornith_no_vuelve_a_backends_sin_pesos():
    texto = MANIFEST.read_text()
    backends = _bloque_backends(texto)
    for muerto in ('"name": "ornith-dgx1"', '"name": "nvidia-qwen36-dgx1"'):
        assert muerto not in backends, (
            f"{muerto} volvio a BACKENDS. Se retiro el 2026-08-13 por no tener "
            "pesos en disco: comprueba que el checkpoint existe ANTES de "
            "reintroducirlo, o el sync declarara un backend que no puede arrancar."
        )


def test_los_alias_de_ornith_ya_no_los_declara_nadie():
    """`ornith-1.0` / `ornith-canary` nombran un MODELO concreto.

    Su regla original sigue siendo la correcta: quien pide un nombre de modelo
    debe recibir ese modelo o un error visible, nunca la respuesta de otro. Con
    Ornith retirado, lo correcto es que ningun backend los declare — que fallen
    en duro — y no que se los quede DeepSeek en silencio.
    """
    backends = _bloque_backends(MANIFEST.read_text())
    for alias in ('"ornith-canary"', '"ornith-1.0"'):
        assert alias not in backends, (
            f"{alias} lo declara algun backend. Es un nombre de MODELO: si lo "
            "sirve otro checkpoint, quien lo pide recibe algo distinto de lo que "
            "pidio sin enterarse."
        )
    assert "ORNITH_ALIASES" not in backends
    assert "ORNITH_CANARY_ALIASES" not in backends


def test_los_alias_de_capacidad_siguen_teniendo_dueno():
    """La retirada no puede dejar `tooling` y compania sin backend declarado."""
    texto = MANIFEST.read_text()
    assert "TOOLING_RESIDENT_ALIASES = TOOLING_COMPAT_ALIASES" in texto
    backends = _bloque_backends(texto)
    assert backends.count('"aliases": TOOLING_RESIDENT_ALIASES') == 1, (
        "los alias de capacidad tienen que tener exactamente UN dueño declarado"
    )
    ds = backends[backends.index('"name": "deepseek-v4-flash-tp2"'):]
    assert '"aliases": TOOLING_RESIDENT_ALIASES' in ds, (
        "el dueño tiene que ser DeepSeek: es el unico backend vivo y su TP=2 "
        "excluye a cualquier otro por hardware en los dos nodos"
    )

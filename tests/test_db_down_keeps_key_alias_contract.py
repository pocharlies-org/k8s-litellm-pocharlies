"""Con la DB caida, una key conocida sigue autenticando con su alias (DGX-1099).

POR QUE EXISTE (2026-10-19)
---------------------------
`allow_requests_on_db_unavailable` deja pasar las peticiones sin DB, pero con un token
restringido (`failed-to-connect-to-db`) que no lleva el alias de la key. El filtro de
cuentas de Claude elige la cuenta por ese alias: sin el, no hay politica y todo
`claude-*` contesta «No deployments». Medido el 19-10 a las 02:52: el reinicio de
postgres-shared (INFRA-942) dejo ~50 s sin Claude a todas las maquinas.

`get_key_object` lee la cache de keys antes que la DB, y la cache dura
`user_api_key_cache_ttl` (60 s por defecto: menos que un reinicio de CNPG). Con una TTL
larga, la key usada en la ultima hora sigue resolviendo su alias sin DB, y una key
desconocida sigue cayendo al token restringido, que no tiene cuentas de Claude.
"""
import pathlib

import yaml


MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _general_settings():
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    cm = next(
        d for d in docs
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )
    return yaml.safe_load(cm["data"]["config.yaml"])["general_settings"]


def test_la_cache_de_keys_dura_mas_que_un_reinicio_de_la_db():
    general = _general_settings()
    assert general.get("allow_requests_on_db_unavailable") is True
    # 10 min como suelo: un reinicio o failover de CNPG dura del orden de 1 min.
    assert float(general.get("user_api_key_cache_ttl", 60)) >= 600

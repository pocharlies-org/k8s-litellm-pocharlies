# ARCHITECTURE.md — k8s-litellm-pocharlies

> LiteLLM: el router/pasarela de modelos de la compañía (namespace `litellm`, BD en Postgres compartido). Su **lista de
> modelos** (`k8s/manifest.yaml`, ~53 `model_name:`) es la fuente de verdad del routing; hay una constitución de contratos
> propia. No es el gateway MCP (ese es AgentGateway). Escrito por `architect` (SC-1426).

## 1. Clientes y versiones

| cliente | repositorio / ruta | versión desplegada | cómo se despliega |
|---|---|---|---|
| Proxy LiteLLM | `k8s/manifest.yaml` | `ghcr.io/berriai/litellm:v1.100.0@sha256:c8756e7b…` | ArgoCD app `litellm` (path `k8s`, `main`) |
| Redis (`redis:7-alpine`), CronJobs (watchdog, spendlogs index/retention; `python:3.12-slim`, `postgres:16-alpine`) | `k8s/litellm-watchdog-cron.yaml`, `spendlogs-*.yaml` | tags | ídem |
| `plan-gateway` (Deployment propio, 2 réplicas, mismo digest que el proxy) | `k8s/manifest.yaml` (`plan-gateway-config` → `plan_gateway.py`) | ídem imagen | ArgoCD app `litellm`, mismo path |
| Scrape de métricas | `k8s/vmservicescrape.yaml` | — | ídem |

Los clientes del router (Claude CLI del x86, Hermes, OpenClaw, document-intake, auto-reply…) usan `https://litellm…`/`litellm.litellm.svc:4000`.

**`plan-gateway`** (`plan-gateway.litellm.svc:4002`, solo dentro del cluster, sin IngressRoute) es el sitio único por el que
pasa la **media** (imagen, vídeo, voz) del Token Plan de Alibaba. Consumidores, uno por token
(`PLAN_GATEWAY_TOKEN_<NOMBRE>`): **Studio** (dgx-infra, `control-nexus`) y **omnivoice** (k8s-ai, ns `llm`); galan no lo
consume (cuelga de omnivoice). Depende de `session_router.draw_account` (selector), del panel (pesos `alibaba_account_weights`
vía `session_router`) y de los Secrets `litellm-alibaba`/`litellm-alibaba-2` (las dos keys) y `plan-gateway-tokens`. Contrato
`dgx.alibaba.plan-gateway.v1`. El chat sigue yendo por el proxy :4000, nunca por el gateway.

## 2. Dependencias, en ambos sentidos

- **Depende de** — `k8s-ai-pocharlies` (modelos vLLM locales en `llm`, pools `tooling`…), Alibaba (dos cuentas, afinidad por
  sesión), OpenRouter y otros proveedores de pago, Postgres compartido, `gpu-arbiter-state` (admisión por compute mode),
  Langfuse (callbacks), 1Password/ExternalSecrets.
- **Dependen de él** — todo el estate: sesiones de la compañía (router local de Claude), Hermes, document-intake, auto-reply,
  dashboards. **`CONTRACTS.yaml`** publica ~22 contratos (`dgx.claude.class-header.v1`, `dgx.session-router.*`,
  `dgx.model-routing.config.v1/v2/v3`, `dgx.litellm.active-requests.v1`, `dgx.hermes.profile-header.v1`, `dgx.litellm.virtual-key.*`,
  `litellm.reasoning-effort.v1`…): nunca renombrar, solo `.vN+1` con `Contract-Change:`.
- **ArgoCD** `litellm`: repo `pocharlies-org/k8s-litellm-pocharlies`, path `k8s`, tronco **`main`**.

## 3. Stack

| pieza | versión | para qué | no se usa en su lugar |
|---|---|---|---|
| LiteLLM | v1.100.0 (digest) | routing multi-proveedor | proxies a medida |
| `plan-gateway` (`plan_gateway.py`, ASGI puro sobre uvicorn + httpx de la imagen del proxy) | DGX-621 | media del Token Plan (imagen, vídeo, voz): LiteLLM no soporta ahí el vídeo asíncrono ni TTS/ASR nativos del plan Team, y `pass_through_endpoints` no reparte ni reintenta ni registra por cuenta. **Excepción razonada** a «proxies a medida»: allowlist de 4 rutas, ~300 líneas, sin estado | un proxy general, un mapa de tareas en Valkey, una fórmula de elección propia |
| Python 3.12 | CI (`.github/requirements/litellm-contracts.txt`, dependencias bloqueadas) | suite de contratos | — |
| `dev/session_router.py` | repo | enrutador de sesión/afinidad (hook del proxy) | lógica en el cliente |

## 4. Componentes compartidos

| concepto | pieza canónica | ruta | quién la usa |
|---|---|---|---|
| Lista de modelos | bloque `model_name:` | `k8s/manifest.yaml` (**citar, no copiar**) | todo el estate |
| Contratos del router | registry | `CONTRACTS.yaml` | session-router, Hermes, dashboard |
| Afinidad Alibaba por cuenta | `tests/integration/alibaba_account_affinity_router.py` | ídem | CI |
| **Selector de cuenta Alibaba** (UNA fórmula; la época de ventana = TTL del pin entra en el hash, DGX-639) | `session_router.preferred_account` + el seam `draw_account(accounts)`/`warm_config()` | `dev/session_router.py` = embed de `litellm-config` (`tests/test_session_router_dev_copy_contract.py` los iguala; manda el manifiesto) | chat sin sid (`alibaba_account_filter`) y `plan-gateway`: nadie define otra fórmula (`tests/test_plan_gateway_contract.py`, test AST) |
| Gateway de media del plan | `plan_gateway.py` | `k8s/manifest.yaml`, ConfigMap `plan-gateway-config` (inline, sin copia en `dev/`) | Studio, omnivoice |
| Compatibilidad Anthropic | `doc/anthropic-compat.md` | ídem | Claude CLI |

**BURST Alibaba (SC-2082 P6c).** `session_router._apply` lee `company.bots_alibaba` de `/api/model-routing/config` (campo aditivo
de v2, saneado aparte de `DEFAULT_COMPANY`: solo un `true` bool enciende). Con él, la key `hermes-batch` (lista fija
`BOT_BURST_KEYS`) con clase `company` y modelo residente se reescribe a `alibaba-q38-flash` por petición, sin ligadura sticky.
Sellada (`company.alibaba=false`), `-uncensored` y `alibaba.overflow=false` ganan. Interruptor sin rollout: apagar el BURST de
Alibaba en el panel (<6 s).

**Entrada propia (DGX-744 P1).** `session_router._apply` trata el `requested_model` literal del cliente (antes de resolver alias)
igual al residente como la elección de la sesión: para esa petición `default_plan` vale `local` en todo el hook (sesión nueva,
exención y retorno 4c). Por eso `default_plan=alibaba` **ya no vale de valla** para quien nombra el residente; sigue mandando sobre
`tooling` y los alias. La capacidad no cambia: residente no Ready (`rebind_alibaba`), `instant_reject` y `kv_budget` desvían igual.
Contrato `dgx.model-routing.config.v3` (v2 deprecated). La valla de tráfico para medir va al árbitro con caducidad (DGX-744 P3,
`dgx-infra`), no a esta palanca.

## 5. Cómo se construye aquí

Un modelo nuevo = entrada en `manifest.yaml` + test de contrato en `tests/test_*_contract.py` + entrada en
`CONTRACTS.yaml` si cambia superficie. Un cambio de ConfigMap **debe** rodar el pod (`test_configmap_revision_bump_contract.py`,
tres parejas configmap/deployment/anotación; `python3 tests/test_configmap_revision_bump_contract.py --fix` recalcula los hashes):
un cambio de `session_router.py` rueda también el `plan-gateway`, que lo importa.
Los modelos locales por defecto llevan `enable_thinking: false`: no cambiar sin pedirlo. Los pods Qwen pueden estar a 0/0
réplicas según el perfil `llm-tp`: un servicio sin endpoints significa «no residente», no «roto».

## 6. Tests y validaciones

```sh
pip install -r .github/requirements/litellm-contracts.txt
python -m pytest tests/ -q                               # suite completa de contratos (la del CI; unittest solo recoge una parte)
bash tests/integration/run_alibaba_account_affinity.sh   # integración de afinidad + import del plan-gateway en la imagen pineada (CI, job aparte)
```

## 7. CI/CD y despliegue

- `ci.yml` (`arc-k8s`): `reusable-ci.yml@main` + job «LiteLLM contracts» (exige runner x86_64) + job «Alibaba account affinity
  (integration)»; `duplicados.yml`, `pr-review.yml`, `release.yml` (dispatch, `reusable-manifest-release.yml@main`).
- Despliegue: merge a `main` → ArgoCD. **Validación en producción**: pedir una completion a un modelo residente por HTTPS con una
  key real (`http://` da 301 y una key mala da 401 que parece error de routing) y revisar `/active_requests`. Synced ≠ funcionando.
  Pendiente de ejecutar.

## 8. Decisiones y trampas

- README es de plantilla (k3s v1.32.5); la documentación real está en `doc/` y en los comentarios del manifiesto.
- El manifiesto cambia mucho (varias sesiones a la vez): releer justo antes de escribir.
- `doc/node-affinity-ubuntu.md`: afinidad a nodo `ubuntu` del proxy; revisar al mover control-plane a KS-5.

Última verificación contra el código: 2026-10-09 · 30d6505 (origin/main) + DGX-744 P1

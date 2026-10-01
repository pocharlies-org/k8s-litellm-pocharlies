# ARCHITECTURE.md — k8s-litellm-pocharlies

> LiteLLM: el router/pasarela de modelos de la compañía (namespace `litellm`, BD en Postgres compartido). Su **lista de
> modelos** (`k8s/manifest.yaml`, ~53 `model_name:`) es la fuente de verdad del routing; hay una constitución de contratos
> propia. No es el gateway MCP (ese es AgentGateway). Escrito por `architect` (SC-1426).

## 1. Clientes y versiones

| cliente | repositorio / ruta | versión desplegada | cómo se despliega |
|---|---|---|---|
| Proxy LiteLLM | `k8s/manifest.yaml` | `ghcr.io/berriai/litellm:v1.100.0@sha256:c8756e7b…` | ArgoCD app `litellm` (path `k8s`, `main`) |
| Redis (`redis:7-alpine`), CronJobs (watchdog, spendlogs index/retention; `python:3.12-slim`, `postgres:16-alpine`) | `k8s/litellm-watchdog-cron.yaml`, `spendlogs-*.yaml` | tags | ídem |
| Scrape de métricas | `k8s/vmservicescrape.yaml` | — | ídem |

Los clientes del router (Claude CLI del x86, Hermes, OpenClaw, document-intake, auto-reply…) usan `https://litellm…`/`litellm.litellm.svc:4000`.

## 2. Dependencias, en ambos sentidos

- **Depende de** — `k8s-ai-pocharlies` (modelos vLLM locales en `llm`, pools `tooling`…), Alibaba (dos cuentas, afinidad por
  sesión), OpenRouter y otros proveedores de pago, Postgres compartido, `gpu-arbiter-state` (admisión por compute mode),
  Langfuse (callbacks), 1Password/ExternalSecrets.
- **Dependen de él** — todo el estate: sesiones de la compañía (router local de Claude), Hermes, document-intake, auto-reply,
  dashboards. **`CONTRACTS.yaml`** publica ~22 contratos (`dgx.claude.class-header.v1`, `dgx.session-router.*`,
  `dgx.model-routing.config.v1/v2`, `dgx.litellm.active-requests.v1`, `dgx.hermes.profile-header.v1`, `dgx.litellm.virtual-key.*`,
  `litellm.reasoning-effort.v1`…): nunca renombrar, solo `.vN+1` con `Contract-Change:`.
- **ArgoCD** `litellm`: repo `pocharlies-org/k8s-litellm-pocharlies`, path `k8s`, tronco **`main`** (`origin/main` = 691ed5e).

## 3. Stack

| pieza | versión | para qué | no se usa en su lugar |
|---|---|---|---|
| LiteLLM | v1.100.0 (digest) | routing multi-proveedor | proxies a medida |
| Python 3.12 | CI (`.github/requirements/litellm-contracts.txt`, dependencias bloqueadas) | suite de contratos | — |
| `dev/session_router.py` | repo | enrutador de sesión/afinidad (hook del proxy) | lógica en el cliente |

## 4. Componentes compartidos

| concepto | pieza canónica | ruta | quién la usa |
|---|---|---|---|
| Lista de modelos | bloque `model_name:` | `k8s/manifest.yaml` (**citar, no copiar**) | todo el estate |
| Contratos del router | registry | `CONTRACTS.yaml` | session-router, Hermes, dashboard |
| Afinidad Alibaba por cuenta | `tests/integration/alibaba_account_affinity_router.py` | ídem | CI |
| Compatibilidad Anthropic | `doc/anthropic-compat.md` | ídem | Claude CLI |

## 5. Cómo se construye aquí

Un modelo nuevo = entrada en `manifest.yaml` + test de contrato en `tests/test_*_contract.py` (59 ficheros) + entrada en
`CONTRACTS.yaml` si cambia superficie. Un cambio de ConfigMap **debe** rodar el pod (`test_configmap_revision_bump_contract.py`).
Los modelos locales por defecto llevan `enable_thinking: false`: no cambiar sin pedirlo. Los pods Qwen pueden estar a 0/0
réplicas según el perfil `llm-tp`: un servicio sin endpoints significa «no residente», no «roto».

## 6. Tests y validaciones

```sh
pip install -r .github/requirements/litellm-contracts.txt
python -m unittest discover -s tests -p 'test_*.py'     # suite completa de contratos (59 ficheros)
bash tests/integration/run_alibaba_account_affinity.sh   # integración de afinidad (CI, job aparte)
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

Última verificación contra el código: 2026-10-01 · 691ed5e (origin/main)

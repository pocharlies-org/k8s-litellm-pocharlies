Rol: pm · Fecha: 2026-10-18T13:20:00+02:00 · Sesión: 18967e69-a273-4300-841a-7a0b0a46bbe3 · Estado: CUMPLE

VALIDACIÓN: CUMPLE

# 80-validacion-pm — SC-2675 · Alibaba en el panel: velocidad tok/s en vivo, por petición y caché

## Qué se pidió y contra qué se mide

Dani, 18-10, en vivo y con captura de la tarjeta «Alibaba Model Studio»: «no veo a la velocidad que
va alibaba de tokens segundo. lo puedes poner? y por request? y la cache?». Tres peticiones, y esas
tres son los criterios: (1) velocidad en t/s de Alibaba en vivo, (2) por cada request, (3) la caché.

El `00-spec.md` del ticket es el borrador del supervisor, sin criterios numerados, y así queda: no
replanteo los criterios para que encajen con la entrega (no es mi decisión y no hace falta, las
palabras de Dani son comprobables). Las filas de abajo son los criterios sacados de la descripción
de la issue, cada uno con su comando y su salida real, y quién la midió.

Entrega validada: productor k8s-litellm-pocharlies#262 (OPEN, head b182ff8) y consumidor
dgx-infra#1323 (OPEN). Ninguno fusionado todavía: este veredicto es el que pide la puerta de #262.

## Criterio 1 — Velocidad en t/s de Alibaba en vivo (cabecera de la tarjeta)

**CUMPLE.** La tarjeta ya promediaba `output_speed_tps` de sus filas y sacaba `--` cuando no había
ninguna (DGX-973, en el trunk). Lo que faltaba era el dato, no el dibujo: DashScope manda `usage`
solo en el último chunk (1 de 10586, medido 18-10), así que en vuelo no había velocidad que promediar.
El productor la fabrica: el primer chunk con contenido sella `ttft_ms` y las muestras de caracteres
alimentan la misma ventana de 2 s.

Comprobado por mí, en el worktree del head exacto del PR (mi cwd), ejecutando la suite de contrato
del productor:

    python3 -m pytest tests/test_active_request_metrics_contract.py -q -k "estimates_live_output"
    .                                                                        [100%]
    1 passed, 10 deselected in 0.35s

El test (líneas 274-319 del fichero) arranca una petición `alibaba-q38-flash`, mete chunks sin `usage`
y exige en el fichero vivo del tracker: `ttft_ms == 500`, `completion_tokens is None`,
`completion_tokens_estimate == 4`, `output_speed_tps == 2.0`. Es decir: velocidad en vuelo sin usage,
que es exactamente el agujero de la captura de Dani.

Lectura del trunk que sostiene la otra mitad (que la media existe y se pinta):

    git show origin/master:services/dashboard/static/inferencia/src/sections/servidores-modelo.js  (tarjetaAlibaba)
    const speeds = requests.filter(... r.output_speed_tps ...);
    velocidad: avgSpeed == null ? '--' : fmtNum(avgSpeed) + ' t/s'

## Criterio 2 — Velocidad por request

**CUMPLE.** La fila en vuelo de Alibaba sale de «en vuelo» y entra en `decode` con su t/s porque
`requestPhase` decide con `ttft_ms != null OR completion_tokens > 0` (trunk, actividad-puro.js
línea ~348) y pinta la velocidad de ESA petición. El panel añade la salida estimada `~N out` junto a
ella y la marca como estimación con su tooltip.

Evidencia: test nuevo del consumidor, ejecutado por CI sobre el head del PR (no por mí: la rama no
está desplegada ni la tengo montada; la midió qa en el worktree y la corrió el job `dashboard-tests`):

    gh run view 39116553322 --job 117643417385 --log
    ok 406 - SC-2675: un stream sin usage (Alibaba) pinta decode con su velocidad y la salida estimada
    # tests 993 · # pass 993 · # fail 0

El test renderiza el HTML real de una fila con `ttft_ms: 900, output_speed_tps: 41.5,
completion_tokens_estimate: 1240` y exige `request-phase-decode`, `41.5 t/s` y `~1k out` marcado como
estimación; y con el `usage` real ya llegado, el estimado desaparece y manda el real. Salvoconducto
del contrato: `python3 -m pytest tests/test_litellm_active.py` (qa, worktree) → 23 passed.

## Criterio 3 — La caché

**CUMPLE, con la limitación del proveedor declarada.** En las peticiones terminadas el porcentaje de
entrada servida desde la caché de prefijo ya se pinta (DGX-951, en el trunk: `promptCache` pedido por
`prompt_tokens` y `cached_tokens`, null si falta → null no es un 0 %). En vuelo sale `null` honesto, y
**eso es limitación del proveedor, no alcance omitido**: `cached_tokens` viaja dentro del `usage`
(`usage.prompt_tokens_details.cached_tokens`) y DashScope solo emite `usage` en el último chunk (1 de
10586 medido). Sin `usage` en vuelo no hay dato de caché que estimar: lo que sí se puede estimar por
caracteres del stream es la salida, y eso es lo que se estimó y se pintó. Inventar una caché en vuelo
sería pintar un número falso, que es lo contrario de lo que pidió Dani.

Evidencia: `activity-shared.jsx` del trunk (función `promptCache` y su llamada en la fila compacta) no
se toca en este PR; la cobertura de terminadas está en los 993 tests de CI (qa cita
`inferencia-request-phase.test.js:270-281`: `cached_tokens: 45120` pinta %, `0` y `null` no inventan).

## Alcance: ni ampliado ni estrechado

Las tres peticiones están cubiertas y no hay nada más. Lo que sobra en el diff es medio, no alcance:
sellar `ttft_ms` en el primer chunk con contenido y extraer el muestreo a `_sample_locked` son el
mecanismo por el que la velocidad existe, no funciones nuevas para nadie. El campo es **aditivo**
(`completion_tokens_estimate`, int|null): nada se renombra ni se borra en `dgx.litellm.active-requests.v1`
ni en `dgx.activity.stream.v1`, así que los consumidores que no lo leen (Swift, v2) no cambian de
comportamiento. Sin el tracker nuevo, el panel pinta exactamente igual que hoy (null-seguro, testeado).

## Documentación

La superficie declarada está actualizada en los dos repos: nota aditiva en `CONTRACTS.yaml` de
k8s-litellm-pocharlies (contrato del tracker) y en el de dgx-infra (contrato del stream del panel),
cada una citando la otra. `ARCHITECTURE.md` de dgx-infra no necesita línea nueva: su lista de campos
es lo que consume Echo Show, y ninguno de ellos es el campo nuevo. El resto es un no explícito: **no
hay ningún otro documento que actualizar**; la explicación para quien mira el panel vive en los
tooltips de la estimación y de la caché.

## Notas (no bloquean el veredicto)

- La caja desplegada pinta la celda `OUT ~` solo con `completion_tokens_estimate > 0`, sin el guarda
  de «si ya hay real, calla» que sí tiene `IN ~`. En el tramo final de una petición Alibaba (el usage
  real ya llegó y la fila aún está activa) pueden verse `OUT` real y `OUT ~` a la vez. Transitorio y
  cosmético; no lo reproduje en producción.
- El changelog de dgx-infra#1323 enlaza el productor como #257, que está cerrado sin fusionar; el
  productor vivo es #262. Enlace obsoleto en la descripción, no en el código.
- La verificación end-to-end en producción (una fila Alibaba real pintando `~N out` y t/s en la
  tarjeta) **no está hecha y no puede estarlo**: ni #262 ni #1323 están fusionados ni desplegados. Es
  lo que queda después del merge, y es tarea del watcher, no de este veredicto.

## Puerta

CI en verde sobre el head de los dos PRs: #262 (LiteLLM contracts 1001 passed, Contract surface,
Alibaba account affinity, manifests, duplicados, juez; `aprobacion/architect` APROBADO) y #1323
(dashboard-tests, manifests, alibaba-key-guard, duplicados, juez; `aprobacion/qa` APROBADO por su
70-qa.md PASA de hoy). Ambos PRs siguen abiertos: fusiona cada uno su dueño; el cierre de la historia
lo hace el maker al fusionar, no yo.

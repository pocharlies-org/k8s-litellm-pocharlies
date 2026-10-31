Antes: la key `synapse` iba sellada con `disable_fallbacks` para todo modelo; `qwen38-flash-next` con el residente caido daba 500 `Connection error`.
Despues: `synapse` pidiendo `qwen38-flash-next` directo no se sella y el Router cae a `alibaba-q38-flash`; con `tooling`, `-uncensored` o cualquier otro modelo sigue sellada; `aurora-rca`, `brain` y `claude-cloudblue` sin cambios.
Quien se mueve: `it`, tras desplegar, pone el override `model` del adapter gmail de Synapse en `qwen38-flash-next` (hoy `alibaba-q38-flash`).
Decision: Dani, 12-10-2026 («que usara el modelo local y de fallback el modelo Alibaba»), recogida en el spec de DGX-841; sustituye para ese camino la decision del 20-09.

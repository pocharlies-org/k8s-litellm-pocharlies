"""Grupos `alibaba-*` del plan Team, compartida por los dos contratos de reparto
(afinidad de cuenta y espejo del -k2): los dos miden el mismo conjunto de alias y
mantenerla en un solo sitio es lo que evita que uno se quede sin el alias nuevo."""

# 22-10-2026: sin `alibaba-dsv41-fl`: el flotante se retiro y es alias de
# `alibaba-ds41f1017` (router_settings.model_group_alias), no un grupo con deployments.
GROUPS = [
    "alibaba-q38-max", "alibaba-q38-flash", "alibaba-q37-plus", "alibaba-q37-max",
    "alibaba-q36-flash", "alibaba-ds41f1017", "alibaba-dsv4-pro",
    "alibaba-dsv4f0731", "alibaba-glm-53", "alibaba-glm-52",
]

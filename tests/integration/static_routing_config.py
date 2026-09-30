"""Config de routing ESTATICA para el test de integracion de afinidad.

El runner ARC esta DENTRO del cluster: sin este servidor, el contenedor del
test resuelve dgx-dashboard-backend.control-nexus... y lee los pesos VIVOS de
alibaba_account_weights, que el dashboard recalcula con el cupo consumido y
cambian cada pocos segundos. La determinancia entre pods que este test mide
exige la MISMA instantanea en los dos procesos: con pesos al aire, cada pod
cachea SWR la suya y sesiones nuevas caen en cuentas distintas segun el
instante de refresco (rojo en CI el 29-09, run 36583219046). Ademas un test de
CI no debe hablar con el panel de produccion.

Pesos IGUALES (1.0/1.0): ejercita la rama ponderada de preferred_account de
punta a punta (sanitize -> _account_weights -> tramos) con el mismo reparto
50/50 que veia el test antes del 29-09. Con pesos sesgados el test se vuelve
ruleta otra vez, pero por otra via: la cuenta con mas sesiones concentra trafico,
su deployment entra en cooldown algun instante y, si ademas el SET del pin muere
contra el presupuesto de 100 ms de Valkey, la sesion hace un blip de un turno a
la otra cuenta y vuelve — correcto en produccion (fail-open), letal para el
assert estricto. El reparto sesgado lo cubren los unitarios
(test_sesiones_nuevas_siguen_el_peso_de_cada_cuenta y cia.).
"""
import http.server
import json

CONFIG = {"alibaba_account_weights": {"k1": 1.0, "k2": 1.0}}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(CONFIG).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("0.0.0.0", 9002), Handler).serve_forever()

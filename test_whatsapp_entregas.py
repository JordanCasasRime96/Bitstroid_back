from datetime import date, datetime, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock

import whatsapp_entregas as wa


class EntregasTests(unittest.TestCase):
    def test_fecha_respeta_corte_y_no_acepta_el_mismo_sabado(self):
        for valor, esperado in [
            ("2026-10-09T16:59:00-05:00", date(2026, 10, 10)),
            ("2026-10-09T17:00:00-05:00", date(2026, 10, 10)),
            ("2026-10-09T17:01:00-05:00", date(2026, 10, 17)),
            ("2026-10-10T08:00:00-05:00", date(2026, 10, 17)),
            ("2026-10-11T12:00:00-05:00", date(2026, 10, 17)),
            ("2026-10-12T12:00:00-05:00", date(2026, 10, 17)),
        ]:
            with self.subTest(valor=valor):
                self.assertEqual(wa.fecha_contraentrega(datetime.fromisoformat(valor)), esperado)

    def test_horario_utc_se_convierte_a_lima(self):
        self.assertEqual(wa.fecha_contraentrega(datetime(2026, 10, 9, 21, 59, tzinfo=timezone.utc)), date(2026, 10, 10))
        with self.assertRaises(ValueError):
            wa.fecha_contraentrega(datetime(2026, 10, 9, 16))

    def test_olva_y_shalom_pago_distinto_sin_confirmar_datos_previos(self):
        for modalidad, pago in [("olva", "anticipado"), ("shalom", "destino")]:
            cur = MagicMock()
            cur.fetchall.return_value = [{"id": "direccion", "nombre_completo": "Cliente"}]
            cur.fetchone.return_value = {"id": "solicitud"}
            resultado = wa.crear_solicitud(cur, "contacto", modalidad, "phone", "wamid")
            self.assertEqual(resultado["pagoEnvio"], pago)
            self.assertEqual(resultado["estado"], "pendiente_datos")
            self.assertEqual(len(resultado["destinatarios"]), 1)
            self.assertNotIn("destinatario_id", cur.execute.call_args.args[0])

    def test_envio_sin_datos_devuelve_lista_vacia(self):
        cur = MagicMock()
        cur.fetchall.return_value = []
        resultado = wa.crear_solicitud(cur, "contacto", "olva", "phone", "wamid")
        self.assertEqual(resultado["destinatarios"], [])
        self.assertEqual(resultado["estado"], "pendiente_datos")

    def test_reintento_no_crea_otra_solicitud(self):
        cur = MagicMock()
        cur.fetchone.return_value = None
        resultado = wa.crear_solicitud(cur, "contacto", "contraentrega", "phone", "wamid")
        self.assertFalse(resultado["creada"])
        self.assertIn("ON CONFLICT (phone_number_id, mensaje_id) DO NOTHING", cur.execute.call_args.args[0])

    def test_modalidad_ajena_se_rechaza(self):
        with self.assertRaises(ValueError):
            wa.crear_solicitud(MagicMock(), "contacto", "otro", "phone", "wamid")

    def test_flow_navegacion_y_acciones_consistentes(self):
        doc = json.loads((Path(__file__).parent / "flows/menu_cliente.json").read_text(encoding="utf-8"))
        screens = {screen["id"]: screen for screen in doc["screens"]}
        self.assertEqual(doc["version"], "7.3")
        destinos = set()
        acciones = {}
        for id_, screen in screens.items():
            children = screen["layout"]["children"]
            for component in children:
                if component["type"] == "NavigationList":
                    self.assertEqual(len(children), 1)
                    self.assertFalse(screen.get("terminal"))
                    for item in component["list-items"]:
                        action = item["on-click-action"]
                        self.assertEqual(action["name"], "navigate")
                        destinos.add(action["next"]["name"])
                if component["type"] == "Footer":
                    self.assertTrue(screen["terminal"])
                    self.assertEqual(component["on-click-action"]["name"], "complete")
                    acciones[id_] = component["on-click-action"]["payload"]
        self.assertTrue(destinos.issubset(screens))
        self.assertEqual(acciones["OLVA"]["modalidad"], "olva")
        self.assertEqual(acciones["SHALOM"]["modalidad"], "shalom")
        self.assertNotIn("atencion_personal", json.dumps(doc))


if __name__ == "__main__":
    unittest.main()

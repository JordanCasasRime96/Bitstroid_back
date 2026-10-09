import json
from io import BytesIO
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import whatsapp_respuestas as wa


class RespuestasTests(unittest.TestCase):
    def test_preparacion_actualiza_tabla_existente_sin_borrar_filas(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        with patch.object(wa, "connect") as conectar:
            conectar.return_value.__enter__.return_value = conn
            wa.preparar_tabla.__wrapped__(())
        consultas = [call.args[0] for call in cur.execute.call_args_list]
        self.assertTrue(any("ADD COLUMN IF NOT EXISTS recibido_en" in sql for sql in consultas))
        self.assertTrue(any("ALTER COLUMN recibido_en SET DEFAULT now()" in sql for sql in consultas))
        self.assertFalse(any("DROP " in sql or "DELETE " in sql for sql in consultas))

    def test_diagnostico_sql_no_expone_datos_del_error(self):
        error = Exception({"C": "42703", "M": "datos privados del servidor", "D": "token privado"})
        resultado = wa.diagnostico_db(error)
        self.assertIn("SQLSTATE=42703", resultado)
        self.assertIn("Falta una columna", resultado)
        self.assertNotIn("privado", resultado)
        self.assertNotIn("servidor", resultado)

    def test_diagnostico_sin_sqlstate_no_imprime_mensaje(self):
        self.assertEqual(wa.diagnostico_db(ValueError("contrasena privada")), "ValueError")

    def test_envio_usa_texto_sin_plantilla(self):
        with patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": "privado", "WHATSAPP_API_VERSION": "v21.0"}), patch.object(wa, "urlopen") as abrir:
            abrir.return_value.__enter__.return_value = BytesIO(b'{"messages":[{"id":"wamid.test"}]}')
            wa.enviar("51943875311", "validado", "1402483696282363")
        request = abrir.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["type"], "text")
        self.assertNotIn("template", body)
        self.assertIn("terminar de crear", body["text"]["body"])

    def dispatcher(self, row):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = row
        p = patch.object(wa, "connect")
        p.start().return_value.__enter__.return_value = conn
        self.addCleanup(p.stop)
        p = patch.object(wa, "preparar_tabla")
        p.start()
        self.addCleanup(p.stop)
        return cur

    def test_fallo_de_envio_no_marca_enviado_ni_propagacion(self):
        cur = self.dispatcher({"id": 1, "numero": "51943875311", "tipo": "validado", "phone_number_id": "123"})
        error = HTTPError("https://graph.facebook.com", 401, "Unauthorized", {}, BytesIO(b'{"error":{"code":190,"message":"secreto"}}'))
        with patch.object(wa, "enviar", side_effect=error), self.assertLogs(wa.logger, level="WARNING") as logs:
            wa.despachar({})
        self.assertNotIn("secreto", " ".join(logs.output))
        self.assertIn("Meta 190", " ".join(logs.output))
        self.assertEqual(cur.execute.call_count, 1)
        self.assertIn("FOR UPDATE SKIP LOCKED", cur.execute.call_args.args[0])

    def test_envio_confirmado_marca_fila(self):
        cur = self.dispatcher({"id": 1, "numero": "51943875311", "tipo": "creado", "phone_number_id": "123"})
        with patch.object(wa, "enviar") as enviar:
            wa.despachar({})
        enviar.assert_called_once()
        self.assertIn("enviado_en = now()", cur.execute.call_args.args[0])

    def test_cola_vacia_no_envia(self):
        self.dispatcher(None)
        with patch.object(wa, "enviar") as enviar:
            wa.despachar({})
        enviar.assert_not_called()

    def test_encolar_idempotente_y_limitado(self):
        cur = MagicMock()
        wa.encolar(cur, "wamid.test", "123", "51943875311", "invalido")
        self.assertIn("ON CONFLICT (phone_number_id, evento) DO NOTHING", cur.execute.call_args.args[0])
        self.assertIn("< 5", cur.execute.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

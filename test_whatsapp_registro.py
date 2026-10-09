import json
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

import whatsapp_registro as otp


class RegistroTests(unittest.TestCase):
    def setUp(self):
        self.config = ["token", "123", "v99.0", "registro", "es", "x" * 48]
        self.config_patch = patch.object(otp, "configuracion", return_value=self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.conn = MagicMock()
        self.cur = self.conn.cursor.return_value.__enter__.return_value
        self.connect_patch = patch.object(otp, "connect")
        self.connect_patch.start().return_value.__enter__.return_value = self.conn
        self.addCleanup(self.connect_patch.stop)
        self.tabla_patch = patch.object(otp, "preparar_tabla")
        self.tabla_patch.start()
        self.addCleanup(self.tabla_patch.stop)
        self.guard = MagicMock()
        self.id = "00000000-0000-0000-0000-000000000001"

    def test_template_envia_codigo_en_cuerpo_y_boton(self):
        with patch.object(otp, "urlopen") as enviar:
            enviar.return_value.__enter__.return_value.read.return_value = b'{"messages":[{"id":"mensaje"}]}'
            otp.enviar_codigo("51999999999", "123456", self.config)
            request = enviar.call_args.args[0]
            data = json.loads(request.data)
            self.assertEqual(data["template"]["components"][0]["parameters"][0]["text"], "123456")
            self.assertEqual(data["template"]["components"][1]["parameters"][0]["text"], "123456")
            self.assertEqual(request.full_url, "https://graph.facebook.com/v99.0/123/messages")

    def test_solicitud_no_crea_usuario_y_no_expone_codigo(self):
        self.cur.fetchone.side_effect = [{"usuario": False, "numero": False, "correo": False}, None]
        with patch.object(otp, "enviar_codigo"), patch.object(otp.secrets, "randbelow", return_value=123456):
            resultado = otp.solicitar_codigo({}, {"apodo": "player", "whatsapp": "51999999999", "password": "clave", "nombres": "", "correo": ""}, self.guard, "ip")
        self.assertNotIn("codigo", resultado)
        self.assertEqual(resultado["venceEn"], 300)
        sentencias = [call.args[0] for call in self.cur.execute.call_args_list]
        self.assertFalse(any("INSERT INTO seguridad.usuario" in sql for sql in sentencias))
        insercion = next(call for call in self.cur.execute.call_args_list if "INSERT INTO cliente.registro_whatsapp" in call.args[0])
        datos = json.loads(insercion.args[1][2])
        self.assertNotIn("password", datos)
        self.assertTrue(datos["hash_contrasena"].startswith("$2"))
        self.assertNotEqual(insercion.args[1][3], "123456")

    def test_fallo_de_codigo_se_persiste_antes_de_responder(self):
        self.cur.fetchone.side_effect = [{"whatsapp": "51999999999"}, {"enviado": True, "vencido": False,
            "intentos": 4, "codigo_hash": otp.hash_codigo(self.id, "123456", self.config[-1])}]
        with self.assertRaises(HTTPException):
            otp.verificar_registro({}, self.id, "000000", self.guard, "ip")
        sentencias = [call.args[0] for call in self.cur.execute.call_args_list]
        self.assertTrue(any("intentos = intentos + 1" in sql for sql in sentencias))
        self.assertFalse(any("INSERT INTO seguridad.usuario" in sql for sql in sentencias))
        self.assertEqual(self.conn.transaction.return_value.__exit__.call_args.args[0], None)

    def test_codigo_vencido_no_crea_usuario(self):
        self.cur.fetchone.side_effect = [{"whatsapp": "51999999999"}, {"enviado": True, "vencido": True, "intentos": 0}]
        with self.assertRaises(HTTPException):
            otp.verificar_registro({}, self.id, "123456", self.guard, "ip")
        self.assertFalse(any("INSERT INTO seguridad.usuario" in call.args[0] for call in self.cur.execute.call_args_list))

    def test_codigo_consumido_no_se_reutiliza(self):
        self.cur.fetchone.return_value = None
        with self.assertRaises(HTTPException):
            otp.verificar_registro({}, self.id, "123456", self.guard, "ip")

    def test_codigo_correcto_crea_cliente_verificado_y_se_elimina(self):
        datos = {"apodo": "player", "whatsapp": "51999999999", "correo": "interno@bitstroid.local", "nombres": "", "hash_contrasena": "hash"}
        self.cur.fetchone.side_effect = [{"whatsapp": datos["whatsapp"]}, {"enviado": True, "vencido": False,
            "intentos": 0, "codigo_hash": otp.hash_codigo(self.id, "123456", self.config[-1]), "datos": datos},
            {"usuario": False, "numero": False, "correo": False}, {"id": "cliente"}]
        self.assertEqual(otp.verificar_registro({}, self.id, "123456", self.guard, "ip"), {"id": "cliente"})
        sql = " ".join(call.args[0] for call in self.cur.execute.call_args_list)
        self.assertIn("false, false, true, 'activo'", sql)
        self.assertIn("INSERT INTO cliente.cliente", sql)
        self.assertIn("DELETE FROM cliente.registro_whatsapp WHERE id", sql)

    def test_codigo_y_solicitud_estan_vinculados(self):
        self.assertNotEqual(otp.hash_codigo("a", "123456", "secret"), otp.hash_codigo("b", "123456", "secret"))


if __name__ == "__main__":
    unittest.main()

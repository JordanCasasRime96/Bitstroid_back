import json
import hmac
from hashlib import sha256
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse, parse_qs

from fastapi import HTTPException
import whatsapp_entrada as wa


class EntradaTests(unittest.TestCase):
    def setUp(self):
        self.secret = "s" * 48
        self.config = (self.secret, "app-secret", "v" * 48, "1402483696282363", "51926839501")
        p = patch.object(wa, "configuracion", return_value=self.config)
        p.start()
        self.addCleanup(p.stop)
        self.conn = MagicMock()
        self.cur = self.conn.cursor.return_value.__enter__.return_value
        p = patch.object(wa, "connect")
        p.start().return_value.__enter__.return_value = self.conn
        self.addCleanup(p.stop)
        p = patch.object(wa, "preparar_tabla")
        p.start()
        self.addCleanup(p.stop)
        self.guard = MagicMock()
        self.id = "00000000-0000-0000-0000-000000000001"
        self.token = "t" * 43
        self.palabra = "ABCD2345"
        self.datos = {"apodo": "player", "whatsapp": "51943875311", "nombres": "", "correo": "", "password": "clave"}

    def payload(self, sender="51943875311", phone_id="1402483696282363", timestamp="1700000000"):
        return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp", "metadata": {"phone_number_id": phone_id}, "messages": [{
                "from": sender, "id": "wamid.test", "timestamp": timestamp, "type": "text",
                "text": {"body": f"Hola Bitstroid\nREGISTRO: {self.palabra}"}}]}}]}]}

    def solicitud(self, confirmado=True, vencido=False):
        return {"whatsapp": self.datos["whatsapp"], "token_hash": wa.hash_codigo(self.id, self.token, self.secret),
            "confirmado": confirmado, "vencido": vencido, "datos": {**self.datos,
            "correo": "interno@bitstroid.local", "hash_contrasena": "hash"}}

    def test_firma_valida_y_manipulada(self):
        body = b'{"entry":[]}'
        signature = "sha256=" + hmac.new(b"app-secret", body, sha256).hexdigest()
        wa.verificar_firma(body, signature, "app-secret")
        for signature_bad in [None, "", "sha256=" + "0" * 64, "sha256=\u00f1"]:
            with self.assertRaises(HTTPException) as error:
                wa.verificar_firma(body, signature_bad, "app-secret")
            self.assertEqual(error.exception.status_code, 401)
        with self.assertRaises(HTTPException):
            wa.verificar_firma(body + b" ", signature, "app-secret")

    def test_handshake_solo_token_correcto(self):
        self.assertEqual(wa.verificar_webhook("subscribe", self.config[2], "123"), "123")
        for mode, token in [("other", self.config[2]), ("subscribe", "incorrecto"), ("subscribe", "\u00f1")]:
            with self.assertRaises(HTTPException):
                wa.verificar_webhook(mode, token, "123")

    def test_solicitud_no_envia_otp_ni_crea_usuario(self):
        self.cur.fetchone.side_effect = [{"usuario": False, "numero": False, "correo": False}, None]
        resultado = wa.solicitar_validacion({}, self.datos, self.guard, "ip")
        url = urlparse(resultado["whatsappUrl"])
        self.assertEqual(url.netloc, "wa.me")
        self.assertEqual(url.path, "/51926839501")
        text = parse_qs(url.query)["text"][0]
        self.assertNotIn(resultado["verificacionId"], text)
        codigo = text.split("REGISTRO: ")[1]
        self.assertRegex(codigo, r"^[A-HJ-NP-Z2-9]{8}$")
        self.assertNotIn(resultado["verificacionToken"], text)
        self.assertEqual(resultado["venceEn"], 300)
        self.assertFalse(any("INSERT INTO seguridad.usuario" in c.args[0] for c in self.cur.execute.call_args_list))
        insert = next(c for c in self.cur.execute.call_args_list if "INSERT INTO cliente.registro_whatsapp_entrada" in c.args[0])
        datos = json.loads(insert.args[1][2])
        self.assertNotIn("password", datos)
        self.assertTrue(datos["hash_contrasena"].startswith("$2"))
        self.assertEqual(insert.args[1][3], wa.hash_codigo(resultado["verificacionId"], resultado["verificacionToken"], self.secret))
        self.assertEqual(insert.args[1][4], wa.hash_codigo(self.datos["whatsapp"], codigo, self.secret))

    def test_limite_de_reenvio(self):
        self.cur.fetchone.side_effect = [{"usuario": False, "numero": False, "correo": False}, {"exists": True}]
        with self.assertRaises(HTTPException) as error:
            wa.solicitar_validacion({}, self.datos, self.guard, "ip")
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(error.exception.headers["Retry-After"], "60")

    def test_duplicado_rechazado(self):
        self.cur.fetchone.return_value = {"usuario": False, "numero": True, "correo": False}
        with self.assertRaises(HTTPException) as error:
            wa.solicitar_validacion({}, self.datos, self.guard, "ip")
        self.assertEqual(error.exception.status_code, 409)

    def test_mensaje_extraido_y_destino_distinto_ignorado(self):
        self.assertEqual(list(wa.mensajes_registro(self.payload(), self.config[3])),
            [(self.palabra, self.datos["whatsapp"], "wamid.test", 1700000000)])
        self.assertEqual(list(wa.mensajes_registro(self.payload(phone_id="otro"), self.config[3])), [])

    def test_payloads_no_validos_no_provocan_error(self):
        for payload in [None, [], {}, {"object": "whatsapp_business_account", "entry": None},
                {"object": "whatsapp_business_account", "entry": [None, {}, {"changes": [None]}]},
                self.payload(timestamp="nan"), self.payload(timestamp="999999999999999999999")]:
            self.assertEqual(list(wa.mensajes_registro(payload, self.config[3])), [])

    def test_update_condiciona_remitente_secreto_caducidad_e_idempotencia(self):
        wa.procesar_mensajes({}, self.payload(sender="51999999999"))
        sql, params = self.cur.execute.call_args.args
        self.assertIn("whatsapp = %s AND palabra_hash = %s AND confirmado = false", sql)
        self.assertIn("vence_en > now()", sql)
        self.assertIn("date_trunc('second', creado_en)", sql)
        self.assertIn("to_timestamp(%s) < vence_en", sql)
        self.assertEqual(params[1], "51999999999")
        self.assertEqual(params[2], wa.hash_codigo("51999999999", self.palabra, self.secret))

    def test_codigo_vinculado_al_numero_remitente(self):
        self.assertNotEqual(wa.hash_codigo("51943875311", self.palabra, self.secret),
            wa.hash_codigo("51999999999", self.palabra, self.secret))

    def test_formato_antiguo_o_codigo_corto_no_valida(self):
        for text in [f"REGISTRO: {self.id} {self.palabra}", "REGISTRO: ABCD234", "REGISTRO: ABCD23456"]:
            payload = self.payload()
            payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = text
            self.assertEqual(list(wa.mensajes_registro(payload, self.config[3])), [])

    def test_mensaje_ajeno_no_modifica_bd(self):
        wa.procesar_mensajes({}, self.payload(phone_id="otro"))
        self.cur.execute.assert_not_called()

    def test_estado_requiere_token_privado(self):
        self.cur.fetchone.return_value = self.solicitud()
        self.assertEqual(wa.estado_validacion({}, self.id, self.token, self.guard, "ip"), {"confirmado": True})
        with self.assertRaises(HTTPException):
            wa.estado_validacion({}, self.id, self.palabra, self.guard, "ip")

    def test_pendiente_no_crea_cliente(self):
        self.cur.fetchone.return_value = self.solicitud(confirmado=False)
        with self.assertRaises(HTTPException) as error:
            wa.crear_registro({}, self.id, self.token, self.guard, "ip")
        self.assertEqual(error.exception.status_code, 409)
        self.assertFalse(any("INSERT INTO seguridad.usuario" in c.args[0] for c in self.cur.execute.call_args_list))

    def test_vencido_consumido_o_token_incorrecto_no_crea_cliente(self):
        for solicitud in [self.solicitud(vencido=True), None, {**self.solicitud(), "token_hash": "otro"}]:
            self.cur.fetchone.return_value = solicitud
            with self.assertRaises(HTTPException):
                wa.crear_registro({}, self.id, self.token, self.guard, "ip")
        self.assertFalse(any("INSERT INTO seguridad.usuario" in c.args[0] for c in self.cur.execute.call_args_list))

    def test_confirmado_crea_cliente_y_consume(self):
        self.cur.fetchone.side_effect = [self.solicitud(), self.solicitud(),
            {"usuario": False, "numero": False, "correo": False}, {"id": "cliente"}]
        self.assertEqual(wa.crear_registro({}, self.id, self.token, self.guard, "ip"), {"id": "cliente"})
        sql = " ".join(c.args[0] for c in self.cur.execute.call_args_list)
        self.assertIn("FOR UPDATE", sql)
        self.assertIn("false, false, true, 'activo'", sql)
        self.assertIn("INSERT INTO cliente.cliente", sql)
        self.assertIn("DELETE FROM cliente.registro_whatsapp_entrada WHERE id", sql)


if __name__ == "__main__":
    unittest.main()

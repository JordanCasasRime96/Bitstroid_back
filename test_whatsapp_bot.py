import ast
from datetime import datetime, timezone
from os import getenv
from pathlib import Path
import time
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from db_compat import connect, dict_row
import whatsapp_bot as bot


class BotTests(unittest.TestCase):
    def test_menu_es_lista_y_seleccion_ligada_a_token(self):
        payload = bot.menu("sesion")
        self.assertEqual(payload["interactive"]["type"], "list")
        rows = payload["interactive"]["action"]["sections"][0]["rows"]
        self.assertEqual([r["title"] for r in rows], ["Mis compras", "Solicitar entrega"])
        self.assertTrue(all(len(r["title"]) <= 24 for r in rows))
        message = {"interactive": {"type": "list_reply", "list_reply": {"id": rows[0]["id"]}}}
        self.assertEqual(bot.seleccion(message, "sesion"), "compras")
        self.assertIsNone(bot.seleccion(message, "otra"))
        self.assertIsNone(bot.seleccion({"text": {"body": "compras"}}, "sesion"))

    def test_registro_excluido(self):
        for value in ("REGISTRO: ABCD2345", "Hola\nREGISTRO: ABCD2345", "registro: ABCD2345"):
            self.assertTrue(bot.es_registro({"text": {"body": value}}))
        self.assertFalse(bot.es_registro({"text": {"body": "Hola"}}))

    def test_campos_y_resumen_por_transportista(self):
        d = {key: "dato" for key, _ in bot.FIELDS}
        shalom = [key for key, _ in bot.campos_envio("shalom")]
        olva = [key for key, _ in bot.campos_envio("olva")]
        self.assertNotIn("direccion", shalom)
        self.assertNotIn("referencia", shalom)
        self.assertIn("agencia_shalom", shalom)
        self.assertIn("direccion", olva)
        self.assertIn("referencia", olva)
        self.assertNotIn("agencia_shalom", olva)
        self.assertIn("Direccion de la agencia Shalom", bot.resumen(d, "shalom"))
        self.assertNotIn("Referencia", bot.resumen(d, "shalom"))
        self.assertNotIn("Shalom", bot.resumen(d, "olva"))
        d["direccion"] = ""
        self.assertTrue(bot.datos_completos(d, "shalom"))
        self.assertFalse(bot.datos_completos(d, "olva"))

    def test_datos_previos_dos_opciones_y_solo_direccion_adecuada(self):
        destino = {key: "dato" for key, _ in bot.FIELDS}
        destino["id"] = "destino"
        for modalidad in ("olva", "shalom"):
            s = {"token": "abc", "paso": "confirmar_entrega", "datos": {"modalidad": modalidad},
                 "contacto_id": "contacto", "phone_number_id": "123"}
            with patch.object(bot, "crear_solicitud", return_value={"solicitudId": "solicitud", "destinatarios": [destino]}), patch.object(bot, "encolar") as queue:
                bot.atender(MagicMock(), s, "solicitar", {"id": "mensaje"}, datetime.now(timezone.utc))
                payload = queue.call_args.args[2]["interactive"]
                self.assertEqual(len(payload["action"]["buttons"]), 2)
                self.assertEqual(s["paso"], "datos_previos")
                self.assertEqual("Direccion de la agencia Shalom" in payload["body"]["text"], modalidad == "shalom")

    def test_datos_incompletos_o_cambiar_abre_formulario(self):
        s = {"token": "abc", "paso": "confirmar_entrega", "datos": {"modalidad": "shalom"},
             "contacto_id": "contacto", "phone_number_id": "123"}
        with patch.object(bot, "crear_solicitud", return_value={"solicitudId": "solicitud", "destinatarios": [{"id": "viejo", "direccion": "Casa"}]}), patch.object(bot, "encolar"):
            bot.atender(MagicMock(), s, "solicitar", {"id": "mensaje"}, datetime.now(timezone.utc))
            self.assertEqual(s["paso"], "formulario")
            s["paso"] = "datos_previos"
            s["datos"]["direccion"] = {"nombre_completo": "Anterior"}
            bot.atender(MagicMock(), s, "nuevos", {}, datetime.now(timezone.utc))
            self.assertEqual(s["paso"], "formulario")
            self.assertEqual(s["datos"]["direccion"], {})

    def test_echo_app_no_echo_api_y_numero_correcto(self):
        value = {"metadata": {"phone_number_id": "123"}, "message_echoes": [
            {"id": "echo", "to": "51943875311", "timestamp": str(int(time.time()))}]}
        payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "smb_message_echoes", "value": value}]}]}
        self.assertEqual(list(bot.ecos_humanos(payload, "123"))[0]["numero"], "51943875311")
        self.assertEqual(list(bot.ecos_humanos(payload, "999")), [])
        payload["entry"][0]["changes"][0]["field"] = "message_echoes"
        self.assertEqual(list(bot.ecos_humanos(payload, "123")), [])

    def test_bot_desactivado_no_toca_bd(self):
        with patch.dict("os.environ", {"WHATSAPP_BOT_ENABLED": "false"}), patch.object(bot, "connect") as db:
            bot.procesar({}, {}, [], "123")
            bot.despachar({})
            db.assert_not_called()

    def test_compras_solo_cuenta_verificada(self):
        cur = MagicMock()
        cur.fetchall.return_value = []
        self.assertIn("No encontramos", bot.compras(cur, "contacto"))
        sql = cur.execute.call_args.args[0]
        self.assertIn("u.whatsapp = c.numero", sql)
        self.assertIn("u.whatsapp_verificado = true", sql)
        self.assertIn("u.id = v.cliente_usuario_id", sql)

    def test_tarifas_confirmacion_no_crea_solicitud(self):
        for mode, keyword in (("olva", "antes del despacho"), ("shalom", "Pago destino"), ("contraentrega", "viernes a las 5 pm")):
            s = {"token": "abc", "paso": "entrega", "datos": {}}
            with patch.object(bot, "encolar") as queue, patch.object(bot, "crear_solicitud") as create:
                bot.atender(MagicMock(), s, mode, {}, datetime.now(timezone.utc))
                self.assertIn(keyword, queue.call_args.args[2]["interactive"]["body"]["text"])
                create.assert_not_called()

    def test_worker_revisa_pausa_antes_de_enviar(self):
        cur = MagicMock()
        cur.fetchone.side_effect = [{"id": 1, "phone_number_id": "123", "numero": "51943875311", "token": "abc"},
                                   {"adquirido": True}, {"token": "abc", "pausado": True}]
        db = MagicMock()
        db.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = cur
        with patch.dict("os.environ", {"WHATSAPP_BOT_ENABLED": "true"}), patch.object(bot, "preparar_tablas"), patch.object(bot, "limpiar"), patch.object(bot, "connect", db), patch.object(bot, "enviar_payload") as send:
            bot.despachar({})
            send.assert_not_called()

    def test_worker_indicador_opcional_no_bloquea_saludo(self):
        cur = MagicMock()
        cur.fetchone.side_effect = [{"id": 1, "phone_number_id": "123", "numero": "51943875311", "token": "abc", "payload": {"status": "read"}},
                                   {"adquirido": True}, {"token": "abc", "pausado": False}]
        db = MagicMock()
        db.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = cur
        with patch.dict("os.environ", {"WHATSAPP_BOT_ENABLED": "true"}), patch.object(bot, "preparar_tablas"), patch.object(bot, "limpiar"), patch.object(bot, "connect", db), patch.object(bot, "enviar_payload", side_effect=ValueError("indicador no soportado")):
            bot.despachar({})
            self.assertEqual(cur.execute.call_args.args[1], (True, 1))


@unittest.skipUnless(getenv("BITSTROID_TEST_DATABASE") == "1", "Requiere PostgreSQL local")
class BotDatabaseTests(unittest.TestCase):
    def test_sesion_deduplicacion_envio_y_pausa(self):
        from main import _connection_kwargs
        kwargs = _connection_kwargs()
        if kwargs["host"] not in ("localhost", "127.0.0.1", "::1"):
            self.skipTest("Solo BD local")
        conn = connect(**kwargs, row_factory=dict_row)
        phone_id, numero = "test-" + uuid4().hex, "519" + str(int(uuid4().hex[:8], 16) % 100000000).zfill(8)
        contacto_id = str(uuid4())

        class Prestada:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def cursor(self):
                return conn.cursor()

        def sesion():
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM whatsapp.bot_sesion WHERE phone_number_id = %s AND numero = %s", (phone_id, numero))
                return cur.fetchone()

        def cantidad():
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) AS n FROM whatsapp.bot_respuesta WHERE phone_number_id = %s", (phone_id,))
                return cur.fetchone()["n"]

        def enviar(body="Hola", opcion=None, id_=None):
            id_ = id_ or uuid4().hex
            message = {"id": id_, "type": "text", "text": {"body": body}}
            if opcion:
                message = {"id": id_, "type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": sesion()["token"] + ":" + opcion}}}
            e = {"numero": numero, "mensaje_id": id_, "timestamp": int(time.time()), "mensaje": message, "tipo": message["type"]}
            bot.procesar(kwargs, {}, [e], phone_id)
            return e

        try:
            with conn.cursor() as cur:
                for name in ("whatsapp_contactos.py", "whatsapp_entregas.py", "whatsapp_bot.py"):
                    tree = ast.parse((Path(__file__).parent / name).read_text(encoding="utf-8"))
                    for n in ast.walk(tree):
                        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "execute" and n.args
                                and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)
                                and n.args[0].value.startswith(("CREATE", "ALTER"))):
                            cur.execute(n.args[0].value)
                cur.execute("""INSERT INTO whatsapp.contacto (id, numero, primer_mensaje_en, ultimo_mensaje_en)
                    VALUES (%s, %s, now(), now())""", (contacto_id, numero))
            with patch.dict("os.environ", {"WHATSAPP_BOT_ENABLED": "true"}), patch.object(bot, "preparar_tablas"), patch.object(bot, "connect", return_value=Prestada()):
                e = enviar()
                self.assertEqual(cantidad(), 2)
                bot.procesar(kwargs, {}, [e], phone_id)
                self.assertEqual(cantidad(), 2)
                enviar("Consulta libre")
                self.assertEqual(cantidad(), 2)
                enviar("REGISTRO: ABCD2345")
                self.assertEqual(cantidad(), 2)
                enviar(opcion="entrega")
                enviar(opcion="shalom")
                enviar(opcion="solicitar")
                self.assertEqual(sesion()["paso"], "formulario")
                for value in ("Destinatario Prueba", "12345678", "Lima", "Lima", "Lima", numero, "Agencia Lima, Calle 123"):
                    enviar(value)
                self.assertEqual(sesion()["paso"], "confirmar_datos")
                enviar(opcion="guardar")
                self.assertEqual(sesion()["paso"], "menu")
                with conn.cursor() as cur:
                    cur.execute("SELECT estado, pago_envio, destinatario_id FROM whatsapp.solicitud_entrega WHERE contacto_id = %s", (contacto_id,))
                    request = cur.fetchone()
                    self.assertEqual(request["estado"], "pendiente")
                    self.assertEqual(request["pago_envio"], "destino")
                    self.assertIsNotNone(request["destinatario_id"])
                    cur.execute("SELECT direccion, referencia, agencia_shalom FROM cliente.destinatario_envio WHERE id = %s", (request["destinatario_id"],))
                    destino = cur.fetchone()
                    self.assertEqual(destino["direccion"], "")
                    self.assertEqual(destino["referencia"], "")
                    self.assertEqual(destino["agencia_shalom"], "Agencia Lima, Calle 123")
                echo = {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "smb_message_echoes", "value": {
                    "metadata": {"phone_number_id": phone_id}, "message_echoes": [{"id": "humano", "to": numero, "timestamp": str(int(time.time()))}]}}]}]}
                bot.procesar(kwargs, echo, [], phone_id)
                self.assertTrue(sesion()["pausado"])
                n = cantidad()
                enviar("Otra consulta")
                self.assertEqual(cantidad(), n)
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) AS n FROM whatsapp.bot_respuesta WHERE phone_number_id = %s AND enviado_en IS NULL AND cancelado = false", (phone_id,))
                    self.assertEqual(cur.fetchone()["n"], 0)
                    cur.execute("UPDATE whatsapp.bot_sesion SET ultima_actividad = now() - interval '61 minutes' WHERE phone_number_id = %s", (phone_id,))
                enviar("Hola nuevamente")
                self.assertFalse(sesion()["pausado"])
                self.assertEqual(cantidad(), n + 2)
        finally:
            conn.rollback()
            conn._conn.close()

import unittest
from unittest.mock import MagicMock

from whatsapp_contactos import mensajes_entrantes, guardar_contactos, vincular_usuario


class ContactosTests(unittest.TestCase):
    def payload(self, phone_id="1402483696282363", tipo="text", contacts=None):
        return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp", "metadata": {"phone_number_id": phone_id},
            "contacts": contacts if contacts is not None else [{"wa_id": "51943875311", "profile": {"name": "Juan"}}],
            "messages": [{"from": "51943875311", "id": "wamid.test", "timestamp": "1700000000",
                "type": tipo, "text": {"body": "Hola, contenido privado"}}]}}]}]}

    def test_hola_y_foto_capturan_contacto(self):
        for kind in ["text", "image", "audio", "document"]:
            messages = list(mensajes_entrantes(self.payload(tipo=kind), "1402483696282363"))
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["numero"], "51943875311")
            self.assertEqual(messages[0]["nombre_perfil"], "Juan")
            self.assertEqual(messages[0]["tipo"], kind)

    def test_numero_negocio_distinto_ignorado(self):
        self.assertEqual(list(mensajes_entrantes(self.payload(phone_id="otro"), "1402483696282363")), [])

    def test_perfil_faltante_o_largo(self):
        for contacts in [[], None, [None], [{"wa_id": "51943875311", "profile": None}]]:
            payload = self.payload()
            payload["entry"][0]["changes"][0]["value"]["contacts"] = contacts
            self.assertEqual(list(mensajes_entrantes(payload, "1402483696282363"))[0]["nombre_perfil"], "")
        contacts = [{"wa_id": "51943875311", "profile": {"name": "X" * 200}}]
        self.assertEqual(len(list(mensajes_entrantes(self.payload(contacts=contacts), "1402483696282363"))[0]["nombre_perfil"]), 160)

    def test_estados_no_se_guardan_como_contactos(self):
        payload = self.payload()
        value = payload["entry"][0]["changes"][0]["value"]
        del value["messages"]
        value["statuses"] = [{"recipient_id": "51943875311", "status": "read"}]
        self.assertEqual(list(mensajes_entrantes(payload, "1402483696282363")), [])

    def test_payload_malformado_ignorado(self):
        for payload in [None, [], {}, {"object": "whatsapp_business_account", "entry": [None, {"changes": [None]}]}]:
            self.assertEqual(list(mensajes_entrantes(payload, "1402483696282363")), [])

    def test_mensaje_nuevo_cuenta_y_vincula_solo_cliente_verificado(self):
        cur = MagicMock()
        cur.fetchone.side_effect = [{"id": "contacto"}, {"contacto_id": "contacto"}]
        guardar_contactos(cur, list(mensajes_entrantes(self.payload(), "1402483696282363")), "1402483696282363")
        sql = " ".join(c.args[0] for c in cur.execute.call_args_list)
        self.assertIn("ON CONFLICT (numero)", sql)
        self.assertIn("total_mensajes = total_mensajes + 1", sql)
        self.assertIn("whatsapp_verificado = true", sql)
        self.assertIn("count(*) = 1", sql)
        self.assertIn("GREATEST(ultimo_mensaje_en", sql)
        self.assertNotIn("INSERT INTO seguridad.usuario", sql)
        params = repr([c.args[1] for c in cur.execute.call_args_list])
        self.assertNotIn("contenido privado", params)

    def test_reintento_no_incrementa_contador(self):
        cur = MagicMock()
        cur.fetchone.side_effect = [{"id": "contacto"}, None]
        guardar_contactos(cur, list(mensajes_entrantes(self.payload(), "1402483696282363")), "1402483696282363")
        self.assertEqual(cur.execute.call_count, 2)
        self.assertIn("ON CONFLICT (phone_number_id, mensaje_id) DO NOTHING", cur.execute.call_args.args[0])

    def test_vinculo_no_reemplaza_otro_usuario(self):
        cur = MagicMock()
        vincular_usuario(cur, "51943875311", "usuario")
        self.assertIn("usuario_id IS NULL OR usuario_id = %s", cur.execute.call_args.args[0])
        self.assertEqual(cur.execute.call_args.args[1], ("usuario", "51943875311", "usuario"))


if __name__ == "__main__":
    unittest.main()

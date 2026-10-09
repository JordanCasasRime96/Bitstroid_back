"""Opt-in regression against local PostgreSQL; all schema/data changes roll back."""
import ast
from contextlib import contextmanager
import json
from os import getenv
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from db_compat import connect, dict_row
import whatsapp_entrada as wa


@unittest.skipUnless(getenv("BITSTROID_TEST_DATABASE") == "1", "Requiere BD local y activacion explicita")
class RegistroDatabaseTests(unittest.TestCase):
    def test_clientes_sin_documento_y_datos_opcionales(self):
        from main import _connection_kwargs
        kwargs = _connection_kwargs()
        if kwargs["host"] not in ("localhost", "127.0.0.1", "::1"):
            self.skipTest("Esta prueba solo permite PostgreSQL local")
        conn = connect(**kwargs, row_factory=dict_row)

        class ConexionPrestada:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def cursor(self):
                return conn.cursor()

            @contextmanager
            def transaction(self):
                yield self

        try:
            with conn.cursor() as cur:
                # Run runtime DDL inside the outer rollback-only transaction.
                for name in ("whatsapp_contactos.py", "whatsapp_respuestas.py", "whatsapp_entrada.py"):
                    tree = ast.parse((Path(__file__).parent / name).read_text(encoding="utf-8"))
                    for node in ast.walk(tree):
                        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                                and node.func.attr == "execute" and node.args
                                and isinstance(node.args[0], ast.Constant)
                                and isinstance(node.args[0].value, str)):
                            sql = node.args[0].value
                            if sql.strip().startswith(("CREATE SCHEMA", "CREATE TABLE", "CREATE INDEX", "ALTER TABLE")):
                                cur.execute(sql)
            config = ("s" * 48, "app-test", "v" * 48, "1402483696282363", "51926839501")
            with patch.object(wa, "configuracion", return_value=config), patch.object(wa, "preparar_tabla"), patch.object(wa, "connect", return_value=ConexionPrestada()):
                for nombre, perfil, con_correo in [("", "Perfil WhatsApp", False), ("Nombre elegido", "Perfil WhatsApp", True), ("", "", False)]:
                    id_ = str(uuid4())
                    token = "t" * 43
                    numero = "519" + str(int(uuid4().hex[:8], 16) % 100000000).zfill(8)
                    correo = f"{id_}@example.com" if con_correo else ""
                    datos = {"apodo": "prueba_" + uuid4().hex, "whatsapp": numero,
                             "nombres": nombre, "correo": correo, "hash_contrasena": "solo-prueba-rollback"}
                    with conn.cursor() as cur:
                        if perfil:
                            cur.execute("""INSERT INTO whatsapp.contacto
                                (id, numero, nombre_perfil, primer_mensaje_en, ultimo_mensaje_en)
                                VALUES (%s, %s, %s, now(), now())""", (str(uuid4()), numero, perfil))
                        cur.execute("""INSERT INTO cliente.registro_whatsapp_entrada
                            (id, whatsapp, datos, token_hash, palabra_hash, confirmado)
                            VALUES (%s, %s, %s::jsonb, %s, %s, true)""",
                            (id_, numero, json.dumps(datos), wa.hash_codigo(id_, token, config[0]),
                             wa.hash_codigo(numero, "ABCD2345", config[0])))
                    row = wa.crear_registro(kwargs, id_, token, MagicMock(), "prueba-local")
                    with conn.cursor() as cur:
                        cur.execute("SELECT numero_documento, nombres, correo, whatsapp_verificado FROM seguridad.usuario WHERE id = %s", (str(row["id"]),))
                        creado = cur.fetchone()
                        self.assertIsNone(creado["numero_documento"])
                        self.assertEqual(creado["nombres"], nombre or perfil or None)
                        self.assertEqual(creado["correo"], correo or None)
                        self.assertTrue(creado["whatsapp_verificado"])
                        cur.execute("SELECT 1 FROM cliente.cliente WHERE usuario_id = %s", (str(row["id"]),))
                        self.assertIsNotNone(cur.fetchone())
                        cur.execute("SELECT tipo FROM whatsapp.respuesta_registro WHERE evento = %s", ("cuenta:" + str(row["id"]),))
                        self.assertEqual(cur.fetchone()["tipo"], "creado")
                        if perfil and not nombre:
                            # Existing customers from the old registration also get repaired.
                            cur.execute("UPDATE seguridad.usuario SET nombres = NULL, correo = %s WHERE id = %s",
                                        (f"{id_}@bitstroid.local", str(row["id"])))
                        wa.actualizar_datos_clientes(cur)
                        cur.execute("SELECT nombres, correo FROM seguridad.usuario WHERE id = %s", (str(row["id"]),))
                        reparado = cur.fetchone()
                        self.assertEqual(reparado["nombres"], nombre or perfil or None)
                        self.assertEqual(reparado["correo"], correo or None)
        finally:
            conn.rollback()
            conn._conn.close()


if __name__ == "__main__":
    unittest.main()

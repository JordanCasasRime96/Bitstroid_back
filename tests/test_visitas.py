import hashlib
import hmac
import json
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from fastapi import HTTPException
import app.services.visitas as visitas


class VisitasTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {'VISITAS_SECRET': 'a' * 64})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.body = json.dumps({'id': 'aef099b1-fafe-4bd2-90cb-e1d1902ae868', 'pais': 'pe',
                                'ciudad': 'Lima', 'pagina': '/?token=privado'}).encode()
        self.firma = hmac.new(b'a' * 64, b'1000.' + self.body, hashlib.sha256).hexdigest()

    def test_firma_y_normalizacion(self):
        datos = visitas.validar(self.body, '1000', self.firma, ahora=1001)
        self.assertEqual(datos[1], 'PE')
        self.assertEqual(datos[5], '/')

    def test_rechaza_firma_falsa_y_expirada(self):
        for firma, ahora in [('falsa', 1001), (self.firma, 1400)]:
            with self.subTest(firma=firma), self.assertRaises(HTTPException) as error:
                visitas.validar(self.body, '1000', firma, ahora=ahora)
            self.assertEqual(error.exception.status_code, 401)

    def test_cuerpo_alterado(self):
        with self.assertRaises(HTTPException):
            visitas.validar(self.body.replace(b'Lima', b'Roma'), '1000', self.firma, ahora=1001)

    def test_sql_entregado_coincide_con_preparacion(self):
        path = Path(__file__).resolve().parents[3] / 'Proyeccion de costeo/backend/database/sql/060_visitas_bitstroid.sql'
        self.assertIn(' '.join(visitas.DDL.split()), ' '.join(path.read_text(encoding='utf-8').split()))

    def test_insert_idempotente_sin_ip_ni_cuenta(self):
        conexion = MagicMock()
        with patch.object(visitas, 'preparar'), patch.object(visitas, 'connect', return_value=conexion):
            visitas.guardar({}, visitas.validar(self.body, '1000', self.firma, ahora=1001))
        sql = conexion.__enter__.return_value.cursor.return_value.__enter__.return_value.execute.call_args.args[0]
        self.assertIn('ON CONFLICT (id) DO NOTHING', sql)
        self.assertNotIn('usuario_id', sql)


if __name__ == '__main__':
    unittest.main()

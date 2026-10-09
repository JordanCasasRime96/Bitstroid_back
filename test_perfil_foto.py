import base64
from io import BytesIO
import unittest

from fastapi import HTTPException
from PIL import Image

from perfil_foto import comprimir_foto


class FotoPerfilTests(unittest.TestCase):
    def test_reduce_y_convierte_a_jpeg_sin_metadatos(self):
        original = BytesIO()
        exif = Image.Exif()
        exif[270] = "dato privado"
        Image.new("RGB", (1200, 800), "cyan").save(original, format="JPEG", exif=exif)
        resultado = comprimir_foto(base64.b64encode(original.getvalue()).decode())
        with Image.open(BytesIO(resultado)) as foto:
            self.assertEqual(foto.size, (192, 192))
            self.assertEqual(foto.format, "JPEG")
            self.assertFalse(foto.getexif())
        self.assertLess(len(resultado), 40_000)

    def test_rechaza_base64_invalido(self):
        with self.assertRaises(HTTPException) as error:
            comprimir_foto("no es base64")
        self.assertEqual(error.exception.status_code, 400)

    def test_rechaza_archivo_que_no_es_imagen(self):
        with self.assertRaises(HTTPException) as error:
            comprimir_foto(base64.b64encode(b"texto").decode())
        self.assertEqual(error.exception.status_code, 400)

    def test_limita_tamano_de_subida(self):
        with self.assertRaises(HTTPException) as error:
            comprimir_foto(base64.b64encode(b"x" * (256 * 1024 + 1)).decode())
        self.assertEqual(error.exception.status_code, 413)


if __name__ == "__main__":
    unittest.main()

"""Every literal runtime schema change must also be shipped as deployable SQL."""
import ast
from os import getenv
from pathlib import Path
import unittest


class SqlMigrationsTests(unittest.TestCase):
    def test_runtime_ddl_has_sql_artifact(self):
        base = Path(__file__).parent
        sql_dir = Path(getenv("SQL_MIGRATIONS_DIR") or base.parents[1] / "Proyeccion de costeo" / "backend" / "database" / "sql")
        scripts = list(sql_dir.rglob("*.sql"))
        self.assertTrue(scripts)
        sql = " ".join(" ".join(path.read_text(encoding="utf-8").split()) for path in scripts)
        statements = 0
        for name in ["main.py", "perfil_foto.py", "whatsapp_registro.py", "whatsapp_entrada.py", "whatsapp_contactos.py"]:
            tree = ast.parse((base / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "execute" or not node.args:
                    continue
                value = node.args[0]
                if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                    continue
                statement = " ".join(value.value.split())
                if statement.startswith(("CREATE SCHEMA ", "CREATE TABLE ", "CREATE INDEX ", "ALTER TABLE ")):
                    statements += 1
                    with self.subTest(file=name, line=node.lineno):
                        self.assertIn(statement + ";", sql)
        self.assertGreater(statements, 0)


if __name__ == "__main__":
    unittest.main()

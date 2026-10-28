"""Estadísticas anónimas recibidas exclusivamente del frontend firmado."""
import hashlib
import hmac
import json
import re
import time
from functools import cache
from os import getenv
from uuid import UUID

from fastapi import HTTPException
from db_compat import connect

DDL = """
CREATE SCHEMA IF NOT EXISTS analitica;
CREATE TABLE IF NOT EXISTS analitica.visita (
    id uuid PRIMARY KEY,
    creado_en timestamptz NOT NULL DEFAULT now(),
    pais varchar(2) NOT NULL DEFAULT '',
    region varchar(120) NOT NULL DEFAULT '',
    ciudad varchar(120) NOT NULL DEFAULT '',
    origen varchar(200) NOT NULL DEFAULT '',
    pagina varchar(200) NOT NULL DEFAULT '/',
    dispositivo varchar(20) NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS visita_fecha_idx ON analitica.visita(creado_en);
"""


@cache
def preparar(conexion):
    with connect(**dict(conexion)) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()


def validar(body: bytes, timestamp: str, firma: str, ahora=None):
    secret = getenv('VISITAS_SECRET', '').strip()
    if len(secret) < 32:
        raise HTTPException(503, 'Estadísticas no configuradas')
    try:
        if len(body) > 2048 or abs((time.time() if ahora is None else ahora) - int(timestamp)) > 300:
            raise ValueError()
        esperada = hmac.new(secret.encode(), timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(esperada, firma):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(401, 'Firma no válida')
    try:
        data = json.loads(body)
        ident = str(UUID(data['id']))
        def texto(campo, limite):
            return re.sub(r'[\x00-\x1f\x7f]', '', str(data.get(campo) or '')).strip()[:limite]
        pais = texto('pais', 2).upper()
        if not re.fullmatch('[A-Z]{2}', pais) or pais in ('XX', 'T1'):
            pais = ''
        pagina = texto('pagina', 200).split('?')[0].split('#')[0]
        return (ident, pais, texto('region', 120), texto('ciudad', 120),
                texto('origen', 200), pagina if pagina.startswith('/') else '/',
                texto('dispositivo', 20))
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(400, 'Visita no válida')


def guardar(conexion, datos):
    preparar(tuple(sorted(conexion.items())))
    with connect(**conexion) as conn:
        with conn.cursor() as cur:
            cur.execute('''INSERT INTO analitica.visita
                (id, pais, region, ciudad, origen, pagina, dispositivo)
                VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING''', datos)
        conn.commit()

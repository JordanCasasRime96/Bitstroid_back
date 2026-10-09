import unicodedata

from db_compat import connect, dict_row


def pagina_catalogo(connection_kwargs, pagina, limite, orden, busqueda, plataforma, ids):
    orden_sql = {
        'reciente': 'creado_en DESC, id DESC',
        'antiguo': 'creado_en ASC, id ASC',
        'menor-precio': 'precio_desde ASC NULLS LAST, id ASC',
        'mayor-precio': 'precio_desde DESC NULLS LAST, id ASC',
        'visitados': 'array_position(%s::text[], p.id::text), p.id ASC',
    }[orden]
    consulta = ''.join(c for c in unicodedata.normalize('NFD', busqueda.strip().lower()) if not unicodedata.combining(c))
    patron = '%' + consulta.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
    seleccion = ids.split(',') if ids else ([] if orden == 'visitados' else None)
    orden_params = (seleccion,) if orden == 'visitados' else ()
    with connect(**connection_kwargs, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT p.id, p.creado_en, MIN(CASE WHEN i.precio_venta_soles > 0 THEN i.precio_venta_soles END) AS precio_desde
                FROM inventario.publicacion p
                JOIN inventario.publicacion_item pi ON pi.publicacion_id = p.id
                JOIN inventario.item i ON i.id = pi.item_id
                WHERE lower(COALESCE(p.estado, '')) = 'publicado'
                  AND COALESCE(p.visible, true)
                  AND lower(COALESCE(i.estado, '')) = 'disponible'
                  AND COALESCE(i.stock_disponible, 0) > 0
                  AND (%s::text[] IS NULL OR p.id::text = ANY(%s::text[]))
                  AND (%s = '' OR EXISTS (
                      SELECT 1 FROM inventario.publicacion_item pb
                      JOIN inventario.item ib ON ib.id = pb.item_id
                      LEFT JOIN costeo.categoria_compra cb ON cb.id = ib.categoria_id
                      WHERE pb.publicacion_id = p.id AND cb.abreviatura = %s
                        AND lower(COALESCE(ib.estado, '')) = 'disponible' AND COALESCE(ib.stock_disponible, 0) > 0
                  ))
                  AND (%s = '' OR translate(lower(p.titulo), 'áéíóúüñ', 'aeiouun') LIKE %s ESCAPE '!' OR EXISTS (
                      SELECT 1 FROM inventario.publicacion_item pb
                      JOIN inventario.item ib ON ib.id = pb.item_id
                      WHERE pb.publicacion_id = p.id
                        AND translate(lower(ib.nombre), 'áéíóúüñ', 'aeiouun') LIKE %s ESCAPE '!'
                        AND lower(COALESCE(ib.estado, '')) = 'disponible' AND COALESCE(ib.stock_disponible, 0) > 0
                  ))
                GROUP BY p.id, p.creado_en
                ORDER BY {orden_sql}
                LIMIT %s OFFSET %s
            """, (seleccion, seleccion, plataforma, plataforma, consulta, patron, patron, *orden_params, limite + 1, (pagina - 1) * limite))
            publicaciones = cur.fetchall()
            hay_mas = len(publicaciones) > limite
            seleccion_ids = [str(row['id']) for row in publicaciones[:limite]]
            items = detalle_items(cur, seleccion_ids, False) if seleccion_ids else []
            cur.execute("""
                SELECT DISTINCT cat.abreviatura AS sigla, cat.nombre
                FROM inventario.publicacion p
                JOIN inventario.publicacion_item pi ON pi.publicacion_id = p.id
                JOIN inventario.item i ON i.id = pi.item_id
                JOIN costeo.categoria_compra cat ON cat.id = i.categoria_id
                WHERE lower(COALESCE(p.estado, '')) = 'publicado' AND COALESCE(p.visible, true)
                  AND lower(COALESCE(i.estado, '')) = 'disponible' AND COALESCE(i.stock_disponible, 0) > 0
                ORDER BY cat.nombre
            """)
            plataformas = [[row['sigla'], row['nombre'] or row['sigla']] for row in cur.fetchall() if row['sigla']]
    posicion = {id_: index for index, id_ in enumerate(seleccion_ids)}
    items.sort(key=lambda item: posicion[item['publicacionId']])
    return {'items': items, 'hayMas': hay_mas, 'pagina': pagina, 'plataformas': plataformas}


def detalle_items(cur, ids, incluir_fotos):
    fotos_sql = """COALESCE((SELECT json_agg(json_build_object(
        'ruta', f.ruta, 'orden', f.orden, 'esPortada', COALESCE((f.metadata->>'es_portada')::boolean, false))
        ORDER BY COALESCE((f.metadata->>'es_portada')::boolean, false) DESC, f.orden, f.creado_en)
        FROM inventario.publicacion_foto f WHERE f.publicacion_id = p.id), '[]'::json)""" if incluir_fotos else "'[]'::json"
    portada_sql = 'f.ruta' if incluir_fotos else "COALESCE(NULLIF(f.metadata->>'miniatura_ruta', ''), f.ruta)"
    cur.execute(f"""
        SELECT p.id AS publicacion_id, p.codigo, p.titulo, p.descripcion, p.creado_en,
               i.id AS item_id, i.codigo AS sku, i.nombre, i.precio_venta_soles, i.stock_disponible,
               cat.nombre AS categoria_nombre, cat.abreviatura,
               estilo.codigo AS estilo_codigo, estilo.nombre AS estilo_nombre,
               (SELECT {portada_sql} FROM inventario.publicacion_foto f WHERE f.publicacion_id = p.id
                ORDER BY COALESCE((f.metadata->>'es_portada')::boolean, false) DESC, f.orden, f.creado_en LIMIT 1) AS imagen_url,
               {fotos_sql} AS fotos
        FROM inventario.publicacion p
        JOIN inventario.publicacion_item pi ON pi.publicacion_id = p.id
        JOIN inventario.item i ON i.id = pi.item_id
        LEFT JOIN costeo.categoria_compra cat ON cat.id = i.categoria_id
        LEFT JOIN publicacion.categoria_estilo_visual estilo ON estilo.id = cat.estilo_visual_id
        WHERE p.id::text = ANY(%s::text[])
          AND lower(COALESCE(p.estado, '')) = 'publicado' AND COALESCE(p.visible, true)
          AND lower(COALESCE(i.estado, '')) = 'disponible' AND COALESCE(i.stock_disponible, 0) > 0
        ORDER BY pi.creado_en, i.id
    """, (ids,))
    return [{
        'publicacionId': str(row['publicacion_id']), 'codigo': row['codigo'] or '',
        'titulo': row['titulo'] or '', 'descripcion': row['descripcion'] or '',
        'fechaCreacion': row['creado_en'].isoformat() if row['creado_en'] else '',
        'imagenUrl': row['imagen_url'] or '', 'fotos': row['fotos'] or [],
        'itemId': str(row['item_id']), 'sku': row['sku'] or '', 'nombre': row['nombre'] or '',
        'categoriaNombre': row['categoria_nombre'] or '', 'categoriaAbreviatura': row['abreviatura'] or '',
        'estiloVisualCodigo': row['estilo_codigo'] or 'default_azul', 'estiloVisualNombre': row['estilo_nombre'] or 'Default azul',
        'precio': float(row['precio_venta_soles'] or 0), 'moneda': 'PEN', 'stockDisponible': int(row['stock_disponible'] or 0),
    } for row in cur.fetchall()]

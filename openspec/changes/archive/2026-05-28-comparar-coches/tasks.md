# Tareas — comparar-coches

## 1. Auditoría del código actual

- [x] 1.1 Leer [comparar_pipeline.py](../../../comparar_pipeline.py) entero y mapear cada función a un requisito del spec
- [x] 1.2 Leer [main.py:2195-2307](../../../main.py#L2195) (handler + flujo `_comparar_procesar_texto`) y mapear cada paso a un requisito
- [x] 1.3 Confirmar que `parsear_comparar_input`, `enriquecer_modelo` y `generar_veredicto_comparar` existen en [ai.py](../../../ai.py) y siguen los contratos descritos en el spec
- [x] 1.4 Documentar en notas qué requisitos del spec YA están cumplidos y cuáles necesitan ajuste

## 2. Constantes a config.py

- [x] 2.1 Mover `KM_AÑO_REF = 15000` y `PRECIO_LITRO = 1.55` de `comparar_pipeline.py:_procesar_lado` a [config.py](../../../config.py) como `COMPARAR_KM_AÑO_REF` y `COMPARAR_PRECIO_LITRO_EUR`
- [x] 2.2 Importar ambas en `comparar_pipeline.py` y reemplazar los literales
- [x] 2.3 Verificar que ningún test/script externo dependía de los nombres locales

## 3. Verificar prompt del veredicto IA

- [x] 3.1 Abrir `generar_veredicto_comparar` en `ai.py` y comprobar que el system prompt instruye explícitamente a usar `nombre_display` de cada lado verbatim
- [x] 3.2 Si no lo hace, añadir línea al prompt: "Refiérete a los coches SIEMPRE por su `nombre_display`. NUNCA uses 'A', 'B', 'lado_a', 'lado_b' ni paráfrasis"
- [ ] 3.3 Smoke test manual: `/comparar Golf 7 GTI vs Civic Type R FK7` y verificar que el HTML usa "Volkswagen Golf Mk7 GTI" / "Honda Civic FK7 Type R" en todos los bloques

## 4. Verificar persistencia en histórico

- [x] 4.1 Confirmar que `_procesar_lado` llama `guardar_historico_batch(validos)` en cada lado (ya está en la línea ~370)
- [x] 4.2 Confirmar que `guardar_historico_batch` aplica los filtros `precio>0 y año>1990` (ya está documentado en spec `dataset-historico`)
- [ ] 4.3 Ejecutar `/comparar` de prueba y verificar con `sqlite3 cabeza_bot.db "SELECT COUNT(*) FROM historico_precios"` que el contador subió en ambos lados

## 5. Verificar coste y descuento de crédito

- [x] 5.1 Confirmar que el decorator en `main.py:2195` es `@requiere_acceso("/comparar", registrar=False)`
- [x] 5.2 Confirmar que `registrar_uso(user.id, 1)` se llama solo tras `_enviar_largo` exitoso
- [x] 5.3 Confirmar que en las ramas de timeout y excepción NO se llama `registrar_uso`
- [ ] 5.4 Test manual con usuario free: forzar timeout (modelo muy raro) y verificar que el crédito NO se descontó

## 6. Verificar timeout 180s

- [x] 6.1 Confirmar `asyncio.wait_for(ejecutar_pipeline(sesion), timeout=180)` en `_comparar_procesar_texto`
- [x] 6.2 Confirmar mensaje "⏱ La comparativa tardó demasiado. Reintenta en 1 min." en la rama TimeoutError

## 7. Verificar detección de mismo coche

- [x] 7.1 Confirmar bloque `# 4. Detectar mismo coche en ambos lados` en `alimentar_slots`
- [ ] 7.2 Test manual: `/comparar Golf 7 GTI vs Golf 7 GTI` → debe pedir un segundo coche distinto

## 8. Tests manuales de los 3 formatos de entrada

- [ ] 8.1 Test URL + URL: `/comparar <wallapop_url> vs <coches_net_url>` — verificar slots auto-rellenados y veredicto coherente
- [ ] 8.2 Test modelo NL + modelo NL: `/comparar Megane RS vs Leon Cupra` — verificar slot-filling de generación si falta
- [ ] 8.3 Test URL + modelo NL: `/comparar <wallapop_url> vs Civic Type R FK7` — verificar mezcla correcta

## 9. Documentación

- [x] 9.1 Confirmar que [openspec/specs/comparar-coches/spec.md](../../specs/comparar-coches/spec.md) NO existe todavía (lo crea el archive)
- [ ] 9.2 Tras testear OK, ejecutar `openspec validate comparar-coches --strict` y confirmar 0 errores
- [ ] 9.3 Ejecutar `/opsx:archive` cuando esté en producción para fusionar el delta en `openspec/specs/`

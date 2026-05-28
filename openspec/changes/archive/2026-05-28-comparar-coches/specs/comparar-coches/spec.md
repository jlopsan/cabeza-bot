## ADDED Requirements

### Requirement: Tres formatos de entrada (URLs, modelos NL, mezcla)
El comando `/comparar` SHALL aceptar tres formatos en el argumento de texto:
1. **Dos URLs** de Wallapop o Coches.net separadas por separador (vs, contra, espacio).
2. **Dos modelos en lenguaje natural** (ej. "Golf 7 GTI vs Civic Type R FK7").
3. **Mezcla**: una URL + un modelo NL.

#### Scenario: Dos URLs en una línea
- **WHEN** el usuario envía `/comparar https://wallapop.com/... vs https://coches.net/...`
- **THEN** el bot extrae ambas URLs vía regex `_URL_RE`, descarga ambos anuncios en paralelo con `obtener_anuncio_por_url`, rellena `lado_a` y `lado_b` con marca/modelo/año del anuncio, y elimina las URLs del texto antes de pasar al parser NL

#### Scenario: Dos modelos NL en una línea
- **WHEN** el usuario envía `/comparar Golf 7 GTI vs Civic Type R FK7`
- **THEN** el bot llama `parsear_comparar_input` con el texto completo y rellena ambos lados con marca, modelo, generación y versión_motor

#### Scenario: URL + modelo NL
- **WHEN** el usuario envía `/comparar https://wallapop.com/item/abc vs Megane RS`
- **THEN** la URL rellena el primer lado vacío y el texto restante "Megane RS" se pasa al parser NL para el otro lado

### Requirement: Slot-filling multi-turno
El bot MUST mantener una sesión en memoria por user_id con TTL 30 minutos. Cuando los slots están incompletos, SHALL preguntar por el campo faltante uno a la vez (identidad de un lado, o generación/año) sin cerrar la conversación.

#### Scenario: Falta segundo coche
- **WHEN** el usuario solo dice `Golf 7 GTI` sin un segundo coche
- **THEN** el bot guarda lado_a, responde "¿Cuál es el segundo coche?" y queda en estado `COMPARAR_FILLING` esperando

#### Scenario: Falta generación de un lado
- **WHEN** el usuario dice `BMW Serie 3 vs Audi A4` sin especificar generación
- **THEN** tras parsear y detectar `año_central=0` en lado_a, el bot pregunta por la generación/año específicos del BMW Serie 3

#### Scenario: Sesión caducada
- **WHEN** han pasado >30 min desde el último mensaje y el usuario responde
- **THEN** `get_sesion` devuelve None y el bot responde "⏰ Sesión caducada. Vuelve a lanzar /comparar." cerrando el ConversationHandler

### Requirement: Detección de mismo coche en ambos lados
Si tras alimentar slots los dos lados coinciden en marca, modelo, generación y versión_motor, el bot MUST resetear lado_b, marcar `slot_preguntando="lado_b.identidad"` y pedir un segundo coche distinto en vez de lanzar el pipeline.

#### Scenario: Usuario repite mismo coche
- **WHEN** el usuario envía `Golf 7 GTI vs Golf 7 GTI`
- **THEN** el bot detecta colisión, vacía lado_b, responde "⚠️ Los dos lados son el mismo coche. Dime un segundo coche distinto." y queda esperando

### Requirement: Pipeline paralelo por lado
Cuando los slots están completos, el pipeline MUST procesar lado A y lado B en paralelo vía `asyncio.gather(_procesar_lado(a), _procesar_lado(b))`. Cada lado SHALL:
1. Buscar comparables vía `buscar_comparables_todas` (n=40, km_ref calculado por año).
2. Filtrar `precio>0`, `año>1990`, y rango de año si `año_min/año_max` definidos.
3. Calcular stats `n`, `mediana`, `p25`, `p75`.
4. Persistir comparables válidos vía `guardar_historico_batch` (cumple [[dataset-historico]]).
5. Calcular etiqueta DGT + info ZBE deterministas.
6. Enriquecer con `enriquecer_modelo` (Tavily + IA, fiabilidad / averías / consumo / mantenimiento / depreciación).

#### Scenario: Ambos lados con suficientes comparables
- **WHEN** el pipeline ejecuta con dos lados válidos
- **THEN** lado_a y lado_b se calculan en paralelo, ambos persisten en histórico, y devuelven dicts con stats + DGT + enriquecimiento

#### Scenario: Lado sin comparables
- **WHEN** un lado devuelve 0 comparables válidos
- **THEN** stats queda `{n:0, mediana:0, p25:0, p75:0}` pero el pipeline continúa hacia el veredicto IA (que decide cómo manejar el dato faltante)

### Requirement: Derivados deterministas (TCO 3 años)
El procesador de cada lado MUST calcular sin llamar al LLM los siguientes derivados, usando `KM_AÑO_REF=15000`, `PRECIO_LITRO=1.55€`:
- `coste_combustible_anual_eur = round(consumo * 15000 / 100 * 1.55)` si `consumo > 0`
- `perdida_3a_eur = round(mediana * depreciacion_pct / 100)` si depreciación y mediana > 0
- `valor_residual_eur = max(0, mediana - perdida_3a_eur)`
- `tco_3a_eur = perdida_3a + 3*mantenimiento + 3*combustible_anual`

#### Scenario: Cálculo TCO con datos completos
- **WHEN** un lado tiene consumo=6.5, mantenimiento=700, depreciacion=35%, mediana=20000
- **THEN** TCO_3a = round(7000 + 2100 + ~3000) ≈ 12100€ calculado por Python, NO por el LLM

#### Scenario: Cálculo TCO con datos parciales
- **WHEN** falta el consumo del lado A pero está mantenimiento y depreciación
- **THEN** `coste_combustible_anual_eur=None`, `tco_3a_eur=None`, los demás derivados se calculan; el veredicto IA recibe los Nones explícitos

### Requirement: Veredicto IA con identificadores verbatim
El veredicto MUST llamar a `generar_veredicto_comparar(a, b)` pasando los dos dicts ya enriquecidos. La IA MUST referirse a los coches por `nombre_display` (ej. "Volkswagen Golf Mk7 GTI") y nunca por etiquetas genéricas tipo "A", "B", "lado_a".

#### Scenario: Render del veredicto
- **WHEN** el veredicto se genera con `a.nombre_display="Volkswagen Golf Mk7 GTI"` y `b.nombre_display="Honda Civic FK7 Type R"`
- **THEN** el HTML resultante usa esos nombres en todos los bloques (mercado, DGT, fiabilidad, TCO, ganador)

### Requirement: Timeout duro 180 segundos
El pipeline MUST cancelarse si `ejecutar_pipeline` tarda más de 180 segundos. En timeout, el bot SHALL responder "⏱ La comparativa tardó demasiado. Reintenta en 1 min.", borrar la sesión y cerrar el ConversationHandler.

#### Scenario: Pipeline tarda >180s
- **WHEN** `asyncio.wait_for(ejecutar_pipeline(...), timeout=180)` lanza TimeoutError
- **THEN** el bot envía mensaje de timeout, llama `borrar_sesion(user_id)` y devuelve `ConversationHandler.END`

### Requirement: Coste de crédito solo si llega al veredicto
El handler de `/comparar` MUST usar `@requiere_acceso("/comparar", registrar=False)` para chequear acceso sin descontar al inicio. SHALL llamar `registrar_uso(user_id, 1)` solo si el veredicto HTML se entrega con éxito al usuario. En caso de timeout, excepción del pipeline o veredicto vacío, NO descuenta crédito.

#### Scenario: Pipeline falla con excepción
- **WHEN** el pipeline lanza una excepción no controlada
- **THEN** el bot avisa, borra sesión, y NO llama `registrar_uso` — el usuario conserva su crédito

#### Scenario: Veredicto entregado con éxito
- **WHEN** `_enviar_largo(html_veredicto)` completa sin excepción
- **THEN** el bot llama `registrar_uso(user_id, 1)` antes de borrar sesión

### Requirement: Cumplimiento con capabilities consumidas
El comando MUST consumir las capabilities existentes sin modificar sus contratos:
- `scraping-multifuente`: usa `buscar_comparables_todas` y `obtener_anuncio_por_url`.
- `dataset-historico`: persiste cada batch vía `guardar_historico_batch` aplicando `precio>0 y año>1990`.
- `freemium-creditos`: respeta el decorator y la regla de coste 1.
- `analizar-anuncio`: reutiliza el extractor de URLs y la lógica de obtener anuncio individual.

#### Scenario: Búsqueda de comparables persiste en histórico
- **WHEN** un lado obtiene 30 comparables válidos
- **THEN** `guardar_historico_batch` los inserta en la tabla `historico_precios` aplicando los filtros del spec de `dataset-historico`

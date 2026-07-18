## ADDED Requirements

### Requirement: Crear misión con /sniper vía lenguaje natural
El comando `/sniper <texto>` MUST parsear con IA (una sola llamada, en la creación) los filtros de la misión: marca y modelo (obligatorios), rango de años, km máx, precio máx DE (opcionales: combustible, caja, potencia mín). Si faltan marca o modelo, el bot MUST preguntarlos (multi-turn, patrón `/ideal`). Antes de activar, el bot MUST mostrar los slots interpretados y pedir confirmación. `/buscar` MUST funcionar como alias del mismo entry point. La marca y el modelo parseados MUST persistirse en la misión: el ciclo del worker NUNCA llama a IA.

#### Scenario: Creación con frase completa
- **WHEN** el usuario envía `/sniper vigila BMW 320d 2019-2021 hasta 25.000€ menos de 100.000 km`
- **THEN** el bot muestra los slots detectados (marca=BMW, modelo=320d, años 2019-2021, precio máx 25.000, km máx 100.000), pide confirmación, y al confirmar crea la misión y responde con confirmación de vigilancia activa

#### Scenario: Faltan campos obligatorios
- **WHEN** el usuario envía `/sniper coches diésel baratos` (sin marca/modelo)
- **THEN** el bot pregunta marca y modelo antes de crear nada

### Requirement: Siembra de snapshot sin alertas en la primera pasada
La primera pasada de una misión nueva MUST registrar todos los anuncios visibles como `tipo='snapshot'` en `alertas_enviadas` y marcar `snapshot_sembrado=1` SIN enviar ninguna alerta. La detección de "anuncio nuevo" MUST compararse contra este snapshot (IDs registrados), NUNCA contra el timestamp de publicación del portal.

#### Scenario: Misión nueva con 40 anuncios en mercado
- **WHEN** se crea una misión y la primera pasada del worker encuentra 40 anuncios que cumplen los filtros
- **THEN** los 40 se registran como snapshot, el usuario recibe cero alertas, y la misión queda sembrada

#### Scenario: Anuncio nuevo tras la siembra
- **WHEN** en una pasada posterior aparece un anuncio cuyo ID no está registrado para la misión
- **THEN** ese anuncio se evalúa como candidato a alerta

### Requirement: Deduplicación de alertas con detección de re-publicación
El sistema MUST NOT alertar dos veces el mismo `anuncio_id` para la misma misión (`UNIQUE(mision_id, anuncio_id)` en `alertas_enviadas`). Además MUST calcular una huella `sha1(marca|modelo|año|km//500|precio//100)` y, si un anuncio con ID nuevo coincide en huella con uno ya registrado en los últimos 30 días, MUST registrarlo como visto sin alertar (re-publicación del mismo coche).

#### Scenario: Mismo anuncio en dos pasadas
- **WHEN** un anuncio ya alertado sigue publicado en la siguiente pasada
- **THEN** no se envía segunda alerta

#### Scenario: Vendedor borra y re-publica el anuncio
- **WHEN** aparece un anuncio con ID nuevo pero misma huella (mismo coche, km ±500, precio similar) que uno registrado hace <30 días
- **THEN** se registra como snapshot y no se alerta

### Requirement: Evaluación de candidato contra valoración de mercado ES cacheada
Un candidato MUST evaluarse contra la valoración de mercado española cacheada en `valoraciones_mercado` (mediana de comparables Wallapop + Coches.net, clave marca+modelo+año+banda de km de 20.000, TTL `VALORACION_TTL_H` default 12 h). El ciclo sniper MUST NOT scrapear fuentes ES: si la valoración caducó, refresca como máximo UNA por pasada y el candidato espera a la siguiente pasada sin marcarse como visto. La valoración se calcula en caliente al crear la misión (reutilizando `buscar_comparables_todas` + la estadística de `/analizar`) y cada refresco MUST persistir los comparables en `historico_precios`.

#### Scenario: Candidato con valoración fresca
- **WHEN** aparece un candidato y su valoración cacheada tiene <12 h
- **THEN** se evalúa inmediatamente sin scraping ES

#### Scenario: Valoración caducada
- **WHEN** aparece un candidato y su valoración tiene >12 h
- **THEN** la pasada refresca esa valoración (máx una por pasada), el candidato no se marca como visto, y se evalúa en la siguiente pasada

### Requirement: Alerta solo con margen por encima del umbral doble
Una alerta MUST enviarse solo si `margen_neto >= umbral_margen_eur` (default `SNIPER_UMBRAL_EUR=1500`) Y `margen_pct >= umbral_margen_pct` (default `SNIPER_UMBRAL_PCT=10`). Ambos umbrales MUST ser editables por misión. El sistema MUST limitar las alertas a `SNIPER_ALERTAS_PASADA` (default 3) por misión y pasada; el resto de candidatos que superen umbral se registran como vistos sin alerta.

#### Scenario: Margen bajo el umbral
- **WHEN** un candidato arroja margen neto de 900€ con umbral 1500€
- **THEN** no se alerta y el anuncio queda registrado como visto

#### Scenario: Avalancha de candidatos
- **WHEN** 6 candidatos superan umbral en la misma pasada
- **THEN** se alertan los 3 de mayor margen y los otros 3 quedan registrados sin alerta

### Requirement: Cuenta de importación con IEDMT sobre valor de mercado
El cálculo del margen MUST usar: `iedmt = mediana_es × tipo_iedmt(co2)` (base = valor de mercado ES, NUNCA el precio de compra DE), tramos 0% ≤120 g/km · 4,75% 121-159 · 9,75% 160-199 · 14,75% ≥200; `importacion = COSTE_TRANSPORTE + COSTE_COC_GESTION + COSTE_HOMOLOGACION_ITV + COSTE_TASAS_DGT + iedmt` (todos por env, defaults 1000/400/300/100); `margen_neto = mediana_es − precio_de − importacion` y `margen_pct` sobre la inversión total. El IEDMT MUST etiquetarse siempre como "estimado". Si el anuncio no trae CO₂, el sistema MUST usar una heurística determinista (combustible × año) y marcar "IEDMT estimado (sin CO₂ del anuncio)" — sin llamadas IA.

#### Scenario: Candidato con CO₂ conocido
- **WHEN** un candidato trae CO₂ = 130 g/km, precio DE 21.900€ y mediana ES 28.400€
- **THEN** el IEDMT es 28.400 × 4,75% = 1.349€ (estimado), la importación suma costes fijos + IEDMT y el margen neto se calcula sobre esa cuenta

#### Scenario: Candidato sin CO₂
- **WHEN** el anuncio no expone CO₂
- **THEN** se aplica la heurística determinista por combustible y año, y la tarjeta indica que el IEDMT es estimado sin CO₂ del anuncio

### Requirement: Avisos fiscales en la alerta
La tarjeta MUST mostrar aviso de IVA español (21%, sin auto-sumarlo) cuando el coche sea nuevo fiscal (<6 meses O <6.000 km), y MUST mostrar flag informativo cuando el precio DE sea Netto (MwSt. ausweisbar, típico de Händler para export).

#### Scenario: Coche con 4.000 km
- **WHEN** un candidato tiene 4.000 km
- **THEN** la tarjeta incluye el aviso de que el IVA español se sumaría a la cuenta

#### Scenario: Precio Netto de Händler
- **WHEN** el anuncio marca el precio como Netto/MwSt. ausweisbar
- **THEN** la tarjeta lo señala como flag informativo para compradores con NIF-IVA

### Requirement: Tarjeta de alerta con formato del vídeo
La alerta MUST incluir: título/año/km del coche, precio DE, mercado ES (mediana), coste de importación (total; desglose solo si el IEDMT es fino), margen neto en € y %, score de confianza por nº de comparables (≥8 🟢, 4-7 🟡, <4 🔴 con advertencia explícita), y botón "Ver anuncio" con la URL. Todo campo scrapeado MUST pasar por `html.escape()`. Mensajes >4000 chars MUST usar `_enviar_largo()`. El tono es directo estilo Juan ("ha saltado uno con 4.400€ de margen"), no burocrático. La tarjeta MUST NOT prometer datos que el sistema no tiene (sin CO₂ → total agregado sin desglose).

#### Scenario: Alerta con margen y confianza alta
- **WHEN** un candidato supera umbral con 12 comparables ES
- **THEN** la tarjeta muestra la cuenta completa con 🟢 y botón "Ver anuncio"

#### Scenario: Pocos comparables
- **WHEN** la valoración se basa en 3 comparables
- **THEN** la tarjeta muestra 🔴 y advierte que el margen es poco fiable

### Requirement: Gestión de misiones desde Telegram
`/sniper` sin argumentos MUST listar las misiones del usuario con estado, filtros, umbral y alertas enviadas, con botones inline para pausar, reanudar, borrar y editar umbral. Las misiones MUST expirar automáticamente a los `SNIPER_MISION_DIAS` (default 30) pasando a estado `EXPIRADA` (sin scraping). Renovar una expirada MUST ser gratis, con confirmación, y re-sembrar snapshot. Pausar/reanudar/borrar/editar MUST NOT consumir créditos.

#### Scenario: Listado de misiones
- **WHEN** un usuario con 2 misiones envía `/sniper`
- **THEN** ve ambas con su estado y botones de gestión

#### Scenario: Misión de 31 días
- **WHEN** una misión supera `SNIPER_MISION_DIAS` sin renovarse
- **THEN** pasa a `EXPIRADA`, deja de scrapear y el usuario puede renovarla gratis con confirmación

### Requirement: Feature flag ENABLE_SNIPER
Con `ENABLE_SNIPER=false`, el handler `/sniper` MUST responder que la función está en construcción y el ciclo sniper del worker MUST dormir sin procesar misiones. El resto del bot no se ve afectado.

#### Scenario: Flag apagado en producción
- **WHEN** `ENABLE_SNIPER=false` y un usuario envía `/sniper`
- **THEN** recibe mensaje corto de "en construcción" y no se crea nada

### Requirement: Embudo instrumentado y /stats_sniper
El sistema MUST registrar en la tabla `eventos(user_id, evento, meta, ts)` los eventos `mision_creada`, `alerta_enviada`, `paywall_visto` y `pago_ok`. El comando `/stats_sniper` (solo admin) MUST mostrar: misiones por estado, alertas en 24 h y 7 días, estado del circuit breaker y conversión por `fuente_captacion`.

#### Scenario: Alerta enviada
- **WHEN** el worker envía una alerta
- **THEN** se inserta el evento `alerta_enviada` con la misión y el margen en `meta`

#### Scenario: Admin consulta métricas
- **WHEN** un admin envía `/stats_sniper`
- **THEN** recibe el resumen de misiones, alertas, breaker y conversión por fuente

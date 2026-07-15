## ADDED Requirements

### Requirement: Comando /tasar pide datos del coche
El bot SHALL exponer el comando `/tasar` que, sin URL ni precio de partida, solicita al usuario los datos del coche (marca, modelo, año y km) en texto libre.

#### Scenario: /tasar sin datos
- **WHEN** el usuario envía `/tasar` sin más texto
- **THEN** el bot responde pidiendo marca, modelo, año y km en texto libre con un ejemplo ("VW Golf 2018 · 120.000 km"), y activa el estado de espera de datos de tasación en `ctx.user_data` con una clave propia distinta de la de `/analizar`

#### Scenario: /tasar con datos en la misma línea
- **WHEN** el usuario envía `/tasar golf 2018 120000km`
- **THEN** el bot parsea los datos directamente sin pedir un segundo mensaje y procede a tasar

### Requirement: Parseo de datos con precio ignorado
El bot SHALL extraer marca, modelo, año y km del texto libre reutilizando `parsear_datos_anuncio_manual`, ignorando cualquier precio que el usuario incluya.

#### Scenario: Usuario incluye precio
- **WHEN** el usuario escribe "golf 2018 120000km 9500€" en modo tasación
- **THEN** el bot usa marca/modelo/año/km y descarta el precio, porque la tasación no parte de un precio dado

#### Scenario: Faltan campos críticos
- **WHEN** el LLM no puede extraer marca, modelo o año
- **THEN** el bot pregunta específicamente los campos críticos que faltan sin reiniciar el flujo ni consumir un crédito adicional

#### Scenario: Falta km
- **WHEN** el usuario da marca, modelo y año pero no km
- **THEN** el bot tasa igualmente el modelo/año sin filtro de km, avisando de que la estimación es más amplia

### Requirement: Afinar la tasación por motor (CV / combustible)
El bot SHALL detectar en el texto del usuario el motor del coche (CV y/o combustible) y, si se da, tasar solo con comparables de ese motor. El modelo solo es poco preciso: distintas motorizaciones tienen precios muy distintos.

#### Scenario: Usuario indica CV
- **WHEN** el usuario escribe un CV (p. ej. "golf 2018 2.0 tdi 150cv") o un motor identificable
- **THEN** el bot filtra los comparables a los de CV similar (tolerancia ±20, ampliable) y tasa solo con ellos, indicando el motor usado en la respuesta

#### Scenario: Variante de alta potencia con pocos comparables
- **WHEN** el usuario pide un CV de gama alta (p. ej. GTI 245cv) y hay muy pocos comparables de ese motor
- **THEN** el bot tasa con esos pocos (aunque sean 1-2), marcando "muestra pequeña" y confianza baja, y NUNCA cae al precio del modelo base (no mezcla el GTI con Golfs de 110cv)

#### Scenario: Solo combustible
- **WHEN** el usuario indica combustible pero no CV (p. ej. "golf 2018 diésel")
- **THEN** el bot filtra por combustible si hay 3 o más comparables; si no, tasa el modelo completo avisando

#### Scenario: Sin motor
- **WHEN** el usuario no indica CV ni combustible
- **THEN** el bot tasa el modelo completo con el recorte estadístico de gama alta/outliers

### Requirement: Tasación robusta con banda de negociación
El bot SHALL calcular la tasación a partir de comparables reales (`buscar_comparables_todas`), recortando variantes de gama alta y precios anómalos, y devolver un valor de mercado con una banda de negociación estrecha: valor (mediana del grueso recortado), precio de oferta (valor −8%) y precio de venta (valor +8%).

#### Scenario: Recorte de gama alta y outliers
- **WHEN** el conjunto de comparables mezcla variantes de distinta gama (p. ej. base y versiones de alta potencia) o precios anómalos
- **THEN** el bot recorta iterativamente los precios lejos de la mediana (mantiene los que caen en una banda de ratio a la mediana) antes de calcular el valor, para que el valor refleje el grueso del mercado y no los extremos

#### Scenario: Modelo con comparables suficientes
- **WHEN** quedan 3 o más comparables tras el recorte
- **THEN** el bot muestra el valor de mercado, la oferta objetivo (−8%) como "qué ofertar si compras", el precio objetivo (+8%) como "qué pedir si vendes", el número de comparables usados (indicando cuántos se excluyeron) y un score de confianza 🟢/🟡/🔴

#### Scenario: Banda de negociación, no rango de mercado completo
- **WHEN** el bot presenta oferta y precio de venta
- **THEN** esos números son una banda estrecha alrededor del valor (margen de negociación), no el mínimo y máximo del mercado, evitando rangos exagerados por mezcla de acabados

#### Scenario: Modelo poco común
- **WHEN** hay menos de 3 comparables con precio>0 (o menos de 3 tras el recorte)
- **THEN** el bot avisa de que no hay datos suficientes para tasar, sugiere un modelo más común, y NO descuenta crédito

### Requirement: Texto de recomendación con fallback determinista
El bot SHALL generar un texto de recomendación humanizado con IA, pero MUST renderizar la tasación con un texto determinista si la IA falla o excede el timeout.

#### Scenario: IA disponible
- **WHEN** la llamada IA de recomendación responde dentro del timeout
- **THEN** el bot muestra el consejo de precio con tono humano junto a los números

#### Scenario: IA falla o timeout
- **WHEN** la llamada IA falla o supera `AI_TIMEOUT_S`
- **THEN** el bot muestra igualmente la tasación con los números y un texto determinista, sin bloquear ni cancelar la respuesta

### Requirement: Persistencia en histórico
El bot MUST persistir los comparables scrapeados en `historico_precios` vía `guardar_historico_batch`, filtrando precio>0 y año>1990, igual que el resto de scrapeos.

#### Scenario: Comparables persistidos
- **WHEN** `/tasar` obtiene comparables de Wallapop o Coches.net
- **THEN** esos comparables se guardan en `historico_precios` antes de calcular la estadística

### Requirement: Coste de un crédito cobrado en éxito
El comando `/tasar` MUST costar el valor configurado en `COSTE_COMANDO["/tasar"]` (hoy 1) y descontarlo solo cuando la tasación se entrega con éxito, no al fallar por falta de comparables.

#### Scenario: Tasación entregada
- **WHEN** el bot entrega una tasación completa a un usuario no-admin
- **THEN** se descuenta 1 crédito de su cuenta

#### Scenario: Sin datos suficientes
- **WHEN** la tasación no se entrega por falta de comparables
- **THEN** no se descuenta ningún crédito

#### Scenario: Usuario admin
- **WHEN** un usuario admin usa `/tasar`
- **THEN** obtiene la tasación sin que se descuente crédito

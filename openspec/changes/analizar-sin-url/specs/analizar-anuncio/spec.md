## ADDED Requirements

### Requirement: Aceptar /analizar sin URL y pedir datos al usuario
Cuando el usuario envía `/analizar` sin URL, el bot SHALL responder con un mensaje que explica cómo introducir los datos del coche en texto libre, en vez de exigir una URL.

#### Scenario: /analizar sin URL
- **WHEN** el usuario envía `/analizar` sin ninguna URL en el mensaje
- **THEN** el bot responde pidiendo que escriba marca, modelo, año, km y precio del coche en texto libre, con un ejemplo ("VW Golf 2019 · 150.000 km · 9.500€"), y activa el estado de espera de datos manuales en `ctx.user_data`

#### Scenario: Usuario responde con datos suficientes
- **WHEN** el usuario ha activado el modo manual y envía un mensaje con marca, modelo, año, km y precio identificables
- **THEN** el bot parsea los datos con LLM, muestra confirmación ("✅ VW Golf 2019 · 150.000 km · 9.500€") y lanza el pipeline de análisis completo

#### Scenario: Usuario responde con datos incompletos
- **WHEN** el usuario ha activado el modo manual y el LLM no puede extraer algún campo crítico (marca, modelo, año, km o precio)
- **THEN** el bot pregunta específicamente los campos que faltan sin reiniciar el flujo

### Requirement: Ofrecer fallback manual cuando falla el scraping de URL
Cuando `obtener_anuncio_por_url` devuelve `None` o un anuncio con precio==0, el bot SHALL ofrecer un botón inline para introducir los datos manualmente, en vez de terminar con solo un mensaje de error.

#### Scenario: Scraping de URL falla
- **WHEN** el bot no puede extraer datos de una URL válida de Wallapop o Coches.net
- **THEN** el mensaje de error incluye un botón "✏️ Introducir datos a mano" además del texto de error habitual

#### Scenario: Usuario pulsa el botón de fallback manual
- **WHEN** el usuario pulsa "✏️ Introducir datos a mano" tras un fallo de scraping
- **THEN** el bot activa el modo manual y pide los datos del coche en texto libre

### Requirement: Pipeline idéntico desde buscar_comparables_todas
El análisis en modo manual MUST ejecutar exactamente el mismo pipeline que el flujo URL a partir de `buscar_comparables_todas`: comparables, estadísticas, veredicto IA, red flags, etiqueta DGT, botón preguntas/checklist.

#### Scenario: Análisis manual completo
- **WHEN** el bot tiene el objeto `Anuncio` construido manualmente
- **THEN** ejecuta `buscar_comparables_todas`, calcula estadísticas, genera veredicto IA y muestra el resultado con el mismo formato que el flujo URL

#### Scenario: Cabecera sin URL
- **WHEN** el análisis se ejecuta en modo manual (sin URL de origen)
- **THEN** la cabecera del veredicto muestra "📋 Datos introducidos manualmente" en lugar del enlace "Ver anuncio", y el resto del formato es idéntico

### Requirement: Coste de créditos idéntico al flujo URL
El modo manual MUST descontar 1 crédito del usuario (o el coste configurado en `COSTE_COMANDO["/analizar"]`) igual que el flujo con URL, gestionado por el mismo decorator `@requiere_acceso`.

#### Scenario: Crédito descontado en modo manual
- **WHEN** el usuario completa un análisis manual
- **THEN** se descuenta 1 crédito de su cuenta, sin importar si el análisis llegó por URL o por datos manuales

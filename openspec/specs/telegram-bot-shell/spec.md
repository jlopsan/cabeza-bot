# telegram-bot-shell

## Purpose

Define el shell de Telegram que sirve como única interfaz de usuario del bot. Incluye `/start`, `/ayuda`, registro de comandos, ConversationHandler para flujos multi-turn, manejo de errores global y tono de respuesta cavernícola (3-6 palabras). Archivos: [main.py](../../../main.py).

## Requirements

### Requirement: Comando /start de bienvenida
El bot SHALL responder a `/start` con un mensaje de bienvenida que presente la marca "Coches con cabeza", explique brevemente las capacidades activas, mencione los 3 créditos gratuitos diarios y liste los comandos principales disponibles.

#### Scenario: Usuario nuevo ejecuta /start
- **WHEN** un usuario envía `/start` por primera vez
- **THEN** el bot crea su registro vía `get_o_crear_usuario` y responde con el mensaje de bienvenida que incluye marca, propósito, créditos free, y al menos `/analizar`, `/ideal`, `/ayuda`

#### Scenario: Usuario existente repite /start
- **WHEN** un usuario ya registrado envía `/start`
- **THEN** el bot responde con el mismo mensaje de bienvenida sin duplicar el registro en base de datos

### Requirement: Tono cavernícola en mensajes al usuario
El bot MUST usar oraciones cortas (3-6 palabras) sin rellenos, preámbulos ni cortesías en todos los mensajes generados, exceptuando bloques estructurados (veredictos HTML, tablas de comparables) donde la legibilidad estructural prevalece.

#### Scenario: Respuesta a comando trivial
- **WHEN** el bot debe confirmar una acción (ej. "misión creada")
- **THEN** el mensaje contiene como máximo 6 palabras y ningún saludo o muletilla

### Requirement: Mensajes largos se trocean automáticamente
El bot MUST trocear cualquier respuesta mayor a 4000 caracteres mediante `_enviar_largo()` para evitar el límite de Telegram, preservando el `parse_mode` HTML y respetando los cortes en saltos de bloque.

#### Scenario: Veredicto largo de /analizar
- **WHEN** un veredicto de `/analizar` supera 4000 caracteres
- **THEN** el bot lo envía en múltiples mensajes consecutivos sin truncar contenido ni romper el HTML

### Requirement: Manejo global de errores
El bot MUST registrar cualquier excepción no capturada vía `error_handler` sin caer el proceso, y SHALL responder al usuario con un mensaje breve indicando que algo falló.

#### Scenario: Excepción en handler de comando
- **WHEN** un handler lanza una excepción no controlada
- **THEN** `error_handler` la registra en logs y el usuario recibe un mensaje corto de error sin stacktrace

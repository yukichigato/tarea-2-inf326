# Discusión de trade-offs para la USM

## Comparación

| Aspecto | Arquitectura 1 | Arquitectura 2 |
| --- | --- | --- |
| Objetivo principal | Registrar visitas que pasan por el acortador. | Reducir trabajo por visita y controlar el arribo de peticiones. |
| Redirección | 302 con `Cache-Control: no-store`. | 301 con `Cache-Control: public, max-age=TTL`. |
| Trabajo por visita recibida | Consulta SQLite y escritura remota por gRPC. | Control de cuota y consulta al caché; SQLite solo cuando falta una respuesta vigente. |
| Analítica | Conserva la URL y el timestamp de cada GET que logra registrar. | Las visitas pueden resolverse en el navegador; no hay registro de analítica. |
| Disponibilidad | La redirección depende del acortador y del registro. | La redirección no depende del registro; una respuesta cacheada puede evitar una lectura de SQLite. |
| Operación | Dos procesos y dos bases para esta arquitectura. | Un proceso, una base y estado de caché/cuotas en memoria. |
| Cambios del destino | Requieren resolver otra vez en el servidor. | Una redirección guardada puede conservar el destino hasta vencer su vigencia. |

## Analítica y disponibilidad

Para una campaña de admisión o difusión de postgrados, la USM podría necesitar medir cuándo se usan sus enlaces. V1 mantiene el contacto con el servicio y registra URL y hora antes de redirigir. Separar el registro por gRPC permite evolucionar su almacenamiento de forma independiente.

La confirmación de escritura agrega latencia y una dependencia al camino de redirección. Esta implementación devuelve 503 cuando el registro falla o supera el timeout. Se prioriza no responder como si la visita hubiera sido registrada. La alternativa de redirigir igualmente mejoraría la disponibilidad del enlace, pero perdería eventos; una cola persistente reduciría ese acoplamiento a cambio de otro componente y de consistencia eventual.

La respuesta 302 por sí sola no expresa una prohibición absoluta de almacenamiento. Por eso v1 agrega `Cache-Control: no-store`, la directiva que impide a cachés conformes guardar la respuesta. La semántica está definida en [RFC 9111, sección 5.2.2.5](https://www.rfc-editor.org/rfc/rfc9111.html#section-5.2.2.5).

Los registros no equivalen a personas ni a visitas exitosas al destino: pueden incluir bots, reintentos o usuarios que cierran la navegación. Tampoco se garantiza procesamiento exactamente una vez: si la base del registro confirma la escritura y se pierde la respuesta gRPC, el cliente puede reintentar y generar otro evento. Se necesitaría un identificador de evento y deduplicación para cambiar esa garantía.

## Rendimiento, caché y cuotas

Para enlaces institucionales estables y muy consultados, v2 puede disminuir consultas a SQLite con el caché de LiteStar y permitir que el navegador resuelva nuevas visitas sin llegar al servidor. Esto reduce trabajo por petición y también reduce la visibilidad sobre los accesos.

El TTL acota cuánto dura una copia en el caché de servidor y cuánto tiempo se anuncia como fresca en el navegador. No se implementa edición de destinos en esta entrega; si se agregara, habría que invalidar las copias del servidor y considerar que las respuestas ya almacenadas por clientes no se pueden retirar de inmediato. La política de caché de LiteStar se configura para guardar solo 301, evitando almacenar errores o repetir una creación desde caché.

El rate limiter limita peticiones por IP antes de atenderlas. Una respuesta cacheada también consume cuota, de modo que el caché no permite eludir el control. Esto ayuda a contener carga, aunque usuarios detrás de una misma salida de la USM comparten cuota. El límite predeterminado de 60 por minuto es un valor inicial configurable, no un resultado de dimensionamiento. La cuota no cubre las visitas resueltas enteramente por el navegador porque no llegan al servicio.

Ambas tácticas usan memoria de un único proceso. Reiniciar el contenedor elimina caché y cuotas, pero mantiene las equivalencias en el volumen SQLite. Si hubiera varias réplicas, sus copias y límites serían independientes. Para una cuota compartida y un caché común habría que agregar un almacenamiento compartido y verificar la atomicidad del conteo.

No se presentan porcentajes de mejora ni capacidad máxima: las pruebas validan comportamiento funcional y no reemplazan una medición de carga con tráfico representativo de la universidad.

## Persistencia y operación

SQLite evita administrar un servidor de base de datos adicional y permite transacciones para asignar el ID y su hash. WAL facilita coexistencia de lecturas y escrituras, pero las escrituras se serializan. El volumen persistente sobrevive a recreaciones del servicio; no representa una réplica ni reemplaza respaldos.

La imagen final excluye herramientas de prueba y compilación de protobuf. Un mismo artefacto ejecuta los roles HTTP y gRPC con distintos comandos, lo que reduce variantes de despliegue. Los certificados locales cumplen el requisito académico de conexión segura. Un despliegue institucional necesitaría certificados para sus dominios y una política operacional de renovación y respaldo; los certificados compartidos de esta demostración no modelan identidades independientes por servicio.

Para la USM, la elección depende de la necesidad de analítica: v1 aporta registros de uso y acepta el costo de esa dependencia; v2 favorece enlaces estables con menos trabajo en el servidor. Una necesidad simultánea de alta disponibilidad y analítica exigiría revisar el registro síncrono, las garantías de eventos y el almacenamiento compartido.

## Bonus: identificadores únicos en un ambiente distribuido

La secuencia de SQLite garantiza unicidad dentro de cada base del despliegue actual. Dos nodos con bases independientes y la misma secuencia inicial generarían los mismos identificadores y hashes. Base62 representa un número de forma reversible; no agrega unicidad a números repetidos.

Una solución sencilla es que todos los nodos utilicen una única base transaccional autoritativa, con una secuencia de enteros de 64 bits, clave primaria en `id` y restricción única en `hash`. Cada creación obtiene un número de esa secuencia, calcula base62, guarda la equivalencia y confirma la transacción antes de devolver la URL. PostgreSQL ofrece `nextval`, que asigna valores distintos de forma atómica ante llamadas concurrentes. Puede haber saltos por transacciones abortadas; esos saltos no perjudican la unicidad. Véase la [documentación de secuencias de PostgreSQL](https://www.postgresql.org/docs/current/functions-sequence.html).

La condición esencial es mantener una sola autoridad de escritura y un procedimiento de failover que no reutilice identificadores ya confirmados. Iniciar una segunda secuencia independiente o recuperar un contador desde un respaldo atrasado anularía esa garantía. Si la autoridad no está disponible, se detienen las nuevas asignaciones en lugar de generar números locales que puedan colisionar.

Si la asignación por registro fuese un cuello de botella medido, la misma autoridad podría reservar rangos disjuntos de forma transaccional y durable para cada nodo. Cada rango se usaría una sola vez; un nodo que pierde su estado debe abandonar su rango anterior. Esto reduce coordinación por URL, pero puede desperdiciar identificadores y complica la recuperación. UUID aleatorios o combinaciones de reloj y nodo requieren garantías adicionales si se pretende demostrar unicidad absoluta, por lo que no se usan como sustituto automático de la coordinación.

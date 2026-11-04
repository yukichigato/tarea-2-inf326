# Discusión de trade-offs para la USM

## Elección entre las arquitecturas

| Aspecto | Arquitectura 1 | Arquitectura 2 |
| --- | --- | --- |
| Respuesta | 302 con `no-store`. | 301 con `public, max-age=TTL`. |
| Trabajo de una visita válida | Lectura de URL y registro remoto confirmado. | Control de cuota y caché; lectura de URL si falta una copia vigente. |
| Analítica | Guarda la URL original y hora de resolución. | No registra visitas; algunas no llegan al servidor por el caché del navegador. |
| Disponibilidad | Redirigir depende también del registro gRPC. | No depende del registro. |
| Operación | Dos procesos y dos bases para esta arquitectura. | Un proceso, una base y estado de caché/cuotas en memoria. |

Para una campaña que necesite registros de uso, v1 ofrece analítica con el costo de esperar su persistencia. Para enlaces estables sin esa necesidad, v2 evita trabajo repetido. No se dispone de tráfico real de la USM para cuantificar esa diferencia ni para afirmar cuál será más utilizada.

## Latencia y disponibilidad del registro

El beneficio de esperar gRPC es concreto: el acortador emite 302 después de recibir la confirmación del registro. El costo incluye serialización, comunicación, espera por un trabajador y escritura en su base. Separar el servicio permite evolucionarlo por su cuenta, pero mantiene esa dependencia en el camino de redirección. No hay un bróker en esta implementación: es una llamada gRPC síncrona.

Si el registro falla, v1 devuelve 503 sin `Location`. Redirigir igualmente favorecería la disponibilidad a costa de perder analítica. Incorporar una cola persistente cambiaría ese equilibrio, pero agregaría otro componente y confirmación diferida; no está implementado.

Los caminos tienen trabajo distinto:

| Camino | Operaciones relevantes |
| --- | --- |
| POST válido | Validación e inserción transaccional. |
| GET válido en v1 | Lectura de URL y espera de gRPC. |
| GET válido en v2 sin copia vigente | Cuota, lectura de URL y almacenamiento en caché. |
| GET válido en v2 con copia vigente | Cuota y caché; no consulta la URL en SQLite. |
| GET inexistente | Consulta de URL sin registro; en v2 pasa antes por la cuota. |
| Rechazo 429 | Control de cuota sin ejecutar la resolución. |
| Redirección guardada por el navegador | No contacta al acortador. |

No se asigna el mismo tiempo a esos caminos ni se calcula una media sin sus frecuencias y mediciones. `GRPC_TIMEOUT=2` limita la espera de la RPC, no toda la petición HTTP. La consulta de URLs ocurre antes y SQLite configura hasta 10 segundos de espera ante bloqueo. También existen esperas de ejecución y red; sumar parámetros no demuestra un peor caso total.

La consecuencia de agotar la RPC es 503, pero no garantiza que el evento esté ausente: el servidor puede completar una escritura ya iniciada después de que el cliente deje de esperar. `test_grpc_timeout_can_be_followed_by_a_committed_event` reproduce ese caso. La cancelación no interrumpe automáticamente todo el código de aplicación; véanse las guías gRPC de [deadlines](https://grpc.io/docs/guides/deadlines/) y [cancelación](https://grpc.io/docs/guides/cancellation/).

## Consistencia y cambios de datos

**Base de URLs.** ID y hash se confirman en la misma transacción, con una restricción única sobre el hash. Otras conexiones leen transacciones confirmadas, de acuerdo con el [aislamiento de SQLite](https://www.sqlite.org/isolation.html). WAL permite coexistencia de lecturas y escrituras, pero no elimina la serialización de escrituras.

**Registro remoto.** Su commit es independiente de la base de URLs y de la entrega de la respuesta HTTP; no hay una transacción distribuida. Un error de comunicación puede dejar un evento persistido y un reintento puede producir otro. No se garantiza procesamiento exactamente una vez. El timestamp se toma antes del envío: la inserción concurrente no establece un orden global por timestamp, y un registro tampoco prueba que el navegador haya abierto el destino.

**Copias de v2.** El servidor conserva respuestas 301 hasta vencer su TTL o reiniciar. Cada navegador tiene su propia vigencia: vencer el TTL del servidor no elimina una copia ya guardada por un cliente. No hay invalidación por edición ni coordinación entre réplicas. V1 agrega `no-store` explícitamente para impedir el almacenamiento en cachés conformes; el código 302 por sí solo no expresa esa prohibición. Véase [RFC 9111](https://www.rfc-editor.org/rfc/rfc9111.html#section-5.2.2.5).

**Destino externo.** La API no permite editar equivalencias; eso no hace inmutables las páginas. Pueden cambiar, desaparecer o redirigir por su cuenta, y el acortador no descarga su contenido. Actualmente, publicar otra URL requiere crear otro enlace corto. Corregir el destino del mismo enlace exigiría ampliar la API y definir invalidación del caché del servidor y tratamiento de copias existentes en clientes.

## Ráfagas, cuotas y datos desconocidos

Una ráfaga puede encontrar el caché frío: varias peticiones pueden consultar la base antes de que exista una copia, porque no se agrupan consultas simultáneas por hash. V1 tiene ocho trabajadores gRPC y escrituras SQLite serializadas; de ese número no se deduce una tasa de atención.

El límite de v2 se aplica por IP, incluso sobre respuestas cacheadas. Usuarios tras una misma salida de la USM comparten cuota. El valor de 60 peticiones por minuto es una política configurable, no una estimación de demanda o capacidad global. Caché y cuotas son locales a un proceso: reiniciarlo los vacía y añadir réplicas no crea un límite compartido.

Las pruebas verifican 16 RPC desde ocho hilos emisores y ocho GET simultáneos con cuota de tres; sus resultados están en [la validación](validation.md). No se conocen el volumen de visitas esperado por la USM, su distribución temporal ni la proporción de aciertos de caché. No se inventan esos valores ni se presentan percentiles, capacidad máxima o garantías temporales a partir de pruebas funcionales.

## Bonus: unicidad en un ambiente distribuido

La secuencia actual garantiza unicidad en cada base SQLite. Dos nodos con bases independientes y el mismo contador producirían los mismos hashes: base62 no agrega unicidad a números repetidos.

Una solución es usar una base transaccional autoritativa para todos los nodos, con una secuencia de 64 bits, clave primaria en `id` y restricción única en `hash`. Cada creación obtiene el ID, calcula base62 y confirma la equivalencia antes de responder. PostgreSQL ofrece `nextval` atómico entre sesiones concurrentes; puede dejar saltos, lo que no perjudica la unicidad. Véase su [documentación de secuencias](https://www.postgresql.org/docs/current/functions-sequence.html).

Se requiere una sola autoridad de escritura y un failover que no reutilice IDs confirmados; otra secuencia independiente o un contador restaurado desde un respaldo atrasado rompería esa condición. Si la autoridad no está disponible, se detienen las nuevas asignaciones. Esta alternativa agrega una dependencia operativa y no está implementada en la entrega.

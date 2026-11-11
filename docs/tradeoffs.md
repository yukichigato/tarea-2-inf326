# Discusión de trade-offs para la USM

La USM, como mandante, necesita un acortador que cree URLs cortas y redirija a las URLs originales. Las dos arquitecturas implementadas cumplen esos requisitos funcionales y difieren en los atributos de calidad que favorecen. La arquitectura 1 favorece la analítica de visitas, mientras que la arquitectura 2 favorece el desempeño y una operación más simple. A continuación se comparan ambas en desempeño, disponibilidad, consistencia y operación, y se evalúa cuál conviene para distintos usos de la USM. El equipo carece de datos de tráfico de la USM, así que la comparación es cualitativa y omite estimaciones de volumen, de tasa de acierto del caché y de latencia.

## Arquitectura 1: redirección temporal con registro de visitas

El acortador de la arquitectura 1 responde con 302 y `Cache-Control: no-store`, una directiva que prohíbe a las cachés conformes guardar la respuesta ([RFC 9111, §5.2.2.5](https://www.rfc-editor.org/rfc/rfc9111.html#section-5.2.2.5)). Por eso, cada visita llega al acortador, que envía un evento `url hit` con la URL y el timestamp al servicio Log. El acortador redirige recién cuando Log confirma la escritura, de modo que cada redirección entregada tiene un evento registrado.

Este beneficio tiene cuatro costos. El primero es la latencia: cada visita suma una llamada gRPC síncrona con TLS y una escritura en la base del registro.

El segundo costo es la disponibilidad. La redirección depende de dos componentes, y cuando Log tarda más que `GRPC_TIMEOUT`, el acortador responde 503. Con ello, el diseño prioriza que el registro esté completo por sobre que la redirección esté disponible. Del código se deduce, sin haberlo medido, que cada visita ocupa un hilo del servidor HTTP mientras espera a Log. Con Log lento, si se ocuparan todos los hilos, también se retrasaría la creación de URLs.

El tercer costo es la consistencia. El acortador y Log confirman sus escrituras por separado, sin una transacción común. Un timeout puede ocurrir después de que Log guardó el evento; en ese caso, el registro contiene un evento de una visita que recibió 503. La prueba `test_grpc_timeout_can_be_followed_by_a_committed_event` reproduce este caso, que se explica por el funcionamiento de los [deadlines de gRPC](https://grpc.io/docs/guides/deadlines/). Si el usuario reintenta, se agrega otro evento. El diseño asegura un evento por cada 302, y el registro puede contener además eventos de visitas fallidas.

El cuarto costo es operativo, porque la arquitectura requiere mantener dos servicios y dos bases de datos.

## Arquitectura 2: redirección permanente con rate limiter y caché

El acortador de la arquitectura 2 responde con 301 y `Cache-Control: public, max-age=60`, lo que autoriza al navegador a guardar la redirección. Mientras su copia está vigente, el navegador resuelve las visitas repetidas sin contactar al servidor.

En el servidor, el caché en memoria de LiteStar implementa la táctica de mantener múltiples copias de datos: entrega la respuesta sin consultar SQLite durante `CACHE_TTL` segundos. El rate limiter de LiteStar implementa la táctica de controlar el arribo de eventos: admite hasta `RATE_LIMIT_REQUESTS` peticiones por minuto de cada IP y responde 429 a las peticiones en exceso antes de resolver la URL. Así, un cliente abusivo consume una cantidad acotada de trabajo del servidor. Además, esta arquitectura funciona con un solo servicio y una sola base.

Estos beneficios también tienen costos. El principal es que el servidor pierde la analítica: las visitas que resuelve el navegador nunca llegan a él, por lo que tampoco podría contarlas un registro agregado en el servidor.

El servidor tampoco controla las copias guardadas en los navegadores. La API actual mantiene fijo el destino de cada enlace, pero la página de destino puede cambiar o desaparecer. Si una versión futura permitiera corregir un enlace, cada navegador seguiría usando su copia hasta que venciera, porque el servidor carece de un mecanismo HTTP para invalidarla.

El rate limiter calcula la cuota por IP. Los usuarios que salen a internet por la misma IP, como los de la red de un campus, comparten una cuota y pueden recibir 429 aunque hagan un uso legítimo. En el despliegue con Docker de esta entrega, el log de acceso mostró que todas las peticiones del host llegan con la IP del gateway de Docker y, por lo tanto, comparten una sola cuota.

Por último, el caché y las cuotas residen en la memoria de un proceso. Se pierden al reiniciarlo, y cada réplica tendría los suyos. Además, cuando una ráfaga encuentra el caché vacío, varias peticiones pueden consultar la base antes de que exista una copia.

## Comparación por camino de ejecución

La siguiente tabla compara el trabajo que realiza cada arquitectura en cada camino. Los tiempos que aparecen son parámetros configurados, y `GRPC_TIMEOUT` acota solo la llamada gRPC dentro de la petición HTTP.

| Camino | Arquitectura 1 | Arquitectura 2 |
| --- | --- | --- |
| Creación de una URL (`POST`) | El acortador valida la URL y la inserta en SQLite. | El rate limiter descuenta la cuota, y luego el acortador valida e inserta la URL. |
| Visita a un hash existente | El acortador lee la URL en SQLite, espera la confirmación de Log y responde 302. | El rate limiter descuenta la cuota, y el acortador usa la copia del caché o lee SQLite antes de responder 301. |
| Visita repetida desde el mismo navegador | El navegador repite el camino completo. | El navegador usa su copia y evita contactar al servidor mientras esa copia esté vigente. |
| Visita a un hash inexistente | El acortador responde 404 sin generar un evento. | El rate limiter descuenta la cuota, y el acortador responde 404 sin guardar la respuesta en el caché. |
| Log lento o detenido | El acortador espera hasta `GRPC_TIMEOUT` y responde 503. | El acortador sigue funcionando, porque no usa Log. |
| Exceso de peticiones | El acortador atiende todas las peticiones. | El rate limiter responde 429 sin resolver la URL. |

## Conveniencia para la USM

| Uso en la USM | Arquitectura | Razón |
| --- | --- | --- |
| Campañas que la USM necesita medir, como la postulación a postgrado del enunciado, la admisión o la inscripción a eventos. | 1 | La arquitectura 1 registra cada visita, mientras que en la 2 las visitas resueltas por el navegador quedan sin registrar. |
| Enlaces estables en reglamentos, documentos oficiales o códigos QR impresos. | 2 | El destino de estos enlaces se mantiene en el tiempo, y la redirección permanente con caché evita repetir trabajo. |
| Publicaciones con mucha demanda simultánea, como resultados o matrícula. | 2, con la cuota revisada | El caché reduce la carga sobre la base, y una cuota por IP demasiado baja rechazaría a estudiantes que comparten red. |

La arquitectura 1 obtiene información de uso a cambio de latencia y disponibilidad. La arquitectura 2 obtiene desempeño y simplicidad a cambio de la analítica y del control sobre las copias. La conveniencia de cada una depende del propósito de cada enlace, y la USM podría operar ambas.

Una alternativa intermedia, fuera de esta implementación, consiste en responder 302 sin esperar a Log y enviar el evento después mediante una cola. Con ella, la redirección dejaría de depender del registro. Sin embargo, una cola en memoria perdería los eventos pendientes al reiniciarse, y una cola persistente agregaría otro componente al despliegue.

El evento de la arquitectura 1 guarda solo la URL y la hora de cada visita. Registrar la IP u otros identificadores del visitante convertiría el log en un registro de datos personales, y la USM tendría que definir su finalidad y su plazo de retención. La arquitectura 2 usa la IP para calcular la cuota y la mantiene solo en memoria.

## Evidencia y datos desconocidos

Las pruebas automatizadas, descritas en el [README](../README.md#pruebas), verifican la concurrencia en la asignación de identificadores, en el registro gRPC y en el rate limiter, y reproducen el timeout ambiguo de gRPC. La IP que observa el rate limiter en Docker se obtuvo aparte, del log de acceso de la arquitectura 2.

Los valores `CACHE_TTL=60`, `RATE_LIMIT_REQUESTS=60` y `GRPC_TIMEOUT=2` son parámetros de configuración elegidos para la entrega, independientes de cualquier estimación de capacidad. El volumen de visitas de la USM, su distribución en el tiempo y la tasa de acierto del caché son desconocidos. Por eso, las comparaciones de rendimiento de este documento son cualitativas.

## Bonus: unicidad de identificadores en un ambiente distribuido

La codificación base62 asigna un hash distinto a cada número, así que los hashes son únicos cuando los `id` lo son. La secuencia SQLite actual garantiza `id` únicos dentro de un nodo. En cambio, dos nodos con bases independientes generarían los mismos `id` y, por lo tanto, los mismos hashes. Un ambiente distribuido necesita una estrategia que impida entregar el mismo `id` dos veces. La tabla describe tres estrategias:

| Estrategia | Funcionamiento | Condición | Costo |
| --- | --- | --- | --- |
| Secuencia central | Una base autoritativa asigna cada `id`. En PostgreSQL, [`nextval`](https://www.postgresql.org/docs/current/functions-sequence.html) lo hace de forma atómica. | La base debe ser el único escritor, y un failover debe continuar la secuencia sin reutilizar IDs. | Cada creación depende de la disponibilidad de esa base. |
| Rangos disjuntos | Un asignador entrega a cada nodo un bloque de IDs sin solapamiento, y el nodo asigna localmente dentro de su bloque. | El asignador debe persistir cada bloque antes de entregarlo y nunca reasignarlo. | Los IDs dejan de crecer en orden entre nodos, y los bloques sin usar dejan huecos. |
| ID compuesto, al estilo Snowflake | Cada nodo arma el `id` con el tiempo, su número de nodo y un contador local. | Cada nodo debe tener un número único y un reloj que siempre avance. | Los hashes se alargan, y los IDs dejan de partir en `597652313` con incremento de 1. |

En cualquiera de las tres, la restricción `UNIQUE` sobre `hash` hace que la base rechace un hash repetido si la estrategia llegara a fallar. Con un solo punto de escritura, la secuencia central es la opción más simple. Si la USM agregara nodos de escritura, los rangos disjuntos conservarían hashes cortos y consultarían al asignador solo al agotar cada bloque. Los UUID aleatorios y el hash de la URL larga reducen la probabilidad de colisión, pero no la eliminan sin una verificación adicional, así que tampoco aseguran la unicidad que exige el enunciado.

## Referencias

- L. Bass, P. Clements y R. Kazman, *Software Architecture in Practice*, 4.ª ed., Addison-Wesley, 2021.
- [RFC 9111: HTTP Caching](https://www.rfc-editor.org/rfc/rfc9111.html).
- [gRPC: Deadlines](https://grpc.io/docs/guides/deadlines/).
- [PostgreSQL: Sequence Manipulation Functions](https://www.postgresql.org/docs/current/functions-sequence.html).
